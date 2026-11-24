# Environment Configuration Matrix

Use this matrix to map a user's machine and VPN setup to the exact Relay configuration keys.

## 1. Execution Mode Selection

| Environment | `engine_mode` | `vpn_mode` | Requires Root? | Best Profile |
| --- | --- | --- | --- | --- |
| Windows / macOS / Linux desktop (local or LAN) | `direct` | `none` | No | `standalone-desktop.md` |
| Desktop or server with an existing VPN interface (`wg0`/`utun*`/`tun0`) | `direct` | `interface` | No | `standalone-desktop.md` |
| Docker standalone | `direct` | `none` | No | `docker-gluetun.md` |
| Docker + Gluetun VPN container | `direct` | `external` | No (container `NET_ADMIN` on Gluetun only) | `docker-gluetun.md` |
| Dedicated Linux server with OS-level UID kill-switch (`nftables`) | `helper` | `guard` | Yes (during install; runtime uses one restricted `sudo` helper) | `linux-systemd-vpn.md` or `linux-openrc-vpn.md` |

## 2. VPN Provider Presets

| Provider | Typical Tunnel | `vpn_dns` | `vpn_gateway` / `nat_pmp_gateway` | `require_ipv6_route` | Notes |
| --- | --- | --- | --- | --- | --- |
| **Proton VPN** (WireGuard) | `wg0` | `10.2.0.1` | `10.2.0.1` (if NAT-PMP enabled in profile) | `true` if dual-stack, else `false` | NAT-PMP renews forwarded torrent port every 60s automatically. |
| **Mullvad** (WireGuard) | `wg0` | `10.64.0.1` | `""` (disabled) | `false` | Mullvad retired port forwarding; leave `nat_pmp_gateway` empty. |
| **AirVPN / PIA** | `wg0` or `tun0` | Provider tunnel DNS | `""` (or Gluetun port forward) | `false` | Easiest with `docker-compose.gluetun.yml`. |
| **IVPN / Custom WireGuard** | `wg0` | Tunnel DNS or `""` | `""` | `false` | Set `wireguard_backend` to `wg-quick` or `networkmanager` for exit switching. |
| **No VPN / System-wide VPN app** | n/a | `""` | `""` | `false` | Set `vpn_mode: "none"`. UI clearly displays `Direct`. |

## 3. OS & Filesystem Quirks

| OS / Filesystem | `file_allocation` | `aria2c` Install Command | Notes |
| --- | --- | --- | --- |
| Linux (`ext4`, `xfs`, `btrfs`) | `"auto"` -> `falloc` | `sudo apt install aria2` / `apk add aria2` / `pacman -S aria2` | `falloc` prevents torrent fragmentation instantaneously on ext4/xfs. |
| Linux (FAT32 / exFAT / older NAS SMB mounts) | `"none"` | Same as above | Set `"file_allocation": "none"` if staging is on a filesystem that rejects `fallocate()`. |
| Windows (NTFS) | `"auto"` -> `none` | `winget install aria2.aria2` or `choco install aria2` | `falloc` and `umask` are unsupported on Windows; Relay handles this automatically. |
| macOS (APFS) | `"auto"` -> `none` | `brew install aria2` | `falloc` is unsupported on APFS; Relay uses `none` automatically. |

## 4. Network & Access Control Keys

| Scenario | `allow_cgnat` | `allowed_hosts` | `trusted_proxies` | `allow_public_clients` |
| --- | --- | --- | --- | --- |
| Home LAN only (`192.168.x.x`, `10.x.x.x`, `172.16.x.x`, `localhost`) | `false` | `[]` | `[]` | `false` |
| Tailscale (direct tailnet IP `100.x.y.z` or MagicDNS `box.tailnet.ts.net`) | `true` | `["*.ts.net"]` (or exact host) | `[]` | `false` |
| Local Caddy / Nginx reverse proxy on same host (`127.0.0.1`) | `false` (or `true` for Tailscale clients) | `["relay.example.lan"]` | `["127.0.0.1", "::1"]` | `false` |
| Reverse proxy on another LAN box (`192.168.1.2`) | `false` | `["relay.home.example.com"]` | `["192.168.1.2"]` | `false` (or `true` **only** with `relay passwd` + HTTPS!) |

> **Warning**: Never set `allow_public_clients: true` unless Relay is behind TLS (HTTPS) **and** a strong household password has been configured via `relay passwd`.
