# Profile: Standalone Desktop / Portable (Windows, macOS, Linux)

Use this profile when the user wants to build and run Relay directly on their own computer or a single-user machine without root privileges, Docker, or a system-wide firewall guard.

## 1. Prerequisites

Ensure Python >= 3.10 and `aria2c` are installed:

- **Windows**: `winget install aria2.aria2` (or `choco install aria2`)
- **macOS**: `brew install aria2`
- **Debian / Ubuntu**: `sudo apt install aria2`
- **Arch**: `sudo pacman -S aria2`
- **Fedora**: `sudo dnf install aria2`

## 2. Install or Build Relay

### Option A — Python virtual environment (fastest)

```bash
# Using uv
uv venv
uv pip install -e .

# Or standard venv + pip
python3 -m venv .venv
.venv/bin/pip install -e .        # Linux / macOS
.venv\Scripts\pip install -e .    # Windows
```

### Option B — Compiled Standalone Binary (PyInstaller)

```bash
pip install -e ".[build]"
python deploy/build/build_standalone.py --bundle-aria2
```

Output: `dist/relay` (Linux/macOS) or `dist/relay.exe` (Windows) — a single self-contained executable with Flask, Waitress, static UI assets, and `aria2c` baked in.

## 3. Initialize Configuration

### Portable folder (stores config, DB, and downloads in `./relay-data`)

```bash
relay init --portable --port 8080 --host-label "My PC"
relay serve --portable --open
```

### OS-native user directories (`%LOCALAPPDATA%\Relay`, `~/Library/Application Support/Relay`, or `~/.config/relay`)

```bash
relay init --port 8080 --host-label "Home" --staging "/path/to/staging"
relay serve --open
```

### Optional: Require a local VPN interface

If the user runs WireGuard or OpenVPN on this machine and wants Relay to pause whenever the VPN interface drops:

```bash
relay init --vpn-mode interface --vpn-interface wg0 --port 8080
```

> Note: On Windows, pass the Windows network adapter name shown by `netsh interface show interface` (or `detect_env.py`). On macOS, pass the active `utunX` interface. For a strict kernel-level kill-switch, prefer `linux-systemd-vpn.md`, `linux-openrc-vpn.md`, or `docker-gluetun.md`.

## 4. Optional Auto-Start

- **macOS (`launchd`)**: Create `~/Library/LaunchAgents/com.relay.dl.plist` invoking `<venv>/bin/relay serve` with `RunAtLoad` and `KeepAlive`.
- **Windows (Task Scheduler / Startup)**: Add a Scheduled Task running `relay.exe serve` (or `<venv>\Scripts\pythonw.exe -m relay.cli serve`) at logon, and allow inbound TCP port `8080` on the **Private** Windows Firewall profile if LAN devices should reach it.
- **Linux user service**: Create `~/.config/systemd/user/relay.service` running `<venv>/bin/relay serve` and enable with `systemctl --user enable --now relay`.
