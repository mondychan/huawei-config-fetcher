# huawei-config-fetcher

Interactive CLI tool to fetch current configuration from Huawei L3 routers/switches
over SSH and store versioned backups locally.

Status: bootstrapping.

## Goals
- Cross-platform CLI (Windows Terminal + Linux bash)
- Read-only SSH sessions; no config changes
- Device list in a config file; batch backups across multiple devices
- Backups named with timestamp + content hash for revision tracking
- Credentials never stored in plaintext

## Safety
This tool must only run read-only commands on devices. The intended command set
is limited to configuration export (for example: `display current-configuration`)
and safe pager handling (`screen-length 0 temporary`). No destructive commands
are ever sent.

## How secrets work
- On first run, a random master password is generated and shown once.
- A data key encrypts device passwords; the data key is wrapped by the master
  password and stored in `config.toml`.
- The data key is also stored in the OS keyring so you do not need to type the
  master password on the same machine.
- If you move the project to another machine, you can unlock it by entering the
  master password once, then the new keyring stores it locally.

## Usage
Run `hcf` without arguments to open the interactive menu. It will create a
config automatically if none exists (and show the master password once).

Commands:
- `hcf init-config` - create `data/config.toml` and generate a master password
- `hcf add-device` - add a device interactively
- `hcf backup` - run batch backup for one or more devices
- `hcf list-devices` - list configured devices
- `hcf show-backups` - list stored revisions
- `hcf reset-config` - reset master password and clear stored credentials

## Layout
- `src/huawei_config_fetcher/` - CLI, SSH, config, crypto, backup logic
- `data/config.toml` - local config (ignored by git)
- `data/config.example.toml` - example config to share
- `backups/<device-id>-<name>/YYYYMMDD-HHMMSS_<hash>.cfg` - stored configs
- `backups/<device-id>-<name>/manifest.json` - metadata per device
- `backups/backup.log` - latest backup run log (rotates to `backup.log.old`)
- `backups/backup_state.json` - last run status + results (used by web UI)

## Quick start (Windows PowerShell)
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
hcf
```

## Quick start (Linux/macOS)
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
hcf
```

## Docker deployment (web UI + scheduler)
This runs the web UI and scheduler in a single container. Bind mount a folder
for config and backups. The UI lets you add/edit/remove devices, set the schedule,
trigger backups, and view/download saved configurations.

Build and run:
```bash
docker build -f docker/Dockerfile -t hcf:latest .
docker run --rm -p 8080:8080 \
  -v /path/to/hcf-data:/data \
  -e HCF_MASTER_PASSWORD="REPLACE_ME" \
  -e HCF_AUTO_TRUST_HOST_KEYS="true" \
  hcf:latest
```

Quick start (run prebuilt image):
```bash
docker run -d --name hcf -p 8080:8080 \
  -v /path/to/hcf-data:/data \
  -e HCF_MASTER_PASSWORD="REPLACE_ME" \
  -e HCF_AUTO_TRUST_HOST_KEYS="true" \
  --restart unless-stopped \
  mondychan/hcf:latest
```

Or with compose:
```bash
docker compose -f docker/compose.yaml up --build
```

Open the UI at `http://<host>:8080`.

Notes:
- Config path defaults to `/data/config.toml` inside the container.
- Backups are stored in `/data/backups`.
- `HCF_MASTER_PASSWORD` or `HCF_DATA_KEY_B64` is required for non-interactive runs.
- Set `HCF_WEB_PORT` to change the listen port.
- Set `HCF_WEB_HOST` to change the listen host (default `0.0.0.0`).
- Set `HCF_CONFIG_PATH` to override the config location.
- `HCF_AUTO_TRUST_HOST_KEYS=true` will accept new/changed SSH host keys automatically.
- Scheduler settings are stored in `config.toml` under `[settings]`.
- Backups with suspiciously short output are retried once and rejected if still incomplete.
- Optional: place `brockmann-medium.otf` in `/data/fonts` (or set `HCF_FONTS_DIR`) to load the brand font.

Local UI (no Docker):
```bash
pip install -e .[web]
hcf-web
```

## Build executables
Windows (builds `dist\hcf.exe`):
```powershell
pip install pyinstaller
pyinstaller --onefile --name hcf src\huawei_config_fetcher\cli.py
```

Linux (build in Docker, outputs `dist/hcf`):
```powershell
docker run --rm -v "${PWD}:/app" -w /app python:3.11 \
  bash -lc "pip install -U pip && pip install -e . pyinstaller && \
  pyinstaller --onefile --name hcf src/huawei_config_fetcher/cli.py"
```

Run the Linux binary:
```bash
chmod +x dist/hcf
./dist/hcf
```

## Host key policy
- On first connect, the tool shows the SSH host key fingerprint and asks to trust it.
- If the host key changes later, you get a warning and can accept or skip.
- In `--non-interactive` mode, unknown/changed host keys are skipped.
