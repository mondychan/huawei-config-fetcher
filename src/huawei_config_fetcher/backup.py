from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import storage
from .models import Config, Device
from .secrets import decrypt_secret
from .ssh_client import HostKeyCallback, fetch_device_config


Result = Dict[str, str]
ResultCallback = Callable[[Result], None]

MIN_COMPLETE_RATIO = 0.6
MIN_BYTES_FOR_RATIO = 4096


def _auto_workers(device_count: int) -> int:
    cpu = os.cpu_count() or 4
    return min(max(4, cpu * 2), 32, device_count)


def _backup_one(
    device: Device,
    data_key: bytes,
    base_dir: Path,
    known_hosts: dict,
    host_key_callback: HostKeyCallback,
) -> Result:
    if not device.username or not device.password_enc:
        return {"device": device.name, "status": "skipped", "reason": "missing-credentials"}

    def last_backup_bytes() -> Optional[int]:
        manifest_path = storage.device_dir(base_dir, device.id, device.name) / "manifest.json"
        records = storage.load_manifest(manifest_path)
        if not records:
            return None
        value = records[-1].get("bytes")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def looks_truncated(output: str, previous_bytes: Optional[int]) -> bool:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            return True
        last_line = lines[-1].lower()
        ends_with_return = last_line == "return"

        output_bytes = len(output.encode("utf-8"))
        if previous_bytes and previous_bytes >= MIN_BYTES_FOR_RATIO:
            if output_bytes < previous_bytes * MIN_COMPLETE_RATIO:
                return True

        if not ends_with_return and output_bytes < MIN_BYTES_FOR_RATIO:
            return True

        return False

    try:
        password = decrypt_secret(data_key, device.password_enc)
    except Exception:
        return {"device": device.name, "status": "skipped", "reason": "decrypt-failed"}

    previous_bytes = last_backup_bytes()
    output = fetch_device_config(
        device=device,
        password=password,
        known_hosts=known_hosts,
        host_key_callback=host_key_callback,
    )

    if output is None:
        return {"device": device.name, "status": "skipped", "reason": "host-key"}

    if looks_truncated(output, previous_bytes):
        retry_output = fetch_device_config(
            device=device,
            password=password,
            known_hosts=known_hosts,
            host_key_callback=host_key_callback,
        )
        if retry_output is None:
            return {"device": device.name, "status": "skipped", "reason": "host-key"}
        if looks_truncated(retry_output, previous_bytes):
            return {"device": device.name, "status": "error", "reason": "partial-output"}
        output = retry_output

    record = storage.write_backup(base_dir, device.id, device.name, output)
    return {
        "device": device.name,
        "status": "ok",
        "timestamp": record["timestamp"],
        "hash": record["hash"],
    }


def run_backup(
    cfg: Config,
    data_key: bytes,
    base_dir: Path,
    devices: List[Device],
    host_key_callback: HostKeyCallback,
    on_result: Optional[ResultCallback] = None,
) -> List[Result]:
    if not devices:
        return []

    workers = cfg.settings.workers or _auto_workers(len(devices))

    results: List[Result] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_backup_one, device, data_key, base_dir, cfg.known_hosts, host_key_callback): device
            for device in devices
        }
        for future in as_completed(futures):
            device = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"device": device.name, "status": "error", "reason": str(exc)}
            results.append(result)
            if on_result is not None:
                on_result(result)

    return results
