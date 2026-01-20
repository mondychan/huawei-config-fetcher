from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict

import toml

from .models import Config, Device, SecurityConfig, Settings

DEFAULT_CONFIG_DIR = "data"
DEFAULT_CONFIG_FILE = "config.toml"
KEYRING_SERVICE = "huawei-config-fetcher"


def default_config_path(base_dir: Path | None = None) -> Path:
    root = base_dir or Path.cwd()
    return root / DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILE


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

    settings = Settings(
        workers=int(settings_data.get("workers", 0)),
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
