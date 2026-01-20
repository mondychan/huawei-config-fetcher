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

    try:
        password = decrypt_secret(data_key, device.password_enc)
    except Exception:
        return {"device": device.name, "status": "skipped", "reason": "decrypt-failed"}

    output = fetch_device_config(
        device=device,
        password=password,
        known_hosts=known_hosts,
        host_key_callback=host_key_callback,
    )

    if output is None:
        return {"device": device.name, "status": "skipped", "reason": "host-key"}

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
