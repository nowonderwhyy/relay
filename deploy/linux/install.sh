#!/usr/bin/env bash
# Parameterized, idempotent installer for hardened Linux VPN-guarded Relay servers.
# Supports both systemd and OpenRC. Safe to re-run for updates.
#
# Environment variables (all optional; sensible defaults shown):
#   RELAY_STAGING_DIR=/var/lib/relay/staging
#   RELAY_DATA_DIR=/var/lib/relay/data
#   RELAY_CONFIG_DIR=/etc/relay
#   RELAY_PORT=8080
#   RELAY_HOST_LABEL="$(hostname -s)"
#   RELAY_VPN_INTERFACE=wg0
#   RELAY_WIREGUARD_BACKEND=interface   # interface | wg-quick | networkmanager
#   RELAY_NM_PROFILE=""                 # e.g. /etc/NetworkManager/system-connections/wg0.nmconnection
#   RELAY_NM_CONNECTION_ID=""
#   RELAY_WG_QUICK_CONFIG=/etc/wireguard/wg0.conf
#   RELAY_VPN_DNS=""                    # e.g. 10.2.0.1
#   RELAY_NAT_PMP_GATEWAY=""            # e.g. 10.2.0.1 for Proton VPN
#   RELAY_REQUIRE_IPV6=false
#   RELAY_DL_USER=relaydl
#   RELAY_WEB_USER=relayweb
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo bash deploy/linux/install.sh)." >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

STAGING_DIR="${RELAY_STAGING_DIR:-/var/lib/relay/staging}"
DATA_DIR="${RELAY_DATA_DIR:-/var/lib/relay/data}"
CONFIG_DIR="${RELAY_CONFIG_DIR:-/etc/relay}"
PORT="${RELAY_PORT:-8080}"
HOST_LABEL="${RELAY_HOST_LABEL:-$(hostname -s 2>/dev/null || echo Server)}"
VPN_IFACE="${RELAY_VPN_INTERFACE:-wg0}"
WG_BACKEND="${RELAY_WIREGUARD_BACKEND:-interface}"
NM_PROFILE="${RELAY_NM_PROFILE:-}"
NM_CONN_ID="${RELAY_NM_CONNECTION_ID:-}"
WG_QUICK_CONF="${RELAY_WG_QUICK_CONFIG:-/etc/wireguard/${VPN_IFACE}.conf}"
VPN_DNS="${RELAY_VPN_DNS:-}"
NAT_PMP_GW="${RELAY_NAT_PMP_GATEWAY:-}"
REQ_IPV6="${RELAY_REQUIRE_IPV6:-false}"
DL_USER="${RELAY_DL_USER:-relaydl}"
WEB_USER="${RELAY_WEB_USER:-relayweb}"
APP_DIR="/opt/relay"

echo "== checking dependencies"
for bin in python3 aria2c nft ip; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "Missing required command: $bin. Install aria2, nftables, iproute2, and python3 first." >&2
        exit 1
    fi
done

echo "== system accounts"
id "$DL_USER" >/dev/null 2>&1 || useradd --system --user-group --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin --comment "Relay download engine" "$DL_USER"
id "$WEB_USER" >/dev/null 2>&1 || useradd --system --user-group --no-create-home \
    --home-dir "$DATA_DIR" --shell /usr/sbin/nologin --comment "Relay web interface" "$WEB_USER"
usermod -a -G "$DL_USER" "$WEB_USER"

echo "== directories"
install -d -o root -g "$WEB_USER" -m 0750 "$CONFIG_DIR"
install -d -o root -g root -m 0700 "$CONFIG_DIR/locations"
install -d -o "$WEB_USER" -g "$WEB_USER" -m 0750 "$DATA_DIR"
install -d -o "$DL_USER" -g "$DL_USER" -m 2770 "$STAGING_DIR"

echo "== installing Relay package into $APP_DIR/venv"
install -d -o root -g root -m 0755 "$APP_DIR"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade "$REPO_ROOT"

ln -sf "$APP_DIR/venv/bin/relay" /usr/local/bin/relay
install -o root -g root -m 0755 "$REPO_ROOT/relay/server_engine.py" /usr/local/sbin/relay-engine

echo "== configuration"
if [ ! -f "$CONFIG_DIR/engine.json" ]; then
    python3 - "$CONFIG_DIR/engine.json" "$STAGING_DIR" "$DL_USER" "$VPN_IFACE" "$WG_BACKEND" \
        "$NM_PROFILE" "$NM_CONN_ID" "$WG_QUICK_CONF" "$VPN_DNS" "$NAT_PMP_GW" "$REQ_IPV6" <<'PY'
import json, pathlib, sys
_, out, staging, dl_user, iface, backend, nm_prof, nm_id, wg_conf, dns, gw, req6 = sys.argv
cfg = {
    "staging_dir": staging,
    "rpc_port": 16874,
    "account": dl_user,
    "nft_table": "relay_guard",
    "vpn_interface": iface,
    "wireguard_backend": backend,
    "nm_profile": nm_prof,
    "nm_connection_id": nm_id,
    "wg_quick_config": wg_conf,
    "vpn_dns": dns,
    "nat_pmp_gateway": gw,
    "require_ipv6_route": req6.lower() in ("1", "true", "yes"),
    "locations_dir": "/etc/relay/locations",
}
pathlib.Path(out).write_text(json.dumps(cfg, indent=2) + "\n")
PY
    chown root:root "$CONFIG_DIR/engine.json"
    chmod 0640 "$CONFIG_DIR/engine.json"
fi

if [ ! -f "$CONFIG_DIR/config.json" ]; then
    python3 - "$CONFIG_DIR/config.json" "$STAGING_DIR" "$DATA_DIR" "$PORT" "$HOST_LABEL" "$VPN_IFACE" <<'PY'
import json, pathlib, secrets, sys
_, out, staging, data, port, label, iface = sys.argv
cfg = {
    "secret_key": secrets.token_hex(32),
    "instance_name": "Relay",
    "host_label": label,
    "host": "0.0.0.0",
    "port": int(port),
    "staging_dir": staging,
    "data_dir": data,
    "keep_days": 7,
    "engine_mode": "helper",
    "vpn_mode": "guard",
    "vpn_interface": iface,
    "helper_path": "/usr/local/sbin/relay-engine",
}
pathlib.Path(out).write_text(json.dumps(cfg, indent=2) + "\n")
PY
    chown root:"$WEB_USER" "$CONFIG_DIR/config.json"
    chmod 0640 "$CONFIG_DIR/config.json"
fi

echo "== sudoers rule"
SUDO_TMP="/etc/sudoers.d/relay-web.new"
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/relay-engine\n' "$WEB_USER" > "$SUDO_TMP"
chmod 0440 "$SUDO_TMP"
visudo -cf "$SUDO_TMP" >/dev/null
mv "$SUDO_TMP" /etc/sudoers.d/relay-web

if [ -d /etc/NetworkManager/dispatcher.d ]; then
    echo "== NetworkManager dispatcher hook"
    sed "s|__VPN_IFACE__|${VPN_IFACE}|g" "$SCRIPT_DIR/90-relay-vpn" > /etc/NetworkManager/dispatcher.d/90-relay-vpn
    chown root:root /etc/NetworkManager/dispatcher.d/90-relay-vpn
    chmod 0755 /etc/NetworkManager/dispatcher.d/90-relay-vpn
fi

echo "== nftables guard and engine"
/usr/local/sbin/relay-engine guard >/dev/null
if pgrep -u "$DL_USER" -x aria2c >/dev/null 2>&1; then
    pkill -TERM -u "$DL_USER" -x aria2c || true
    for _ in $(seq 1 30); do pgrep -u "$DL_USER" -x aria2c >/dev/null 2>&1 || break; sleep 0.5; done
fi
/usr/local/sbin/relay-engine start >/dev/null || echo "Engine did not start yet (is $VPN_IFACE up?). Relay will retry automatically."

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    echo "== systemd services"
    sed -e "s|__WEB_USER__|${WEB_USER}|g" -e "s|__CONFIG_DIR__|${CONFIG_DIR}|g" \
        -e "s|__DATA_DIR__|${DATA_DIR}|g" -e "s|__STAGING_DIR__|${STAGING_DIR}|g" \
        "$SCRIPT_DIR/relay-web.service" > /etc/systemd/system/relay-web.service
    cp "$SCRIPT_DIR/relay-guard.service" /etc/systemd/system/relay-guard.service
    systemctl daemon-reload
    systemctl enable --now relay-guard.service
    systemctl enable --now relay-web.service
    systemctl restart relay-web.service
elif command -v rc-service >/dev/null 2>&1; then
    echo "== OpenRC services"
    sed -e "s|__WEB_USER__|${WEB_USER}|g" -e "s|__CONFIG_DIR__|${CONFIG_DIR}|g" -e "s|__DATA_DIR__|${DATA_DIR}|g" \
        "$SCRIPT_DIR/relay-web.initd" > /etc/init.d/relay-web
    cp "$SCRIPT_DIR/relay-guard.initd" /etc/init.d/relay-guard
    chmod 0755 /etc/init.d/relay-web /etc/init.d/relay-guard
    rc-update add relay-guard boot >/dev/null 2>&1 || rc-update add relay-guard default >/dev/null 2>&1 || true
    rc-update add relay-web default >/dev/null 2>&1 || true
    rc-service relay-guard start >/dev/null 2>&1 || true
    rc-service relay-web restart >/dev/null 2>&1 || rc-service relay-web start
else
    echo "Neither systemd nor OpenRC detected. Start manually with:"
    echo "  sudo -u $WEB_USER /opt/relay/venv/bin/relay serve --config $CONFIG_DIR/config.json"
fi

echo "Relay installed. Open http://<server-lan-ip>:${PORT}/ on your local network."
