#!/usr/bin/python3
"""Root-owned Linux helper for hardened VPN-guarded Relay server deployments.

Configuration is read from /etc/relay/engine.json (or RELAY_ENGINE_CONFIG).
Only manages Relay's dedicated download account, nftables kill-switch, aria2c
engine, staging cleanup, and optional WireGuard exit switching.
"""
from __future__ import annotations

import base64
import configparser
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import uuid
from typing import Any

try:
    import pwd
except ImportError:  # Windows/non-POSIX (module still importable for unit tests)
    pwd = None  # type: ignore[assignment]

# Bump when the nftables guard policy changes so existing tables get replaced.
GUARD_VERSION = '3'

DEFAULT_ENGINE_CONFIG: dict[str, Any] = {
    'staging_dir': '/var/lib/relay/staging',
    'rpc_port': 16874,
    'account': 'relaydl',
    'nft_table': 'relay_guard',
    'vpn_interface': 'wg0',
    'wireguard_backend': 'interface',  # 'networkmanager' | 'wg-quick' | 'interface'
    'nm_profile': '',
    'nm_connection_id': '',
    'wg_quick_config': '/etc/wireguard/wg0.conf',
    'vpn_dns': '',
    'nat_pmp_gateway': '',
    'require_ipv6_route': False,
    'locations_dir': '/etc/relay/locations',
    'backup_dir': '/var/backups/relay-vpn',
    'aria2_binary': '/usr/bin/aria2c',
    'max_concurrent_downloads': 3,
    'file_allocation': 'falloc',
    'disk_cache': '64M',
    'max_connection_per_server': 16,
    'split': 16,
    'min_split_size': '4M',
    'seed_time': 0,
    'bt_max_peers': 150,
    'bt_request_peer_speed_limit': '50M',
    'bt_max_open_files': 256,
    'http_accept_gzip': True,
    'max_upload_limit': '0',
    'dht_entry_point': 'router.bittorrent.com:6881',
}


def load_engine_config(path: str | pathlib.Path | None = None) -> dict[str, Any]:
    """Load engine.json merged over defaults."""
    cfg = dict(DEFAULT_ENGINE_CONFIG)
    target = pathlib.Path(path or os.environ.get('RELAY_ENGINE_CONFIG', '/etc/relay/engine.json'))
    if target.is_file():
        data = json.loads(target.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if v is not None})
    return cfg


def parse_wg_conf(text: str) -> dict[str, dict[str, str]]:
    """Parse a standard WireGuard .conf ([Interface] and [Peer] sections)."""
    conf: dict[str, dict[str, str]] = {'interface': {}, 'peer': {}}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip().lower()
            continue
        if section in conf and '=' in line:
            key, _, value = line.partition('=')
            conf[section][key.strip().lower()] = value.strip()
    return conf


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse host:port or [ipv6]:port endpoint string."""
    address, port = endpoint.rsplit(':', 1)
    ip = str(ipaddress.ip_address(address.strip('[]')))
    return ip, int(port)


def render_nm_profile(conf: dict[str, dict[str, str]], nm_id: str, conn_uuid: str, interface: str) -> str:
    """Render a NetworkManager .nmconnection file from a parsed WireGuard .conf."""
    iface, peer = conf['interface'], conf['peer']
    for key in ('privatekey', 'address'):
        if not iface.get(key):
            raise RuntimeError(f'WireGuard config is missing Interface {key}.')
    for key in ('publickey', 'endpoint', 'allowedips'):
        if not peer.get(key):
            raise RuntimeError(f'WireGuard config is missing Peer {key}.')
    addresses = [a.strip() for a in iface['address'].split(',') if a.strip()]
    dns = [d.strip() for d in iface.get('dns', '').split(',') if d.strip()]
    v4 = next((a for a in addresses if ':' not in a), '')
    v6 = next((a for a in addresses if ':' in a), '')
    dns4 = next((d for d in dns if ':' not in d), '')
    dns6 = next((d for d in dns if ':' in d), '')
    allowed = ';'.join(a.strip() for a in peer['allowedips'].split(',') if a.strip())
    keepalive = peer.get('persistentkeepalive', '25')
    lines = [
        '[connection]',
        f'id={nm_id}',
        f'uuid={conn_uuid}',
        'type=wireguard',
        f'interface-name={interface}',
        '',
        '[wireguard]',
        f'private-key={iface["privatekey"]}',
        '',
        f'[wireguard-peer.{peer["publickey"]}]',
        f'endpoint={peer["endpoint"]}',
        f'persistent-keepalive={keepalive}',
        f'allowed-ips={allowed}',
        '',
        '[ipv4]',
        'method=manual',
    ]
    if v4:
        lines.append(f'address1={v4}')
    if dns4:
        lines.append(f'dns={dns4};')
    lines += ['', '[ipv6]', 'addr-gen-mode=default', 'method=manual']
    if v6:
        lines.append(f'address1={v6}')
    if dns6:
        lines.append(f'dns={dns6};')
    lines += ['', '[proxy]', '']
    return '\n'.join(lines) + '\n'


def build_nft_policy(
    table: str,
    uid: int,
    vpn_interface: str,
    exception: str = '',
    replace_existing: bool = False,
) -> tuple[str, str]:
    """Generate the nftables kill-switch policy and its content signature."""
    if not re.fullmatch(r'[A-Za-z0-9_]+', table):
        raise RuntimeError(f'Invalid nftables table name: {table!r}')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', vpn_interface):
        raise RuntimeError(f'Invalid VPN interface name: {vpn_interface!r}')
    signature = hashlib.sha256((GUARD_VERSION + str(uid) + vpn_interface + exception).encode()).hexdigest()[:16]
    exc_line = f'  {exception}\n' if exception else ''
    policy = (
        f'table inet {table} {{\n'
        f' chain output {{\n'
        f'  type filter hook output priority 0; policy accept;\n'
        f'{exc_line}'
        f'  meta skuid {uid} oifname "lo" ct state new counter reject comment "Relay loopback"\n'
        f'  meta skuid {uid} oifname != "{vpn_interface}" oifname != "lo" counter reject comment "Relay {signature}"\n'
        f' }}\n'
        f'}}\n'
    )
    if replace_existing:
        policy = f'delete table inet {table}\n' + policy
    return policy, signature


def safe_job_dir(root: pathlib.Path, identifier: str) -> pathlib.Path:
    if not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise RuntimeError('Invalid job identifier.')
    resolved = root.resolve()
    target = resolved / identifier
    if target.is_symlink() or target.resolve().parent != resolved:
        raise RuntimeError('Invalid job directory.')
    return target


def _run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def _read_nm_peer(nm_profile: pathlib.Path) -> dict[str, str]:
    profile = configparser.ConfigParser(interpolation=None, strict=False)
    profile.read(nm_profile)
    peers = [s for s in profile.sections() if s.startswith('wireguard-peer.')]
    if len(peers) != 1:
        raise RuntimeError('Relay expects a single-peer WireGuard NetworkManager profile.')
    return {
        'private': profile.get('wireguard', 'private-key', fallback=''),
        'public': peers[0].split('wireguard-peer.', 1)[1],
        'endpoint': profile.get(peers[0], 'endpoint'),
    }


def _peer_endpoint(cfg: dict[str, Any]) -> tuple[str, int] | None:
    backend = cfg['wireguard_backend']
    if backend == 'networkmanager' and cfg.get('nm_profile'):
        nm_path = pathlib.Path(cfg['nm_profile'])
        if nm_path.is_file():
            return parse_endpoint(_read_nm_peer(nm_path)['endpoint'])
    elif backend == 'wg-quick' and cfg.get('wg_quick_config'):
        wg_path = pathlib.Path(cfg['wg_quick_config'])
        if wg_path.is_file():
            conf = parse_wg_conf(wg_path.read_text(encoding='utf-8', errors='replace'))
            if conf['peer'].get('endpoint'):
                return parse_endpoint(conf['peer']['endpoint'])
    return None


def guard(cfg: dict[str, Any]) -> None:
    if pwd is None:
        raise RuntimeError('nftables guard is only supported on Linux.')
    uid = pwd.getpwnam(cfg['account']).pw_uid
    vpn_iface = cfg['vpn_interface']
    table = cfg['nft_table']
    exception = ''

    endpoint = _peer_endpoint(cfg)
    if endpoint:
        address, port = endpoint
        try:
            routes = json.loads(_run('ip', '-j', 'route', 'get', '1.1.1.1'))
        except subprocess.CalledProcessError:
            routes = []
        route_table = routes[0].get('table') if routes and routes[0].get('dev') == vpn_iface else None
        try:
            rules = json.loads(_run('ip', '-j', 'rule'))
        except subprocess.CalledProcessError:
            rules = []
        marks = [r.get('fwmark') for r in rules if str(r.get('table')) == str(route_table) and r.get('fwmark')]
        mark = int(str(marks[0]), 0) if marks else None
        if mark:
            family = 'ip6' if ':' in address else 'ip'
            # Allow only kernel-marked WireGuard outer UDP packets to the pinned peer.
            exception = f'meta skuid {uid} meta mark {mark} {family} daddr {address} udp dport {port} accept'

    existing = subprocess.run(['nft', '-nn', 'list', 'table', 'inet', table], capture_output=True)
    policy, signature = build_nft_policy(table, uid, vpn_iface, exception, replace_existing=existing.returncode == 0)
    if existing.returncode == 0 and signature in existing.stdout.decode('utf-8', 'replace'):
        return
    subprocess.run(['nft', '-f', '-'], input=policy, text=True, check=True)


def forward_port(cfg: dict[str, Any]) -> int | None:
    gateway = cfg.get('nat_pmp_gateway') or ''
    if not gateway:
        return None
    vpn_iface = cfg['vpn_interface']
    port = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if hasattr(socket, 'SO_BINDTODEVICE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, vpn_iface.encode())
            sock.settimeout(1.5)
            sock.connect((gateway, 5351))
            for protocol in (1, 2):  # NAT-PMP: 1=UDP (DHT), 2=TCP (peers)
                sock.send(struct.pack('!BBHHHI', 0, protocol, 0, 0, 1, 60))
                _, answered, result, _, _, port, _ = struct.unpack('!BBHIHHI', sock.recv(64)[:16])
                if answered != 128 + protocol or result != 0:
                    return None
    except (OSError, struct.error):
        return None
    return port or None


def health(cfg: dict[str, Any]) -> dict[str, Any]:
    vpn_iface = cfg['vpn_interface']
    targets = [('ipv4', '1.1.1.1')]
    if cfg.get('require_ipv6_route'):
        targets.append(('ipv6', '2606:4700:4700::1111'))
    routes: dict[str, bool] = {}
    for family, address in targets:
        args = ['ip'] + (['-6'] if family == 'ipv6' else []) + ['route', 'get', address]
        proc = subprocess.run(args, capture_output=True, text=True)
        routes[family] = proc.returncode == 0 and f'dev {vpn_iface}' in proc.stdout
    interface = subprocess.run(['ip', '-brief', 'link', 'show', vpn_iface], capture_output=True, text=True)
    ready = all(routes.values()) and interface.returncode == 0
    return {
        'vpn_ready': ready,
        'vpn_interface': vpn_iface,
        'routes': routes,
        'guard': True,
        'root': str(cfg['staging_dir']),
        'port': int(cfg['rpc_port']),
        'forward': forward_port(cfg) if ready else None,
    }


def start(cfg: dict[str, Any]) -> dict[str, Any]:
    if pwd is None:
        raise RuntimeError('Linux helper start is only supported on Linux.')
    guard(cfg)
    state = health(cfg)
    vpn_iface = cfg['vpn_interface']
    if not state['vpn_ready']:
        raise RuntimeError(f'VPN is disconnected. Bring up {vpn_iface} before downloading.')

    root = pathlib.Path(cfg['staging_dir'])
    state_dir = root / '.engine'
    port = int(cfg['rpc_port'])
    account = pwd.getpwnam(cfg['account'])

    state_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(state_dir, account.pw_uid, account.pw_gid)

    token_file = state_dir / 'token'
    if not token_file.exists():
        token_file.write_text(secrets.token_hex(32) + '\n', encoding='utf-8')
        os.chmod(token_file, 0o640)
        os.chown(token_file, account.pw_uid, account.pw_gid)
    token = token_file.read_text(encoding='utf-8').strip()

    session_file = state_dir / 'session.txt'
    if not session_file.exists():
        session_file.touch(mode=0o640)
        os.chown(session_file, account.pw_uid, account.pw_gid)

    options: dict[str, str] = {
        'enable-rpc': 'true',
        'rpc-listen-all': 'false',
        'rpc-listen-port': str(port),
        'rpc-secret': token,
        'rpc-allow-origin-all': 'false',
        'dir': str(root),
        'save-session': str(session_file),
        'input-file': str(session_file),
        'save-session-interval': '10',
        'max-concurrent-downloads': str(cfg['max_concurrent_downloads']),
        'dht-file-path': str(state_dir / 'dht.dat'),
        'dht-file-path6': str(state_dir / 'dht6.dat'),
        'continue': 'true',
        'check-integrity': 'true',
        'auto-file-renaming': 'false',
        'allow-overwrite': 'false',
        'file-allocation': str(cfg['file_allocation']),
        'max-connection-per-server': str(cfg['max_connection_per_server']),
        'split': str(cfg['split']),
        'min-split-size': str(cfg['min_split_size']),
        'seed-time': str(cfg['seed_time']),
        'bt-max-peers': str(cfg['bt_max_peers']),
        'bt-request-peer-speed-limit': str(cfg['bt_request_peer_speed_limit']),
        'bt-max-open-files': str(cfg.get('bt_max_open_files', 256)),
        'http-accept-gzip': 'true' if cfg.get('http_accept_gzip', True) else 'false',
        'max-overall-upload-limit': str(cfg['max_upload_limit']),
        'bt-enable-lpd': 'false',
        'enable-dht': 'true',
        'enable-dht6': 'true',
        'enable-peer-exchange': 'true',
        'follow-torrent': 'false',
        'follow-metalink': 'false',
        'interface': vpn_iface,
        'rpc-save-upload-metadata': 'false',
        'rpc-max-request-size': '16M',
        'max-download-result': '1000',
        'keep-unfinished-download-result': 'true',
        'umask': '0027',
        'console-log-level': 'warn',
        'daemon': 'true',
        'disk-cache': str(cfg['disk_cache']),
        'connect-timeout': '15',
        'bt-tracker-connect-timeout': '10',
        'bt-tracker-timeout': '20',
    }
    if cfg.get('dht_entry_point'):
        options['dht-entry-point'] = str(cfg['dht_entry_point'])
    if cfg.get('vpn_dns'):
        options['async-dns'] = 'true'
        options['async-dns-server'] = str(cfg['vpn_dns'])
    if state['forward']:
        options['listen-port'] = options['dht-listen-port'] = str(state['forward'])

    conf_file = state_dir / 'aria2.conf'
    conf_file.write_text('\n'.join(f'{k}={v}' for k, v in options.items()) + '\n', encoding='utf-8')
    os.chmod(conf_file, 0o640)
    os.chown(conf_file, account.pw_uid, account.pw_gid)

    def ask(method: str) -> Any:
        payload = json.dumps({'jsonrpc': '2.0', 'id': 'health', 'method': method,
                              'params': ['token:' + token]}).encode('utf-8')
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{port}/jsonrpc', payload,
                                         {'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.load(response).get('result')
        except Exception:
            return None

    def running() -> str | None:
        return (ask('aria2.getVersion') or {}).get('version')

    version = running()
    if version and state['forward'] and (ask('aria2.getGlobalOption') or {}).get('listen-port') != str(state['forward']):
        ask('aria2.forceShutdown')
        for _ in range(40):
            if subprocess.run(['pgrep', '-u', cfg['account'], '-x', 'aria2c'], capture_output=True).returncode:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError('The download engine did not stop to change its forwarded port.')
        version = None

    if not version:
        subprocess.run(
            [cfg['aria2_binary'], '--conf-path=' + str(conf_file)],
            user=account.pw_uid,
            group=account.pw_gid,
            extra_groups=[account.pw_gid],
            cwd=str(state_dir),
            check=True,
            env={'HOME': str(state_dir), 'PATH': os.environ['PATH']},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for _ in range(20):
            version = running()
            if version:
                break
            time.sleep(0.15)

    if not version:
        raise RuntimeError('The download engine did not start. Run aria2c in foreground to diagnose.')
    state.update(token=token, version=version)
    return state


def _active_location(cfg: dict[str, Any]) -> str | None:
    locations_dir = pathlib.Path(cfg.get('locations_dir') or '')
    if not locations_dir.is_dir():
        return None
    backend = cfg['wireguard_backend']
    live_pub = live_endpoint = ''
    try:
        if backend == 'networkmanager' and cfg.get('nm_profile'):
            peer = _read_nm_peer(pathlib.Path(cfg['nm_profile']))
            live_pub, live_endpoint = peer['public'], peer['endpoint']
        elif backend == 'wg-quick' and cfg.get('wg_quick_config'):
            conf = parse_wg_conf(pathlib.Path(cfg['wg_quick_config']).read_text(encoding='utf-8', errors='replace'))
            live_pub = conf['peer'].get('publickey', '')
            live_endpoint = conf['peer'].get('endpoint', '')
    except Exception:
        return None

    for path in sorted(locations_dir.glob('*.conf')):
        try:
            conf = parse_wg_conf(path.read_text(encoding='utf-8', errors='replace'))
        except Exception:
            continue
        if conf['peer'].get('publickey') == live_pub and conf['peer'].get('endpoint') == live_endpoint:
            return path.stem
    return None


def location_list(cfg: dict[str, Any]) -> dict[str, Any]:
    locations_dir = pathlib.Path(cfg.get('locations_dir') or '')
    if not locations_dir.is_dir():
        return {'locations': [], 'active': None, 'count': 0}
    active = _active_location(cfg)
    rows = []
    for path in sorted(locations_dir.glob('*.conf')):
        try:
            conf = parse_wg_conf(path.read_text(encoding='utf-8', errors='replace'))
            pub = conf['peer'].get('publickey', '')
            fp = hashlib.sha256(base64.b64decode(pub)).hexdigest()[:16] if pub else ''
            rows.append({
                'slug': path.stem,
                'endpoint': conf['peer'].get('endpoint', ''),
                'fingerprint': fp,
                'active': path.stem == active,
            })
        except Exception as exc:
            rows.append({'slug': path.stem, 'error': str(exc)[:120], 'active': False})
    return {'locations': rows, 'active': active, 'count': len(rows)}


def _wait_for_interface(interface: str, timeout_steps: int = 40) -> bool:
    for _ in range(timeout_steps):
        probe = subprocess.run(['ip', '-br', 'addr', 'show', interface], capture_output=True, text=True)
        if probe.returncode == 0 and ('UP' in probe.stdout or 'UNKNOWN' in probe.stdout) and '/' in probe.stdout:
            return True
        time.sleep(0.5)
    return False


def location_set(cfg: dict[str, Any], slug: str) -> dict[str, Any]:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', slug):
        raise RuntimeError(f'Invalid location name: {slug}')
    locations_dir = pathlib.Path(cfg.get('locations_dir') or '')
    source = locations_dir / f'{slug}.conf'
    if not source.is_file():
        raise RuntimeError(f'No such location: {slug}')

    backend = cfg['wireguard_backend']
    vpn_iface = cfg['vpn_interface']
    backup_dir = pathlib.Path(cfg['backup_dir'])
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    conf_text = source.read_text(encoding='utf-8', errors='replace')
    conf = parse_wg_conf(conf_text)

    if backend == 'networkmanager':
        nm_path = pathlib.Path(cfg['nm_profile'])
        nm_id = cfg['nm_connection_id'] or nm_path.stem
        conn_uuid = str(uuid.uuid4())
        if nm_path.is_file():
            current = configparser.ConfigParser(interpolation=None, strict=False)
            current.read(nm_path)
            conn_uuid = current.get('connection', 'uuid', fallback=conn_uuid)
            backup = backup_dir / f'nm-{time.strftime("%Y%m%d-%H%M%S")}.nmconnection'
            shutil.copy2(nm_path, backup)
        else:
            backup = None
        rendered = render_nm_profile(conf, nm_id, conn_uuid, vpn_iface)
        tmp = nm_path.with_suffix('.tmp')
        tmp.write_text(rendered, encoding='utf-8')
        os.chmod(tmp, 0o600)
        os.chown(tmp, 0, 0)
        os.replace(tmp, nm_path)
        guard(cfg)
        try:
            subprocess.run(['nmcli', 'connection', 'reload'], check=True, capture_output=True)
            subprocess.run(['nmcli', 'connection', 'down', nm_id], capture_output=True)
            up = subprocess.run(['nmcli', 'connection', 'up', nm_id], capture_output=True, text=True)
            if not _wait_for_interface(vpn_iface):
                raise RuntimeError('Tunnel did not come up: ' + (up.stderr.strip() or up.stdout.strip())[:200])
        except Exception:
            if backup and backup.is_file():
                shutil.copy2(backup, nm_path)
                subprocess.run(['nmcli', 'connection', 'reload'], capture_output=True)
                subprocess.run(['nmcli', 'connection', 'up', nm_id], capture_output=True)
            raise
    elif backend == 'wg-quick':
        wg_path = pathlib.Path(cfg['wg_quick_config'])
        backup = None
        if wg_path.is_file():
            backup = backup_dir / f'wg-{time.strftime("%Y%m%d-%H%M%S")}.conf'
            shutil.copy2(wg_path, backup)
        tmp = wg_path.with_suffix('.tmp')
        tmp.write_text(conf_text, encoding='utf-8')
        os.chmod(tmp, 0o600)
        os.chown(tmp, 0, 0)
        os.replace(tmp, wg_path)
        guard(cfg)
        try:
            subprocess.run(['wg-quick', 'down', vpn_iface], capture_output=True)
            up = subprocess.run(['wg-quick', 'up', vpn_iface], capture_output=True, text=True)
            if up.returncode != 0 or not _wait_for_interface(vpn_iface):
                raise RuntimeError('wg-quick up failed: ' + (up.stderr.strip() or up.stdout.strip())[:200])
        except Exception:
            if backup and backup.is_file():
                shutil.copy2(backup, wg_path)
                subprocess.run(['wg-quick', 'up', vpn_iface], capture_output=True)
            raise
    else:
        raise RuntimeError('Location switching requires wireguard_backend to be "networkmanager" or "wg-quick".')

    # Restart aria2c so it rebinds cleanly to the new tunnel & forwarded port.
    subprocess.run(['pkill', '-u', cfg['account'], '-x', 'aria2c'], capture_output=True)
    for _ in range(40):
        if subprocess.run(['pgrep', '-u', cfg['account'], '-x', 'aria2c'], capture_output=True).returncode:
            break
        time.sleep(0.25)
    state = start(cfg)
    ep = _peer_endpoint(cfg)
    return {
        'switched': slug,
        'endpoint': f'{ep[0]}:{ep[1]}' if ep else '',
        'forward': state.get('forward'),
        'vpn_ready': state.get('vpn_ready'),
        'version': state.get('version'),
    }


def main(argv: list[str] | None = None) -> None:
    os.environ['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    args = sys.argv[1:] if argv is None else argv
    try:
        if not hasattr(os, 'geteuid') or os.geteuid() != 0:
            raise RuntimeError('Run relay-engine as root via the installed sudo rule.')
        cfg = load_engine_config()
        action = args[0] if args else ''
        if action == 'start':
            result: Any = start(cfg)
        elif action == 'health':
            guard(cfg)
            result = health(cfg)
        elif action == 'guard':
            guard(cfg)
            result = {'guard': True}
        elif action == 'cleanup' and len(args) <= 2:
            identifier = args[1] if len(args) == 2 else sys.stdin.readline().strip()
            target = safe_job_dir(pathlib.Path(cfg['staging_dir']), identifier)
            if target.exists():
                shutil.rmtree(target)
            result = {'cleaned': identifier}
        elif action == 'location' and len(args) >= 2:
            sub = args[1]
            if sub == 'list':
                result = location_list(cfg)
            elif sub == 'set' and len(args) == 3:
                result = location_set(cfg, args[2])
            else:
                raise RuntimeError('Usage: location list | location set <slug>')
        else:
            raise RuntimeError('Supported actions: start, health, guard, cleanup [job], location list|set')
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)


if __name__ == '__main__':
    main()
