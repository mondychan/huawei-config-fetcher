from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.prompt import Prompt
from rich.table import Table

from huawei_config_fetcher import backup as backup_mod
from huawei_config_fetcher import config as config_mod
from huawei_config_fetcher import secrets
from huawei_config_fetcher import ssh_client
from huawei_config_fetcher.models import Config, Device, SecurityConfig, Settings

app = typer.Typer(add_completion=False, no_args_is_help=False)
console = Console()


def _load_config(path: Path) -> Config:
    if not path.exists():
        console.print(f"Config not found: {path}")
        console.print("Initialize now.")
        init_config(config_path=path)
    return config_mod.load_config(path)


def _get_data_key(cfg: Config, config_path: Path, non_interactive: bool) -> bytes:
    data_key = secrets.get_keyring_data_key(cfg.security.keyring_service, cfg.security.keyring_user)
    if data_key:
        return data_key

    if non_interactive:
        raise typer.BadParameter("Keyring missing and non-interactive mode is enabled.")

    master = typer.prompt("Master password", hide_input=True)
    try:
        data_key = secrets.unwrap_data_key(
            master,
            cfg.security.salt_b64,
            cfg.security.wrap_nonce_b64,
            cfg.security.wrapped_key_b64,
        )
    except Exception as exc:
        raise typer.BadParameter("Invalid master password.") from exc

    secrets.set_keyring_data_key(cfg.security.keyring_service, cfg.security.keyring_user, data_key)
    return data_key


def _backup_dir(base_dir: Optional[Path] = None) -> Path:
    root = base_dir or Path.cwd()
    return root / "backups"

def _prompt_device_ids() -> Optional[List[int]]:
    raw = Prompt.ask("Device IDs (comma separated, empty = all)", default="")
    if not raw.strip():
        return None
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    ids: List[int] = []
    for part in parts:
        try:
            ids.append(int(part))
        except ValueError:
            console.print(f"Invalid device id: {part}")
            return None
    return ids


def _interactive_menu(config_path: Optional[Path]) -> None:
    while True:
        console.print("\nSelect an action:")
        console.print("1) init-config")
        console.print("2) add-device")
        console.print("3) list-devices")
        console.print("4) backup")
        console.print("5) show-backups")
        console.print("6) reset-config")
        console.print("7) exit")
        choice = Prompt.ask("Choice", choices=[str(i) for i in range(1, 8)])

        try:
            if choice == "1":
                init_config(config_path=config_path)
            elif choice == "2":
                add_device(config_path=config_path)
            elif choice == "3":
                list_devices(config_path=config_path)
            elif choice == "4":
                ids = _prompt_device_ids()
                backup_configs(config_path=config_path, device_id=ids, non_interactive=False)
            elif choice == "5":
                ids = _prompt_device_ids()
                show_backups(config_path=config_path, device_id=ids)
            elif choice == "6":
                reset_config(config_path=config_path)
            elif choice == "7":
                break
        except typer.Exit:
            pass


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Show menu when no command is provided"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    if ctx.invoked_subcommand is None and interactive:
        _interactive_menu(config_path)


@app.command("init-config")
def init_config(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    path = config_path or config_mod.default_config_path()
    if path.exists():
        console.print(f"Config already exists at {path}")
        raise typer.Exit(code=1)

    master_password = secrets.generate_master_password()
    data_key = secrets.generate_data_key()
    wrapped = secrets.wrap_data_key(master_password, data_key)
    keyring_user = config_mod.keyring_user_for_path(path)

    security = SecurityConfig(
        kdf=wrapped["kdf"],
        salt_b64=wrapped["salt_b64"],
        wrap_nonce_b64=wrapped["wrap_nonce_b64"],
        wrapped_key_b64=wrapped["wrapped_key_b64"],
        keyring_service=config_mod.KEYRING_SERVICE,
        keyring_user=keyring_user,
    )

    cfg = Config(version=1, security=security, settings=Settings(), devices=[], known_hosts={})
    config_mod.save_config(cfg, path)

    secrets.set_keyring_data_key(security.keyring_service, security.keyring_user, data_key)

    console.print("Config created.")
    console.print("Master password (store this safely, it is shown once):")
    console.print(master_password)


@app.command("reset-config")
def reset_config(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    path = config_path or config_mod.default_config_path()
    cfg = _load_config(path)

    if not typer.confirm("Reset master password and clear stored credentials?"):
        raise typer.Exit()

    master_password = secrets.generate_master_password()
    data_key = secrets.generate_data_key()
    wrapped = secrets.wrap_data_key(master_password, data_key)

    cfg.security.kdf = wrapped["kdf"]
    cfg.security.salt_b64 = wrapped["salt_b64"]
    cfg.security.wrap_nonce_b64 = wrapped["wrap_nonce_b64"]
    cfg.security.wrapped_key_b64 = wrapped["wrapped_key_b64"]

    for device in cfg.devices:
        device.username = None
        device.password_enc = None

    config_mod.save_config(cfg, path)
    secrets.clear_keyring_data_key(cfg.security.keyring_service, cfg.security.keyring_user)
    secrets.set_keyring_data_key(cfg.security.keyring_service, cfg.security.keyring_user, data_key)

    console.print("Config reset.")
    console.print("New master password (store this safely, it is shown once):")
    console.print(master_password)


@app.command("add-device")
def add_device(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    path = config_path or config_mod.default_config_path()
    cfg = _load_config(path)
    data_key = _get_data_key(cfg, path, non_interactive=False)

    name = typer.prompt("Device name")
    host = typer.prompt("Device host or IP")
    port = int(typer.prompt("SSH port", default="22"))
    username = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)

    next_id = max([d.id for d in cfg.devices], default=0) + 1
    enc_password = secrets.encrypt_secret(data_key, password)

    cfg.devices.append(
        Device(
            id=next_id,
            name=name,
            host=host,
            port=port,
            username=username,
            password_enc=enc_password,
        )
    )
    config_mod.save_config(cfg, path)
    console.print(f"Added device {name} with id {next_id}.")


@app.command("list-devices")
def list_devices(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
) -> None:
    path = config_path or config_mod.default_config_path()
    cfg = _load_config(path)

    table = Table(title="Devices")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Host")
    table.add_column("Port", justify="right")
    table.add_column("Username")

    for d in cfg.devices:
        table.add_row(str(d.id), d.name, d.host, str(d.port), d.username or "-")

    console.print(table)


@app.command("backup")
def backup_configs(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
    device_id: Optional[List[int]] = typer.Option(None, "--device-id", help="Device id(s) to back up"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Fail or skip prompts"),
) -> None:
    path = config_path or config_mod.default_config_path()
    cfg = _load_config(path)
    data_key = _get_data_key(cfg, path, non_interactive=non_interactive)

    devices = cfg.devices
    if device_id:
        devices = [d for d in cfg.devices if d.id in device_id]

    def prompt_host_key(host_id: str, new_fp: str, old_fp: Optional[str]) -> bool:
        if old_fp is None:
            prompt = f"Unknown host key for {host_id}: {new_fp}. Trust and continue?"
        else:
            prompt = (
                f"Host key changed for {host_id}.\n"
                f"Old: {old_fp}\nNew: {new_fp}\n"
                "Continue and trust new key?"
            )
        return typer.confirm(prompt)

    preflight_results = []
    approved_devices: List[Device] = []
    pending_prompts = []

    if devices:
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Checking host keys", total=len(devices))

            for device in devices:
                progress.update(task_id, description=f"Host key: {device.name}")
                fingerprint, error = ssh_client.fetch_host_fingerprint(device)
                host_id = f"{device.host}:{device.port}"
                if error:
                    preflight_results.append(
                        {"device": device.name, "status": "error", "reason": error}
                    )
                    progress.update(task_id, advance=1)
                    continue

                known_fp = cfg.known_hosts.get(host_id)
                if known_fp == fingerprint:
                    approved_devices.append(device)
                else:
                    pending_prompts.append((device, host_id, fingerprint, known_fp))
                progress.update(task_id, advance=1)

            progress.update(task_id, description="Host key check complete")

    if non_interactive:
        for device, _host_id, _fp, _known in pending_prompts:
            preflight_results.append(
                {"device": device.name, "status": "skipped", "reason": "host-key"}
            )
    else:
        for device, host_id, fp, known_fp in pending_prompts:
            if prompt_host_key(host_id, fp, known_fp):
                cfg.known_hosts[host_id] = fp
                approved_devices.append(device)
            else:
                preflight_results.append(
                    {"device": device.name, "status": "skipped", "reason": "host-key"}
                )

    base_dir = _backup_dir()
    results = []
    if approved_devices:
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Backing up devices", total=len(approved_devices))

            def on_result(item: dict) -> None:
                progress.update(task_id, advance=1, description=f"Backup: {item.get('device', '-')}")

            results = backup_mod.run_backup(
                cfg, data_key, base_dir, approved_devices, lambda *_: False, on_result=on_result
            )
            progress.update(task_id, description="Backup complete")

    config_mod.save_config(cfg, path)

    table = Table(title="Backup results")
    table.add_column("Device")
    table.add_column("Status")
    table.add_column("Detail")

    for item in preflight_results + results:
        status = item.get("status", "-")
        detail = item.get("hash") or item.get("reason") or "-"
        table.add_row(item.get("device", "-"), status, detail)

    console.print(table)


@app.command("show-backups")
def show_backups(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
    device_id: Optional[List[int]] = typer.Option(None, "--device-id", help="Device id(s)"),
) -> None:
    path = config_path or config_mod.default_config_path()
    cfg = _load_config(path)

    from huawei_config_fetcher import storage

    base_dir = _backup_dir()
    devices = cfg.devices
    if device_id:
        devices = [d for d in cfg.devices if d.id in device_id]

    for d in devices:
        device_path = storage.device_dir(base_dir, d.id, d.name)
        manifest_path = device_path / "manifest.json"
        records = storage.load_manifest(manifest_path)

        table = Table(title=f"Backups for {d.name} ({d.id})")
        table.add_column("Timestamp")
        table.add_column("Hash")
        table.add_column("Bytes", justify="right")

        for record in records:
            table.add_row(record.get("timestamp", "-"), record.get("hash", "-")[:8], str(record.get("bytes", 0)))

        console.print(table)


if __name__ == "__main__":
    app()
