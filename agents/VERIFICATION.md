# Relay Verification Checklist

Run these checks after installing or reconfiguring Relay in any environment.

## 1. Environment & Diagnostics Check

```bash
relay doctor --config <path-to-config.json>
```

Expected:
- `[OK  ] python` (>= 3.10)
- `[OK  ] aria2c` (resolved binary path)
- `[OK  ] staging_dir` (writable with free disk space)
- `[OK  ] vpn` (matches chosen `vpn_mode`)

## 2. Unit & API Test Suite

From the repository root:

```bash
python3 -m unittest discover -s tests -v    # Linux / macOS
python -m unittest discover -s tests -v     # Windows
```

All tests in `test_core.py`, `test_web.py`, and `test_server_engine.py` must pass.

## 3. Live HTTP & Gatekeeping Smoke Test

Replace `127.0.0.1:8080` with the instance's bind address and port:

```bash
# 1. Health endpoint responds 200 OK
curl -fsS http://127.0.0.1:8080/healthz

# 2. Metadata endpoint responds 200 OK with configured branding & vpn_mode
curl -fsS http://127.0.0.1:8080/api/me

# 3. DNS-rebinding guard: spoofed external Host header MUST return 403
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: evil.example.com" http://127.0.0.1:8080/

# 4. CSRF guard: POST without X-Relay: 1 MUST return 403
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Content-Type: application/json" \
  -d '{"name":"Test"}' http://127.0.0.1:8080/api/login

# 5. Auth guard: /api/status without session MUST return 401
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/status
```

Expected status codes: `200`, `200`, `403`, `403`, `401`.

## 4. VPN Kill-Switch Verification (Hardened Linux `guard` Mode Only)

Verify both **egress IP** and **leak blocking**:

```bash
# 1. Inspect installed nftables guard table
sudo nft -nn list table inet relay_guard

# 2. Confirm helper health reports vpn_ready=true and guard=true
sudo /usr/local/sbin/relay-engine health

# 3. Confirm download user (default: relaydl) egresses through the VPN IP, NOT home ISP IP
sudo -u relaydl curl -s --max-time 8 https://ifconfig.me; echo

# 4. Confirm download user CANNOT reach local host services on loopback (must fail/refused)
sudo -u relaydl curl -s --max-time 3 http://127.0.0.1:8080/healthz || echo "OK: loopback blocked"
```

Optional full kill-switch test (briefly disconnects the VPN):
1. Bring the VPN interface down (`nmcli` or `wg-quick down wg0`).
2. Run `sudo -u relaydl curl -s --max-time 5 https://ifconfig.me` — it **must fail** with network/operation blocked by nftables, never returning your ISP IP.
3. Bring the VPN interface back up and verify `relay-engine health` returns `"vpn_ready": true`.

## 5. Docker + Gluetun Verification

```bash
# Confirm relay shares Gluetun's VPN IP
docker exec relay python3 -c "import urllib.request; print(urllib.request.urlopen('https://ifconfig.me', timeout=8).read().decode())"

# Confirm Gluetun health check is reachable from Relay
docker exec relay python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/v1/publicip/ip', timeout=5).read().decode())"
```
