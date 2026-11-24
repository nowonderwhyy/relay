# Agent-Driven Setup (`agents/`)

Every user's environment is different: OS, init system, VPN provider, storage drive, reverse proxy, and LAN layout vary widely. This folder lets any AI coding agent (**Claude Code**, **Codex**, **Antigravity**, **Cursor**, **Windsurf**, etc.) inspect your machine and configure Relay cleanly for your exact setup.

## For Humans: How to Use This

Clone the repo, open it in your coding agent, and ask something like:

> "Read `agents/SETUP_WORKFLOW.md` and set up Relay for this machine."

Or be specific about your goal:

- *"Set up Relay as a standalone local app on my Windows/Mac/Linux laptop."*
- *"Configure Relay on this Debian server with systemd, Proton VPN WireGuard on `wg0`, and the nftables kill-switch, storing downloads on `/mnt/data/relay`."*
- *"Deploy Relay with Docker Compose routed through Gluetun using my Mullvad WireGuard credentials."*
- *"Put Relay behind Caddy / Tailscale Serve so my phone can reach it securely."*

## Contents

| Path | Purpose |
| --- | --- |
| [`SETUP_WORKFLOW.md`](SETUP_WORKFLOW.md) | Master 5-phase workflow: inspect -> clarify -> configure -> build/install -> verify |
| [`ENVIRONMENT_MATRIX.md`](ENVIRONMENT_MATRIX.md) | Decision tables for modes, VPN providers, OS quirks, storage, and reverse proxies |
| [`VERIFICATION.md`](VERIFICATION.md) | Automated tests, security gate checks, and VPN kill-switch leak tests |
| [`scripts/detect_env.py`](scripts/detect_env.py) | Zero-dependency host environment probe (JSON output; never reads secrets) |
| [`profiles/standalone-desktop.md`](profiles/standalone-desktop.md) | Windows / macOS / Linux local or LAN standalone run & PyInstaller binary build |
| [`profiles/linux-systemd-vpn.md`](profiles/linux-systemd-vpn.md) | Hardened Linux home server: systemd + WireGuard + nftables kill-switch |
| [`profiles/linux-openrc-vpn.md`](profiles/linux-openrc-vpn.md) | Hardened Linux home server: OpenRC + WireGuard + nftables kill-switch |
| [`profiles/docker-gluetun.md`](profiles/docker-gluetun.md) | Docker Compose standalone or Gluetun VPN container stack |
| [`profiles/reverse-proxy-tailscale.md`](profiles/reverse-proxy-tailscale.md) | Exposing Relay safely over Caddy, Nginx, or Tailscale Serve |
