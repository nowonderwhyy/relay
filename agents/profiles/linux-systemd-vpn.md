# Profile: Hardened Linux Server (systemd + VPN Kill-Switch)

Use this profile on a dedicated Debian, Ubuntu, Fedora, or Arch Linux home server with **systemd**, `nftables`, and a WireGuard VPN tunnel (`wg0` or similar).

## Security Architecture

- `relaydl`: Unprivileged download account running `aria2c` only.
- `relayweb`: Unprivileged web account running Flask + Waitress, granted a single `NOPASSWD` sudo rule to `/usr/local/sbin/relay-engine`.
- `nftables` table `inet relay_guard`: Loaded before the network comes up (`relay-guard.service`). Blocks all outbound non-VPN traffic and all new loopback connections from `relaydl`, so if the VPN drops, downloads stall instead of leaking onto the home ISP connection.

## 1. Install System Dependencies

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y --no-install-recommends aria2 nftables iproute2 python3 python3-venv wireguard-tools

# Arch
sudo pacman -S --needed aria2 nftables iproute2 python wireguard-tools

# Fedora
sudo dnf install -y aria2 nftables iproute python3 wireguard-tools
```

## 2. Run the Parameterized Installer

Set environment variables to match the user's machine from `detect_env.py`:

```bash
sudo \
  RELAY_STAGING_DIR="/srv/storage/relay-downloads" \
  RELAY_DATA_DIR="/var/lib/relay/data" \
  RELAY_PORT="80" \
  RELAY_HOST_LABEL="Home Server" \
  RELAY_VPN_INTERFACE="wg0" \
  RELAY_WIREGUARD_BACKEND="wg-quick" \
  RELAY_WG_QUICK_CONFIG="/etc/wireguard/wg0.conf" \
  RELAY_VPN_DNS="10.2.0.1" \
  RELAY_NAT_PMP_GATEWAY="10.2.0.1" \
  RELAY_REQUIRE_IPV6="false" \
  bash deploy/linux/install.sh
```

Adjust per VPN provider (see `agents/ENVIRONMENT_MATRIX.md`):
- **Proton VPN**: `RELAY_VPN_DNS="10.2.0.1"`, `RELAY_NAT_PMP_GATEWAY="10.2.0.1"` (enables automatic NAT-PMP port forwarding).
- **Mullvad / IVPN / Custom WireGuard**: Leave `RELAY_NAT_PMP_GATEWAY=""` empty; set `RELAY_VPN_DNS` to your tunnel DNS (e.g. `10.64.0.1` for Mullvad) or leave empty.
- **NetworkManager WireGuard**: Set `RELAY_WIREGUARD_BACKEND="networkmanager"`, `RELAY_NM_PROFILE="/etc/NetworkManager/system-connections/<name>.nmconnection"`, and `RELAY_NM_CONNECTION_ID="<name>"`.

## 3. Optional: Multi-Location VPN Exit Switcher

Drop standard WireGuard `.conf` profiles (mode `0600`, owned by `root:root`) into `/etc/relay/locations/`:

```text
/etc/relay/locations/
├── united-states.conf
├── switzerland.conf
└── netherlands.conf
```

When 1 or more `.conf` files exist there and `wireguard_backend` is `wg-quick` or `networkmanager`, the **VPN exit** dropdown appears automatically in the web UI footer. Optionally add friendly display names under `"location_labels"` in `/etc/relay/config.json`.

## 4. Optional Household Password

Tell the user to run interactively on the server:

```bash
sudo RELAY_CONFIG=/etc/relay/config.json RELAY_CONFIG_GROUP=relayweb /usr/local/bin/relay passwd
```

## 5. Service Management & Logs

```bash
sudo systemctl status relay-guard relay-web
sudo journalctl -u relay-web -f
sudo /usr/local/sbin/relay-engine health
```
