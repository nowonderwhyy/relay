# Profile: Hardened Linux Server (OpenRC + VPN Kill-Switch)

Use this profile on Alpine Linux, Gentoo, Artix, or Debian systems running **OpenRC** (`rc-service`, `supervise-daemon`), `nftables`, and WireGuard (via NetworkManager or `wg-quick`).

## 1. Install System Dependencies

```bash
# Debian (OpenRC)
sudo apt update && sudo apt install -y --no-install-recommends aria2 nftables iproute2 python3 python3-venv

# Alpine
sudo apk add aria2 nftables iproute2 python3 py3-pip sudo bash
```

## 2. Run the Parameterized Installer

`deploy/linux/install.sh` automatically detects OpenRC (`rc-service`) and installs `/etc/init.d/relay-guard`, `/etc/init.d/relay-web`, and (if NetworkManager is installed) `/etc/NetworkManager/dispatcher.d/90-relay-vpn`:

```bash
sudo \
  RELAY_STAGING_DIR="/srv/storage/relay-downloads" \
  RELAY_DATA_DIR="/var/lib/relay/data" \
  RELAY_PORT="80" \
  RELAY_HOST_LABEL="Home Server" \
  RELAY_VPN_INTERFACE="wg0" \
  RELAY_WIREGUARD_BACKEND="networkmanager" \
  RELAY_NM_PROFILE="/etc/NetworkManager/system-connections/<profile>.nmconnection" \
  RELAY_NM_CONNECTION_ID="<profile>" \
  RELAY_VPN_DNS="10.2.0.1" \
  RELAY_NAT_PMP_GATEWAY="10.2.0.1" \
  bash deploy/linux/install.sh
```

> **Note**: If `port` is `< 1024` (such as `80`), `/etc/init.d/relay-web` uses `supervise-daemon` with `capabilities="^cap_net_bind_service"` so `relayweb` can bind port 80 without running as root.

## 3. Optional: Multi-Location VPN Exit Switcher

Place standard WireGuard `.conf` files (`0600 root:root`) in `/etc/relay/locations/`. Relay automatically detects them, surfaces the **VPN exit** dropdown in the UI, keeps rollback backups in `/var/backups/relay-vpn`, re-pins the `nftables` guard before switching, and resumes active downloads automatically.

## 4. Service Management & Logs

```bash
sudo rc-service relay-guard status
sudo rc-service relay-web status
sudo tail -f /var/log/relay-web.log
sudo /usr/local/sbin/relay-engine health
```
