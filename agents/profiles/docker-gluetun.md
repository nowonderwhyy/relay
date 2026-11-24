# Profile: Docker & Docker + Gluetun VPN

Use this profile on any host with Docker and Docker Compose (Linux, NAS appliances like Synology/TrueNAS/Unraid, macOS, or Windows).

## Option A — Standalone Container (No Built-In VPN)

```bash
docker compose up -d --build
```

Open `http://<host-ip>:8080/`. Persistent config, SQLite ledger, and staging files live under `./relay-data/`.

Customize via `.env` (git-ignored):

```dotenv
RELAY_PORT=8080
RELAY_HOST_LABEL=NAS
RELAY_KEEP_DAYS=7
RELAY_ALLOW_CGNAT=false
RELAY_ALLOWED_HOSTS=
```

## Option B — VPN-Guarded Container Stack (Gluetun)

`docker-compose.gluetun.yml` runs Relay inside Gluetun's network namespace (`network_mode: "service:gluetun"`). Gluetun enforces an iptables firewall kill-switch inside the container network, and Relay monitors Gluetun's local HTTP control server (`http://127.0.0.1:8000/v1/publicip/ip`) so the UI accurately reflects live VPN status.

### 1. Create `.env` (never commit this file!)

Ask the user to create `.env` locally (or populate non-secret fields and have the user paste their WireGuard private key directly into `.env`):

```dotenv
RELAY_PORT=8080
RELAY_HOST_LABEL=Home VPN
RELAY_KEEP_DAYS=7

# Example: Proton VPN WireGuard (see https://github.com/qdm12/gluetun-wiki for other providers)
VPN_SERVICE_PROVIDER=protonvpn
VPN_TYPE=wireguard
WIREGUARD_PRIVATE_KEY=REPLACE_WITH_YOUR_PRIVATE_KEY
WIREGUARD_ADDRESSES=10.2.0.2/32
SERVER_COUNTRIES=Switzerland
VPN_PORT_FORWARDING=on
```

Supported providers through Gluetun include Proton VPN, Mullvad, AirVPN, IVPN, PIA, Surfshark, NordVPN, Windscribe, and custom WireGuard/OpenVPN configs.

### 2. Build and Start

```bash
docker compose -f docker-compose.gluetun.yml up -d --build
```

### 3. Optional Household Password

```bash
docker exec -it relay relay passwd
```

### 4. Verify VPN Egress

Follow Section 5 of [`../VERIFICATION.md`](../VERIFICATION.md) to confirm the container egresses through the VPN IP and that `/healthz` reports `"vpn": true`.
