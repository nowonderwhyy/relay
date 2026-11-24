# AGENTS.md

This repository is **Relay**, a standalone self-hosted household download relay powered by `aria2`, Flask, and Waitress.

## Setting Up Relay for a User's Environment

If the user asks you to set up, install, build, deploy, or configure Relay for their machine, homelab, or container stack:

1. **Read [`agents/SETUP_WORKFLOW.md`](agents/SETUP_WORKFLOW.md)** before making changes.
2. **Run `python3 agents/scripts/detect_env.py`** (or `python agents/scripts/detect_env.py` on Windows) to inspect the host environment safely.
3. Use [`agents/ENVIRONMENT_MATRIX.md`](agents/ENVIRONMENT_MATRIX.md) and the matching profile in `agents/profiles/` to generate a tailored configuration without hardcoding another machine's paths, IPs, or VPN profiles.
4. Run the checks in [`agents/VERIFICATION.md`](agents/VERIFICATION.md) before reporting completion.

## Architecture & Invariants

Do not weaken these properties when editing or configuring Relay:

- **Zero-log privacy**: `aria2c` runs with no log file; SQLite runs with `PRAGMA secure_delete = ON`; `Delete`, `Clear history`, and automatic retention remove files, uploaded `.torrent` files, and database rows together.
- **Per-user isolation**: Users only see and download jobs created under their own name (case-insensitive). Other users' job IDs return `404`.
- **Network gatekeeping**: Defaults to private/loopback clients only, blocks DNS rebinding via `Host` validation, requires `X-Relay: 1` on `POST` requests, and refuses links pointing at local/private addresses.
- **Private torrent safety**: Never append public fallback trackers when a `.torrent` contains `7:privatei1e`.
- **Honest VPN status**: Never report `VPN on` unless a real VPN check (`interface`, `external`, or `guard`) passed. Standalone `vpn_mode: "none"` displays `Direct`.
- **Secret hygiene**: Never commit `config.json`, `engine.json`, `.env`, WireGuard `.conf` / `.nmconnection` files, or `relay-data/`.

## Repository Map

- `relay/` — Python package (`config.py`, `store.py`, `engine.py`, `core.py`, `web.py`, `cli.py`, `server_engine.py`, `static/`)
- `deploy/linux/` — Parameterized Linux hardened server installer, systemd units, OpenRC init scripts, NetworkManager hook
- `deploy/build/` — PyInstaller standalone executable builder
- `Dockerfile`, `docker-compose.yml`, `docker-compose.gluetun.yml` — Container builds (standalone and Gluetun VPN)
- `agents/` — Agent-driven environment detection, setup playbooks, profiles, and verification checklists
- `tests/` — Unit and API test suite (`python -m unittest discover -s tests`)
