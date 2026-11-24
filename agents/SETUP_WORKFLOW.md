# Relay Agent Setup Workflow

Follow this 5-phase workflow whenever a user asks you to install, build, or configure Relay for their environment.

---

## Phase 1 — Inspect the Environment

Run the zero-dependency probe script first:

```bash
python3 agents/scripts/detect_env.py    # Linux / macOS
python agents/scripts/detect_env.py     # Windows
```

Read the JSON output to determine:
- OS, distro, architecture, and init system (`systemd`, `openrc`, `launchd`, `windows`)
- Whether `aria2c`, `uv`, `docker`, `nft`, `nmcli`, `wg`, `tailscale`, `caddy`, `nginx` are installed
- Existing VPN interfaces (`wg0`, `tun0`, `utun*`, etc.)
- Candidate storage mounts and free space
- Available ports (`80`, `8080`, `16874`)

> **Privacy rule**: Never inspect or print WireGuard private keys, `.nmconnection` secrets, `.env` credentials, or existing `config.json` `secret_key`/`password_hash` values into chat.

---

## Phase 2 — Clarify User Preferences

Ask the user **only** the questions that are not already clear from their prompt or `detect_env.py`:

1. **Deployment target**:
   - Standalone local/LAN app (`relay serve` or compiled binary)
   - Docker / Docker + Gluetun VPN container
   - Hardened Linux server (dedicated `relaydl` user + `nftables` kill-switch + `systemd`/`OpenRC`)
2. **VPN requirement**:
   - `none` (direct connection — fastest standalone setup; UI displays `Direct`)
   - `interface` (pause downloads unless a specific VPN interface like `wg0`/`tun0` is up)
   - `external` (container routed through Gluetun or another VPN container)
   - `guard` (hardened Linux server with UID-scoped `nftables` VPN-only kill-switch)
   - Optional: VPN provider / NAT-PMP port forwarding (e.g., Proton VPN `10.2.0.1`) and multi-exit WireGuard location switching
3. **Storage & retention**:
   - Where to store active/finished staging files (`staging_dir`, ideally on a large drive)
   - Retention window (`keep_days`, default `7`)
4. **Network & access**:
   - Listen port (`8080` unprivileged, or `80` on a dedicated LAN box)
   - Display label (`host_label`, e.g., `Home Server`, `NAS`, `Laptop`)
   - Optional household password (yes/no; if yes, tell the user to run `relay passwd` interactively rather than pasting the password in chat!)
   - Reverse proxy or Tailscale access (`allow_cgnat`, `allowed_hosts`, `trusted_proxies`)

---

## Phase 3 — Select Profile & Generate Configuration

Consult [`ENVIRONMENT_MATRIX.md`](ENVIRONMENT_MATRIX.md) and open the matching profile under `agents/profiles/`:

- **Windows / macOS / Linux desktop or portable standalone**: [`profiles/standalone-desktop.md`](profiles/standalone-desktop.md)
- **Linux server with systemd + VPN kill-switch**: [`profiles/linux-systemd-vpn.md`](profiles/linux-systemd-vpn.md)
- **Linux server with OpenRC + VPN kill-switch**: [`profiles/linux-openrc-vpn.md`](profiles/linux-openrc-vpn.md)
- **Docker / Gluetun VPN stack**: [`profiles/docker-gluetun.md`](profiles/docker-gluetun.md)
- **Behind Caddy, Nginx, or Tailscale Serve**: [`profiles/reverse-proxy-tailscale.md`](profiles/reverse-proxy-tailscale.md)

Use `relay init` (for standalone) or the parameterized `deploy/linux/install.sh` (for hardened Linux) or `.env` + `docker-compose*.yml` (for Docker) to generate the environment-specific configuration cleanly.

---

## Phase 4 — Install / Build / Start

1. Install missing system prerequisites (`aria2` is the only external binary required for standalone mode; Linux guarded mode also needs `nftables` and `iproute2`).
2. Install or build Relay:
   - Python package: `uv pip install -e .` or `pip install .`
   - Standalone binary (if requested): `python deploy/build/build_standalone.py --bundle-aria2`
   - Docker: `docker compose up -d --build` (or `-f docker-compose.gluetun.yml`)
   - Hardened Linux: `sudo ... bash deploy/linux/install.sh`
3. If the user wants a household password, give them the exact `relay passwd` command to run locally (never ask them to paste passwords into chat).

---

## Phase 5 — Verify Before Finishing

Execute every applicable check in [`VERIFICATION.md`](VERIFICATION.md):

1. Run `relay doctor` and confirm `python`, `aria2c`, `staging_dir`, and `vpn` checks pass.
2. Run the unit test suite (`python -m unittest discover -s tests`).
3. Verify HTTP gatekeeping (`/healthz`, `/api/me`, DNS-rebinding rejection, CSRF header enforcement).
4. If VPN kill-switch (`guard` or `gluetun`) is enabled, run the VPN egress and kill-switch drop verification.
5. Report the exact LAN URL, config file paths, service management commands, and any manual follow-up steps (such as running `relay passwd` or dropping WireGuard `.conf` files into `/etc/relay/locations/`).
