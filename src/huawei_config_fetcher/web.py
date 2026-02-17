from __future__ import annotations

import asyncio
import json
import math
import os
from collections import Counter
from datetime import datetime, time, timedelta
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import backup as backup_mod
from . import config as config_mod
from . import secrets
from . import storage
from .models import Device


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_time(value: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except Exception:
        return time(2, 0)


def _format_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "never"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_bytes(value: Optional[int]) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _next_run_at(now: datetime, start_time: time, interval_hours: int) -> datetime:
    start = datetime.combine(now.date(), start_time)
    if start > now:
        return start
    hours_since = (now - start).total_seconds() / 3600.0
    steps = math.floor(hours_since / interval_hours) + 1
    return start + timedelta(hours=interval_hours * steps)


def _summarize_results(results: List[Dict[str, str]]) -> str:
    if not results:
        return "empty"
    statuses = [item.get("status", "error") for item in results]
    if any(status == "error" for status in statuses):
        return "error"
    if any(status != "ok" for status in statuses):
        return "partial"
    return "ok"


class BackupService:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.base_dir = Path.cwd() / "backups"
        self.lock = asyncio.Lock()
        self.running = False
        self.last_run_at: Optional[datetime] = None
        self.last_run_finished_at: Optional[datetime] = None
        self.last_run_duration: Optional[float] = None
        self.last_run_status: str = "never"
        self.last_run_reason: str = "-"
        self.last_error: Optional[str] = None
        self.last_results: List[Dict[str, str]] = []
        self.next_run_at: Optional[datetime] = None
        self.schedule_task: Optional[asyncio.Task] = None
        self.notice: Optional[str] = None
        self.notice_error: bool = False
        self._load_state()

    def _state_path(self) -> Path:
        return self.base_dir / "backup_state.json"

    def _log_path(self) -> Path:
        return self.base_dir / "backup.log"

    def _log_old_path(self) -> Path:
        return self.base_dir / "backup.log.old"

    def _load_state(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        self.last_run_at = _parse_dt(data.get("last_run_at"))
        self.last_run_finished_at = _parse_dt(data.get("last_run_finished_at"))
        self.last_run_duration = data.get("last_run_duration")
        self.last_run_status = data.get("last_run_status", self.last_run_status)
        self.last_run_reason = data.get("last_run_reason", self.last_run_reason)
        self.last_error = data.get("last_error")
        self.last_results = data.get("last_results", []) or []

    def _save_state(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_run_finished_at": self.last_run_finished_at.isoformat() if self.last_run_finished_at else None,
            "last_run_duration": self.last_run_duration,
            "last_run_status": self.last_run_status,
            "last_run_reason": self.last_run_reason,
            "last_error": self.last_error,
            "last_results": self.last_results,
        }
        self._state_path().write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _append_log(self, lines: List[str], max_lines: int = 1000) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path()
        old_path = self._log_old_path()

        existing_lines = 0
        if log_path.exists():
            with log_path.open("r", encoding="utf-8") as handle:
                existing_lines = sum(1 for _ in handle)

        if existing_lines + len(lines) > max_lines and log_path.exists():
            if old_path.exists():
                old_path.unlink()
            log_path.replace(old_path)

        with log_path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def _log_run(self, reason: str, results: List[Dict[str, str]], error: Optional[str]) -> None:
        timestamp = _format_dt(self.last_run_at)
        status = self.last_run_status
        lines = [f"run {timestamp} reason={reason} status={status}"]
        if error:
            lines.append(f"error {error}")
        if results:
            for item in results:
                device = item.get("device", "-")
                state = item.get("status", "-")
                detail = item.get("hash") or item.get("reason") or "-"
                lines.append(f"{timestamp} device={device} status={state} detail={detail}")
        else:
            lines.append(f"{timestamp} no-results")
        lines.append("---")
        self._append_log(lines)

    async def start(self) -> None:
        if self.schedule_task and not self.schedule_task.done():
            return
        self.schedule_task = asyncio.create_task(self._schedule_loop())

    async def _schedule_loop(self) -> None:
        while True:
            if not self.config_path.exists():
                self.next_run_at = None
                await asyncio.sleep(30)
                continue

            try:
                cfg = config_mod.load_config(self.config_path)
            except Exception:
                self.next_run_at = None
                await asyncio.sleep(30)
                continue

            settings = cfg.settings
            signature = (
                settings.schedule_enabled,
                settings.schedule_time,
                settings.schedule_interval_hours,
            )

            if not settings.schedule_enabled:
                self.next_run_at = None
                await asyncio.sleep(30)
                continue

            start_time = _parse_time(settings.schedule_time)
            interval_hours = max(1, int(settings.schedule_interval_hours))
            next_run = _next_run_at(datetime.now(), start_time, interval_hours)
            self.next_run_at = next_run

            while True:
                await asyncio.sleep(30)
                if not self.config_path.exists():
                    break
                try:
                    cfg = config_mod.load_config(self.config_path)
                except Exception:
                    break

                new_signature = (
                    cfg.settings.schedule_enabled,
                    cfg.settings.schedule_time,
                    cfg.settings.schedule_interval_hours,
                )
                if new_signature != signature:
                    break

                if datetime.now() >= next_run:
                    await self.run_backup(reason="schedule")
                    break

    async def run_backup(self, reason: str = "manual") -> Dict[str, str]:
        if self.lock.locked():
            return {"status": "busy"}

        async with self.lock:
            self.running = True
            self.last_run_at = datetime.now()
            self.last_run_reason = reason
            self.last_error = None
            start = datetime.now()

            try:
                auto_trust = _env_bool("HCF_AUTO_TRUST_HOST_KEYS", False)
                results = await asyncio.to_thread(self._run_backup_sync, auto_trust)
                self.last_results = results
                self.last_run_status = _summarize_results(results)
            except Exception as exc:
                self.last_results = []
                self.last_run_status = "error"
                self.last_error = str(exc)
            finally:
                end = datetime.now()
                self.last_run_finished_at = end
                self.last_run_duration = (end - start).total_seconds()
                self.running = False
                try:
                    self._log_run(self.last_run_reason, self.last_results, self.last_error)
                    self._save_state()
                except Exception:
                    pass

        return {"status": self.last_run_status}

    def trigger_backup(self, reason: str = "manual") -> bool:
        if self.running:
            return False
        asyncio.create_task(self.run_backup(reason=reason))
        return True

    def _run_backup_sync(self, auto_trust: bool) -> List[Dict[str, str]]:
        if not self.config_path.exists():
            raise RuntimeError(f"Config not found: {self.config_path}")

        cfg = config_mod.load_config(self.config_path)

        data_key = _get_data_key(cfg)

        devices = cfg.devices
        base_dir = self.base_dir

        def host_key_callback(_host_id: str, _new_fp: str, _old_fp: Optional[str]) -> bool:
            return auto_trust

        results = backup_mod.run_backup(cfg, data_key, base_dir, devices, host_key_callback)
        config_mod.save_config(cfg, self.config_path)
        return results


config_path = config_mod.resolve_config_path(None)
service = BackupService(config_path)

templates_dir = resources.files("huawei_config_fetcher").joinpath("templates")
templates = Jinja2Templates(directory=str(templates_dir))

app = FastAPI()
fonts_dir = Path(os.getenv("HCF_FONTS_DIR", str(Path.cwd() / "fonts")))
try:
    fonts_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/fonts", StaticFiles(directory=str(fonts_dir)), name="fonts")
except OSError:
    pass


def _redirect_message(text: str, error: bool = False) -> RedirectResponse:
    service.notice = text
    service.notice_error = error
    return RedirectResponse("/", status_code=303)


def _get_data_key(cfg) -> bytes:
    data_key = secrets.data_key_from_env(
        cfg.security.salt_b64,
        cfg.security.wrap_nonce_b64,
        cfg.security.wrapped_key_b64,
    )
    if data_key is None:
        data_key = secrets.get_keyring_data_key(cfg.security.keyring_service, cfg.security.keyring_user)
    if data_key is None:
        raise RuntimeError("Missing data key. Set HCF_MASTER_PASSWORD or HCF_DATA_KEY_B64.")
    return data_key


def _find_device(cfg, device_id: int) -> Optional[Device]:
    for device in cfg.devices:
        if device.id == device_id:
            return device
    return None


def _device_dirs(base_dir: Path, device_id: int, device_name: Optional[str]) -> List[Path]:
    device_dirs: List[Path] = []
    if device_name:
        preferred = storage.device_dir(base_dir, device_id, device_name)
        if preferred.exists():
            device_dirs.append(preferred)
    if base_dir.exists():
        for path in sorted(base_dir.glob(f"{device_id:03d}-*")):
            if path.is_dir() and path not in device_dirs:
                device_dirs.append(path)
    return device_dirs


def _load_backup_records(base_dir: Path, device_id: int, device_name: Optional[str]) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    for device_dir in _device_dirs(base_dir, device_id, device_name):
        manifest_path = device_dir / "manifest.json"
        for record in storage.load_manifest(manifest_path):
            entry = dict(record)
            entry["dir"] = device_dir.name
            records.append(entry)
    records.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    return records


def _find_backup_entry(
    base_dir: Path,
    device_id: int,
    device_name: Optional[str],
    filename: str,
) -> tuple[Optional[Path], Optional[Dict[str, str]]]:
    for device_dir in _device_dirs(base_dir, device_id, device_name):
        manifest_path = device_dir / "manifest.json"
        for record in storage.load_manifest(manifest_path):
            if record.get("file") == filename:
                return device_dir / filename, record
        candidate = device_dir / filename
        if candidate.exists() and candidate.is_file():
            return candidate, None
    return None, None


def _validate_filename(filename: str) -> None:
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")


@app.on_event("startup")
async def _startup() -> None:
    await service.start()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    config_exists = service.config_path.exists()
    cfg = config_mod.load_config(service.config_path) if config_exists else None

    schedule = {
        "enabled": cfg.settings.schedule_enabled if cfg else False,
        "time": cfg.settings.schedule_time if cfg else "02:00",
        "interval_hours": cfg.settings.schedule_interval_hours if cfg else 24,
    }

    status = {
        "next_run": _format_dt(service.next_run_at),
        "last_run": _format_dt(service.last_run_at),
        "last_run_status": service.last_run_status,
        "last_run_reason": service.last_run_reason,
        "last_run_duration": f"{service.last_run_duration:.1f}s" if service.last_run_duration else "-",
        "last_error": service.last_error or "",
        "running": service.running,
    }

    env_key_set = bool(os.getenv("HCF_MASTER_PASSWORD") or os.getenv("HCF_DATA_KEY_B64"))
    if env_key_set:
        data_key_source = "env"
    elif cfg:
        keyring_key = secrets.get_keyring_data_key(cfg.security.keyring_service, cfg.security.keyring_user)
        data_key_source = "keyring" if keyring_key else "missing"
    else:
        data_key_source = "missing"
    auto_trust = _env_bool("HCF_AUTO_TRUST_HOST_KEYS", False)

    results = list(service.last_results)
    results.sort(key=lambda item: item.get("device", ""))
    result_counts = Counter(item.get("status", "unknown") for item in results)

    devices = cfg.devices if cfg else []
    message = service.notice
    error = service.notice_error
    service.notice = None
    service.notice_error = False

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "config_exists": config_exists,
            "config_path": str(service.config_path),
            "schedule": schedule,
            "status": status,
            "devices": devices,
            "results": results,
            "result_counts": dict(result_counts),
            "auto_trust": auto_trust,
            "data_key_source": data_key_source,
            "message": message,
            "error": error,
        },
    )


@app.post("/schedule")
async def update_schedule(
    enabled: Optional[str] = Form(None),
    schedule_time: str = Form("02:00"),
    interval_hours: int = Form(24),
) -> RedirectResponse:
    if not service.config_path.exists():
        return _redirect_message("Config not found.", error=True)
    if service.lock.locked():
        return _redirect_message("Backup is running. Try again soon.", error=True)

    async with service.lock:
        cfg = config_mod.load_config(service.config_path)
        cfg.settings.schedule_enabled = enabled is not None
        cfg.settings.schedule_time = schedule_time.strip() or cfg.settings.schedule_time
        cfg.settings.schedule_interval_hours = max(1, int(interval_hours))
        config_mod.save_config(cfg, service.config_path)
    return _redirect_message("Schedule updated.")


@app.post("/run")
async def run_backup() -> RedirectResponse:
    if not service.trigger_backup(reason="manual"):
        return _redirect_message("Backup is already running.", error=True)
    return _redirect_message("Backup started.")


@app.get("/backups/{device_id}", response_class=HTMLResponse)
async def list_backups(request: Request, device_id: int) -> HTMLResponse:
    if not service.config_path.exists():
        return _redirect_message("Config not found.", error=True)

    cfg = config_mod.load_config(service.config_path)
    device = _find_device(cfg, device_id)
    device_name = device.name if device else None
    records = _load_backup_records(service.base_dir, device_id, device_name)
    for record in records:
        record["bytes_display"] = _format_bytes(record.get("bytes"))

    return templates.TemplateResponse(
        "backups.html",
        {
            "request": request,
            "device_id": device_id,
            "device_name": device_name or f"Device {device_id}",
            "records": records,
        },
    )


@app.get("/backups/{device_id}/{filename}")
async def download_backup(device_id: int, filename: str) -> FileResponse:
    _validate_filename(filename)
    if not service.config_path.exists():
        raise HTTPException(status_code=404, detail="Config not found.")

    cfg = config_mod.load_config(service.config_path)
    device = _find_device(cfg, device_id)
    device_name = device.name if device else None
    path, _record = _find_backup_entry(service.base_dir, device_id, device_name, filename)
    if not path:
        raise HTTPException(status_code=404, detail="Backup not found.")

    return FileResponse(path, filename=path.name, media_type="text/plain")


@app.get("/backups/{device_id}/{filename}/view", response_class=HTMLResponse)
async def view_backup(request: Request, device_id: int, filename: str) -> HTMLResponse:
    _validate_filename(filename)
    if not service.config_path.exists():
        raise HTTPException(status_code=404, detail="Config not found.")

    cfg = config_mod.load_config(service.config_path)
    device = _find_device(cfg, device_id)
    device_name = device.name if device else None
    path, record = _find_backup_entry(service.base_dir, device_id, device_name, filename)
    if not path:
        raise HTTPException(status_code=404, detail="Backup not found.")

    content = path.read_text(encoding="utf-8", errors="ignore")
    metadata = record or {"file": filename, "bytes": path.stat().st_size, "timestamp": "-", "hash": "-"}
    metadata["bytes_display"] = _format_bytes(metadata.get("bytes"))

    return templates.TemplateResponse(
        "backup_view.html",
        {
            "request": request,
            "device_id": device_id,
            "device_name": device_name or f"Device {device_id}",
            "metadata": metadata,
            "content": content,
        },
    )


@app.post("/devices/add")
async def add_device(
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    username: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    if not service.config_path.exists():
        return _redirect_message("Config not found.", error=True)
    if service.lock.locked():
        return _redirect_message("Backup is running. Try again soon.", error=True)

    async with service.lock:
        cfg = config_mod.load_config(service.config_path)
        try:
            data_key = _get_data_key(cfg)
        except Exception as exc:
            return _redirect_message(str(exc), error=True)
        next_id = max([d.id for d in cfg.devices], default=0) + 1
        enc_password = secrets.encrypt_secret(data_key, password)
        cfg.devices.append(
            Device(
                id=next_id,
                name=name.strip(),
                host=host.strip(),
                port=int(port),
                username=username.strip(),
                password_enc=enc_password,
            )
        )
        config_mod.save_config(cfg, service.config_path)

    return _redirect_message("Device added.")


@app.post("/devices/update")
async def update_device(
    device_id: int = Form(...),
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    username: str = Form(...),
    password: str = Form(""),
) -> RedirectResponse:
    if not service.config_path.exists():
        return _redirect_message("Config not found.", error=True)
    if service.lock.locked():
        return _redirect_message("Backup is running. Try again soon.", error=True)

    async with service.lock:
        cfg = config_mod.load_config(service.config_path)
        device = _find_device(cfg, device_id)
        if not device:
            return _redirect_message("Device not found.", error=True)

        device.name = name.strip()
        device.host = host.strip()
        device.port = int(port)
        device.username = username.strip()

        if password.strip():
            try:
                data_key = _get_data_key(cfg)
            except Exception as exc:
                return _redirect_message(str(exc), error=True)
            device.password_enc = secrets.encrypt_secret(data_key, password)

        config_mod.save_config(cfg, service.config_path)

    return _redirect_message("Device updated.")


@app.post("/devices/delete")
async def delete_device(device_id: int = Form(...)) -> RedirectResponse:
    if not service.config_path.exists():
        return _redirect_message("Config not found.", error=True)
    if service.lock.locked():
        return _redirect_message("Backup is running. Try again soon.", error=True)

    async with service.lock:
        cfg = config_mod.load_config(service.config_path)
        device = _find_device(cfg, device_id)
        if not device:
            return _redirect_message("Device not found.", error=True)

        cfg.devices = [d for d in cfg.devices if d.id != device_id]
        config_mod.save_config(cfg, service.config_path)

    return _redirect_message("Device removed.")


@app.get("/api/status")
async def api_status() -> JSONResponse:
    payload = {
        "running": service.running,
        "next_run": _format_dt(service.next_run_at),
        "last_run": _format_dt(service.last_run_at),
        "last_status": service.last_run_status,
        "last_error": service.last_error,
    }
    return JSONResponse(payload)


def main() -> None:
    host = os.getenv("HCF_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("HCF_WEB_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
