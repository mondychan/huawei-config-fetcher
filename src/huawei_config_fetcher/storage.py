from __future__ import annotations

import json
import re
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Dict, List


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "device"


def device_dir(base_dir: Path, device_id: int, name: str) -> Path:
    slug = slugify(name)
    return base_dir / f"{device_id:03d}-{slug}"


def compute_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def backup_filename(timestamp: str, content_hash: str) -> str:
    return f"{timestamp}_{content_hash[:8]}.cfg"


def write_backup(
    base_dir: Path,
    device_id: int,
    device_name: str,
    content: str,
) -> Dict:
    device_path = device_dir(base_dir, device_id, device_name)
    device_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    content_hash = compute_hash(content)
    filename = backup_filename(timestamp, content_hash)
    backup_path = device_path / filename
    backup_path.write_text(content, encoding="utf-8")

    record = {
        "timestamp": timestamp,
        "hash": content_hash,
        "file": str(backup_path.relative_to(device_path)),
        "bytes": backup_path.stat().st_size,
    }

    manifest_path = device_path / "manifest.json"
    records = load_manifest(manifest_path)
    records.append(record)
    save_manifest(manifest_path, records)

    return record


def load_manifest(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, records: List[Dict]) -> None:
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
