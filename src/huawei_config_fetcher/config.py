from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, Optional

import toml

from .models import Config, Device, SecurityConfig, Settings

DEFAULT_CONFIG_DIR = "data"
DEFAULT_CONFIG_FILE = "config.toml"
KEYRING_SERVICE = "huawei-config-fetcher"


def default_config_path(base_dir: Path | None = None) -> Path:
    root = base_dir or Path.cwd()
    return root / DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILE


def resolve_config_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return path
    env_path = os.getenv("HCF_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return default_config_path()


def ensure_config_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def keyring_user_for_path(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"config-{digest}"


def load_config(path: Path) -> Config:
    data = toml.load(path)
    security_data = data.get("security", {})
    settings_data = data.get("settings", {})
    known_hosts = data.get("known_hosts", {})

    security = SecurityConfig(
        kdf=security_data.get("kdf", "scrypt"),
        salt_b64=security_data.get("salt", ""),
        wrap_nonce_b64=security_data.get("wrap_nonce", ""),
        wrapped_key_b64=security_data.get("wrapped_key", ""),
        keyring_service=security_data.get("keyring_service", KEYRING_SERVICE),
        keyring_user=security_data.get("keyring_user", keyring_user_for_path(path)),
    )

    schedule_enabled = settings_data.get("schedule_enabled", False)
    if isinstance(schedule_enabled, str):
        schedule_enabled = schedule_enabled.strip().lower() in {"1", "true", "yes", "on"}

    schedule_time = settings_data.get("schedule_time", Settings.schedule_time)
    if not isinstance(schedule_time, str) or not schedule_time.strip():
        schedule_time = Settings.schedule_time

    schedule_interval = settings_data.get("schedule_interval_hours", Settings.schedule_interval_hours)
    try:
        schedule_interval = int(schedule_interval)
    except (TypeError, ValueError):
        schedule_interval = Settings.schedule_interval_hours

    settings = Settings(
        workers=int(settings_data.get("workers", 0)),
        schedule_enabled=bool(schedule_enabled),
        schedule_time=schedule_time,
        schedule_interval_hours=max(1, schedule_interval),
    )

    devices = []
    for item in data.get("devices", []):
        devices.append(
            Device(
                id=int(item.get("id", 0)),
                name=item.get("name", ""),
                host=item.get("host", ""),
                port=int(item.get("port", 22)),
                username=item.get("username"),
                password_enc=item.get("password"),
            )
        )

    return Config(
        version=int(data.get("version", 1)),
        security=security,
        settings=settings,
        devices=devices,
        known_hosts=known_hosts,
    )


def _config_to_dict(cfg: Config) -> Dict:
    return {
        "version": cfg.version,
        "security": {
            "kdf": cfg.security.kdf,
            "salt": cfg.security.salt_b64,
            "wrap_nonce": cfg.security.wrap_nonce_b64,
            "wrapped_key": cfg.security.wrapped_key_b64,
            "keyring_service": cfg.security.keyring_service,
            "keyring_user": cfg.security.keyring_user,
        },
        "settings": {
            "workers": cfg.settings.workers,
            "schedule_enabled": cfg.settings.schedule_enabled,
            "schedule_time": cfg.settings.schedule_time,
            "schedule_interval_hours": cfg.settings.schedule_interval_hours,
        },
        "known_hosts": cfg.known_hosts,
        "devices": [
            {
                "id": d.id,
                "name": d.name,
                "host": d.host,
                "port": d.port,
                "username": d.username,
                "password": d.password_enc,
            }
            for d in cfg.devices
        ],
    }


def save_config(cfg: Config, path: Path) -> None:
    ensure_config_dir(path)
    data = _config_to_dict(cfg)
    with path.open("w", encoding="utf-8") as f:
        toml.dump(data, f)
