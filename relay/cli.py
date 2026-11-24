"""Unified CLI for standalone and server Relay deployments."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigStore, Settings, default_paths, ensure_secret_key, write_config
from .core import Relay, hash_password
from .engine import build_engine, check_interface_up, find_aria2
from .store import Store
from .web import create_app


def _cmd_serve(args: argparse.Namespace) -> None:
    from waitress import serve as waitress_serve

    overrides: dict[str, Any] = {}
    for attr, key in [
        ('host', 'host'),
        ('port', 'port'),
        ('staging', 'staging_dir'),
        ('data', 'data_dir'),
        ('host_label', 'host_label'),
        ('vpn_mode', 'vpn_mode'),
        ('vpn_interface', 'vpn_interface'),
        ('engine_mode', 'engine_mode'),
    ]:
        val = getattr(args, attr, None)
        if val is not None:
            overrides[key] = val

    settings = Settings.load(config_path=args.config, overrides=overrides, portable=args.portable)
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    ensure_secret_key(settings.config_path)

    config_store = ConfigStore(settings.config_path)
    store = Store(settings.data_dir / 'relay.db')
    engine = build_engine(settings)
    relay = Relay(store, engine, settings)

    sync_thread = threading.Thread(target=relay.run_forever, name='relay-sync', daemon=True)
    sync_thread.start()
    app = create_app(relay, config_store, settings)

    display_host = '127.0.0.1' if settings.host in {'0.0.0.0', '::', ''} else settings.host
    url = f'http://{display_host}:{settings.port}/'
    print(
        f'{settings.instance_name} ({settings.effective_host_label}) v{__version__}\n'
        f'  URL:      {url}  (bound to {settings.host}:{settings.port})\n'
        f'  Mode:     engine={settings.engine_mode}, vpn={settings.vpn_mode}\n'
        f'  Staging:  {settings.staging_dir}\n'
        f'  Data:     {settings.data_dir}\n'
        f'  Config:   {settings.config_path}',
        file=sys.stderr,
        flush=True,
    )
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        waitress_serve(app, host=settings.host, port=settings.port, threads=16, ident=None, channel_timeout=300)
    except KeyboardInterrupt:
        pass
    finally:
        relay.stop()
        store.close()


def _cmd_init(args: argparse.Namespace) -> None:
    paths = default_paths(portable=args.portable)
    cfg_path = Path(args.config).expanduser() if args.config else paths['config']
    if cfg_path.exists() and not args.force:
        sys.exit(f'Config already exists at {cfg_path}. Pass --force to overwrite.')

    staging = str(Path(args.staging).expanduser()) if args.staging else str(paths['staging'])
    data = str(Path(args.data).expanduser()) if args.data else str(paths['data'])
    values: dict[str, Any] = {
        'secret_key': secrets.token_hex(32),
        'instance_name': args.instance_name,
        'host_label': args.host_label or '',
        'host': args.host,
        'port': args.port,
        'staging_dir': staging,
        'data_dir': data,
        'keep_days': args.keep_days,
        'max_concurrent_downloads': 3,
        'engine_mode': args.engine_mode,
        'vpn_mode': args.vpn_mode,
        'vpn_interface': args.vpn_interface or '',
        'vpn_dns': args.vpn_dns or '',
        'vpn_gateway': args.vpn_gateway or '',
        'allow_cgnat': bool(args.allow_cgnat),
        'allowed_hosts': [h.strip() for h in (args.allowed_hosts or '').split(',') if h.strip()],
    }
    write_config(values, cfg_path)
    Path(staging).mkdir(parents=True, exist_ok=True)
    Path(data).mkdir(parents=True, exist_ok=True)
    print(f'Wrote {cfg_path}')
    print(f'Start Relay with: relay serve --config "{cfg_path}"')


def _cmd_passwd(args: argparse.Namespace) -> None:
    settings = Settings.load(config_path=args.config, portable=args.portable)
    cfg_path = settings.config_path
    store = ConfigStore(cfg_path)
    values = dict(store.get())
    values.setdefault('secret_key', secrets.token_hex(32))
    group_name = os.environ.get('RELAY_CONFIG_GROUP') or None

    if args.clear:
        values.pop('password_hash', None)
        try:
            write_config(values, cfg_path, group_name=group_name)
        except PermissionError:
            sys.exit(f'Permission denied writing {cfg_path}. Re-run with sudo.')
        print('Household password removed. Anyone on the local network can use Relay with just a name.')
        return

    if sys.stdin.isatty():
        password = getpass.getpass('New household password: ')
        if password != getpass.getpass('Type it again: '):
            sys.exit('The two passwords did not match. Nothing changed.')
    else:
        password = sys.stdin.readline().rstrip('\r\n')
    if len(password) < 8:
        sys.exit('Use at least 8 characters. Nothing changed.')
    values['password_hash'] = hash_password(password)
    try:
        write_config(values, cfg_path, group_name=group_name)
    except PermissionError:
        sys.exit(f'Permission denied writing {cfg_path}. Re-run with sudo.')
    print('Household password set. Anyone already signed in will need to sign in again.')


def _cmd_doctor(args: argparse.Namespace) -> None:
    settings = Settings.load(config_path=args.config, portable=args.portable)
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({'check': name, 'ok': ok, 'detail': detail})

    record('python', sys.version_info >= (3, 10), f'{sys.version.split()[0]} ({sys.platform})')
    aria2 = find_aria2(settings.aria2_binary)
    record('aria2c', bool(aria2), aria2 or 'not found on PATH (install aria2 or set aria2_binary)')

    cfg_exists = settings.config_path.is_file()
    record('config', True, f'{settings.config_path} ({"exists" if cfg_exists else "using defaults"})')

    try:
        probe_dir = settings.staging_dir
        exists = probe_dir.is_dir()
        while not probe_dir.exists() and probe_dir != probe_dir.parent:
            probe_dir = probe_dir.parent
        usage = shutil.disk_usage(probe_dir)
        free_gb = usage.free / (1024 ** 3)
        state_note = '' if exists else ', will be created on start'
        record('staging_dir', free_gb >= 1.0, f'{settings.staging_dir} ({free_gb:.1f} GB free{state_note})')
    except OSError as exc:
        record('staging_dir', False, f'{settings.staging_dir} ({exc})')

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((settings.host if settings.host != '::' else '0.0.0.0', settings.port))
        record('listen_port', True, f'{settings.host}:{settings.port} available')
    except OSError as exc:
        record('listen_port', False, f'{settings.host}:{settings.port} busy or restricted ({exc})')

    if settings.engine_mode == 'helper':
        helper_ok = Path(settings.helper_path).is_file()
        record('server_helper', helper_ok, settings.helper_path)

    if settings.vpn_mode == 'none':
        record('vpn', True, 'direct mode (no VPN requirement configured)')
    elif settings.vpn_mode == 'interface':
        up = check_interface_up(settings.vpn_interface)
        record('vpn', up, f'interface {settings.vpn_interface or "(unset)"} is {"up" if up else "down"}')
    else:
        record('vpn', True, f'mode={settings.vpn_mode} interface={settings.vpn_interface or "(none)"}')

    if args.json:
        print(json.dumps({'version': __version__, 'checks': checks}, indent=2))
    else:
        print(f'Relay doctor (v{__version__})')
        for item in checks:
            mark = 'OK  ' if item['ok'] else 'WARN'
            print(f'  [{mark}] {item["check"]:14s} {item["detail"]}')

    if not all(item['ok'] for item in checks if item['check'] in {'python', 'aria2c', 'staging_dir'}):
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='relay', description='Relay — standalone household download relay')
    parser.add_argument('--version', action='version', version=f'relay {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)

    serve = commands.add_parser('serve', help='run the Relay web app and download engine')
    serve.add_argument('--config', help='path to config.json')
    serve.add_argument('--portable', action='store_true', help='store config, data, and staging in ./relay-data')
    serve.add_argument('--host', help='bind address (default: 0.0.0.0)')
    serve.add_argument('--port', type=int, help='listen port (default: 8080)')
    serve.add_argument('--staging', help='directory for active and completed downloads')
    serve.add_argument('--data', help='directory for SQLite ledger and uploaded .torrent files')
    serve.add_argument('--host-label', help='label shown beside the Relay wordmark in the UI')
    serve.add_argument('--engine-mode', choices=['direct', 'helper'], help='engine backend (default: direct)')
    serve.add_argument('--vpn-mode', choices=['none', 'interface', 'external', 'guard'], help='VPN enforcement mode')
    serve.add_argument('--vpn-interface', help='VPN interface name (e.g. wg0, tun0)')
    serve.add_argument('--open', action='store_true', help='open the Relay web page in the default browser')

    init = commands.add_parser('init', help='generate a config.json file for this environment')
    init.add_argument('--config', help='output path for config.json')
    init.add_argument('--portable', action='store_true', help='use ./relay-data paths')
    init.add_argument('--force', action='store_true', help='overwrite an existing config.json')
    init.add_argument('--instance-name', default='Relay')
    init.add_argument('--host-label', default='')
    init.add_argument('--host', default='0.0.0.0')
    init.add_argument('--port', type=int, default=8080)
    init.add_argument('--staging', default='')
    init.add_argument('--data', default='')
    init.add_argument('--keep-days', type=int, default=7)
    init.add_argument('--engine-mode', choices=['direct', 'helper'], default='direct')
    init.add_argument('--vpn-mode', choices=['none', 'interface', 'external', 'guard'], default='none')
    init.add_argument('--vpn-interface', default='')
    init.add_argument('--vpn-dns', default='')
    init.add_argument('--vpn-gateway', default='')
    init.add_argument('--allow-cgnat', action='store_true', help='allow RFC 6598 CGNAT clients (e.g. Tailscale)')
    init.add_argument('--allowed-hosts', default='', help='comma-separated extra Host headers (for reverse proxy)')

    passwd = commands.add_parser('passwd', help='set, change, or clear the optional household password')
    passwd.add_argument('--config', help='path to config.json')
    passwd.add_argument('--portable', action='store_true', help='use ./relay-data/config.json')
    passwd.add_argument('--clear', action='store_true', help='remove password so a name is enough')

    doctor = commands.add_parser('doctor', help='check aria2c, staging storage, ports, and VPN readiness')
    doctor.add_argument('--config', help='path to config.json')
    doctor.add_argument('--portable', action='store_true', help='use ./relay-data/config.json')
    doctor.add_argument('--json', action='store_true', help='emit machine-readable JSON output')

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'serve':
        _cmd_serve(args)
    elif args.command == 'init':
        _cmd_init(args)
    elif args.command == 'passwd':
        _cmd_passwd(args)
    elif args.command == 'doctor':
        _cmd_doctor(args)


if __name__ == '__main__':
    main()
