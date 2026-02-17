from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Device:
    id: int
    name: str
    host: str
    port: int = 22
    username: Optional[str] = None
    password_enc: Optional[str] = None


@dataclass
class SecurityConfig:
    kdf: str
    salt_b64: str
    wrap_nonce_b64: str
    wrapped_key_b64: str
    keyring_service: str
    keyring_user: str


@dataclass
class Settings:
    workers: int = 0
    schedule_enabled: bool = False
    schedule_time: str = "02:00"
    schedule_interval_hours: int = 24


@dataclass
class Config:
    version: int
    security: SecurityConfig
    settings: Settings
    devices: List[Device] = field(default_factory=list)
    known_hosts: Dict[str, str] = field(default_factory=dict)
