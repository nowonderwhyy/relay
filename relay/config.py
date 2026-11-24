"""Configuration loading, live reload, and cross-platform path defaults."""
from __future__ import annotations

import dataclasses
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from typing import Any

try:
    import grp
except ImportError:  # Windows
    grp = None  # type: ignore[assignment]


DEFAULT_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.demonii.com:1337/announce',
    'udp://open.stealth.si:80/announce',
    'udp://tracker.torrent.eu.org:451/announce',
    'udp://explodie.org:6969/announce',
    'udp://open.tracker.cl:1337/announce',
]

SPEED_LIMITS = {
    '0': 'No limit',
    '10M': '10 MB/s',
    '5M': '5 MB/s',
    '2M': '2 MB/s',
    '1M': '1 MB/s',
}


def static_dir() -> Path:
    """Locate static web assets in source, installed wheel, or PyInstaller bundle."""
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        bundled = Path(meipass) / 'relay' / 'static'
        if bundled.is_dir():
            return bundled
    return Path(__file__).resolve().with_name('static')


def default_paths(portable: bool = False) -> dict[str, Path]:
    """Return sensible OS-native config, data, and staging paths."""
    if portable:
        base = Path.cwd() / 'relay-data'
        return {
            'config': base / 'config.json',
            'data': base / 'data',
            'staging': base / 'staging',
        }
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')) / 'Relay'
        return {
            'config': base / 'config.json',
            'data': base / 'data',
            'staging': base / 'staging',
        }
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support' / 'Relay'
        return {
            'config': base / 'config.json',
            'data': base / 'data',
            'staging': base / 'staging',
        }
    # Linux / BSD: follow XDG unless system paths are explicitly passed via env/CLI.
    xdg_config = Path(os.environ.get('XDG_CONFIG_HOME') or (Path.home() / '.config')) / 'relay'
    xdg_data = Path(os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share')) / 'relay'
    return {
        'config': xdg_config / 'config.json',
        'data': xdg_data / 'data',
        'staging': xdg_data / 'staging',
    }


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _parse_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(',') if part.strip()]


def _default_host_label() -> str:
    try:
        name = socket.gethostname().split('.')[0].strip()
        if name and len(name) <= 24:
            return name
    except OSError:
        pass
    return 'Local'


@dataclasses.dataclass
class Settings:
    """Resolved runtime settings for Relay."""
    config_path: Path
    staging_dir: Path
    data_dir: Path
    instance_name: str = 'Relay'
    host_label: str = ''
    host: str = '0.0.0.0'
    port: int = 8080
    keep_days: int = 7
    max_concurrent_downloads: int = 3
    engine_mode: str = 'direct'          # 'direct' | 'helper'
    vpn_mode: str = 'none'               # 'none' | 'interface' | 'external' | 'guard'
    vpn_interface: str = ''
    vpn_dns: str = ''
    vpn_gateway: str = ''
    vpn_health_url: str = ''
    aria2_binary: str = ''
    rpc_port: int = 16874
    file_allocation: str = 'auto'        # 'auto' | 'none' | 'trunc' | 'prealloc' | 'falloc'
    disk_cache: str = '64M'
    max_connection_per_server: int = 16
    split: int = 16
    min_split_size: str = '4M'
    seed_time: int = 0
    bt_max_peers: int = 100
    bt_request_peer_speed_limit: str = '5M'
    max_upload_limit: str = '0'
    dht_entry_point: str = 'router.bittorrent.com:6881'
    fallback_trackers: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_TRACKERS))
    allow_cgnat: bool = False
    allow_public_clients: bool = False
    allow_local_links: bool = False
    allowed_hosts: list[str] = dataclasses.field(default_factory=list)
    allowed_networks: list[str] = dataclasses.field(default_factory=list)
    trusted_proxies: list[str] = dataclasses.field(default_factory=list)
    helper_path: str = '/usr/local/sbin/relay-engine'
    location_labels: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def effective_host_label(self) -> str:
        return self.host_label or _default_host_label()

    @property
    def resolved_file_allocation(self) -> str:
        if self.file_allocation != 'auto':
            return self.file_allocation
        return 'falloc' if sys.platform.startswith('linux') else 'none'

    @classmethod
    def load(
        cls,
        config_path: Path | str | None = None,
        overrides: dict[str, Any] | None = None,
        portable: bool = False,
    ) -> Settings:
        """Load settings: OS defaults -> config JSON -> env vars -> explicit overrides."""
        defaults = default_paths(portable=portable)
        env_config = os.environ.get('RELAY_CONFIG') or os.environ.get('RELAY_WEB_CONFIG')
        cfg_path = Path(config_path or env_config or defaults['config']).expanduser()

        raw: dict[str, Any] = {}
        if cfg_path.is_file():
            try:
                raw = json.loads(cfg_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f'Could not read config file {cfg_path}: {exc}') from exc

        cfg_dir = cfg_path.parent

        def resolve_path(value: Any, fallback: Path) -> Path:
            if not value:
                return fallback.expanduser()
            p = Path(str(value)).expanduser()
            if not p.is_absolute():
                p = (cfg_dir / p).resolve()
            return p

        def pick(key: str, env_names: tuple[str, ...], default: Any) -> Any:
            if overrides and key in overrides and overrides[key] is not None:
                return overrides[key]
            for env_name in env_names:
                val = os.environ.get(env_name)
                if val is not None and val != '':
                    return val
            if key in raw and raw[key] is not None:
                return raw[key]
            return default

        staging_val = pick('staging_dir', ('RELAY_ROOT', 'RELAY_STAGING_DIR'), None)
        data_val = pick('data_dir', ('RELAY_DATA', 'RELAY_WEB_DATA', 'RELAY_DATA_DIR'), None)
        engine_mode = str(pick('engine_mode', ('RELAY_ENGINE_MODE',), 'direct')).strip().lower()
        default_vpn_mode = 'guard' if engine_mode == 'helper' else 'none'
        vpn_mode = str(pick('vpn_mode', ('RELAY_VPN_MODE',), default_vpn_mode)).strip().lower()

        if engine_mode not in {'direct', 'helper'}:
            raise ValueError(f'Unsupported engine_mode: {engine_mode!r} (expected direct or helper)')
        if vpn_mode not in {'none', 'interface', 'external', 'guard'}:
            raise ValueError(f'Unsupported vpn_mode: {vpn_mode!r}')

        trackers_raw = pick('fallback_trackers', ('RELAY_FALLBACK_TRACKERS',), DEFAULT_TRACKERS)
        trackers = _parse_list(trackers_raw) if trackers_raw != [] else []

        labels_raw = raw.get('location_labels')
        location_labels = dict(labels_raw) if isinstance(labels_raw, dict) else {}

        return cls(
            config_path=cfg_path,
            staging_dir=resolve_path(staging_val, defaults['staging']),
            data_dir=resolve_path(data_val, defaults['data']),
            instance_name=str(pick('instance_name', ('RELAY_INSTANCE_NAME',), 'Relay')).strip() or 'Relay',
            host_label=str(pick('host_label', ('RELAY_HOST_LABEL',), '')).strip(),
            host=str(pick('host', ('RELAY_HOST',), '0.0.0.0')).strip() or '0.0.0.0',
            port=int(pick('port', ('RELAY_PORT',), 8080)),
            keep_days=max(1, int(pick('keep_days', ('RELAY_KEEP_DAYS',), 7))),
            max_concurrent_downloads=max(1, int(pick('max_concurrent_downloads', ('RELAY_MAX_CONCURRENT',), 3))),
            engine_mode=engine_mode,
            vpn_mode=vpn_mode,
            vpn_interface=str(pick('vpn_interface', ('RELAY_VPN_INTERFACE',), '')).strip(),
            vpn_dns=str(pick('vpn_dns', ('RELAY_VPN_DNS',), '')).strip(),
            vpn_gateway=str(pick('vpn_gateway', ('RELAY_VPN_GATEWAY',), '')).strip(),
            vpn_health_url=str(pick('vpn_health_url', ('RELAY_VPN_HEALTH_URL',), '')).strip(),
            aria2_binary=str(pick('aria2_binary', ('RELAY_ARIA2_BIN',), '')).strip(),
            rpc_port=int(pick('rpc_port', ('RELAY_RPC_PORT',), 16874)),
            file_allocation=str(pick('file_allocation', ('RELAY_FILE_ALLOCATION',), 'auto')).strip().lower(),
            disk_cache=str(pick('disk_cache', ('RELAY_DISK_CACHE',), '64M')).strip(),
            max_connection_per_server=max(1, int(pick('max_connection_per_server', ('RELAY_MAX_CONN',), 16))),
            split=max(1, int(pick('split', ('RELAY_SPLIT',), 16))),
            min_split_size=str(pick('min_split_size', ('RELAY_MIN_SPLIT_SIZE',), '4M')).strip(),
            seed_time=max(0, int(pick('seed_time', ('RELAY_SEED_TIME',), 0))),
            bt_max_peers=max(0, int(pick('bt_max_peers', ('RELAY_BT_MAX_PEERS',), 100))),
            bt_request_peer_speed_limit=str(pick('bt_request_peer_speed_limit', ('RELAY_BT_SPEED_LIMIT',), '5M')).strip(),
            max_upload_limit=str(pick('max_upload_limit', ('RELAY_MAX_UPLOAD_LIMIT',), '0')).strip(),
            dht_entry_point=str(pick('dht_entry_point', ('RELAY_DHT_ENTRY_POINT',), 'router.bittorrent.com:6881')).strip(),
            fallback_trackers=trackers,
            allow_cgnat=_parse_bool(pick('allow_cgnat', ('RELAY_ALLOW_CGNAT',), False)),
            allow_public_clients=_parse_bool(pick('allow_public_clients', ('RELAY_ALLOW_PUBLIC_CLIENTS',), False)),
            allow_local_links=_parse_bool(pick('allow_local_links', ('RELAY_ALLOW_LOCAL_LINKS',), False)),
            allowed_hosts=_parse_list(pick('allowed_hosts', ('RELAY_ALLOWED_HOSTS',), [])),
            allowed_networks=_parse_list(pick('allowed_networks', ('RELAY_ALLOWED_NETWORKS',), [])),
            trusted_proxies=_parse_list(pick('trusted_proxies', ('RELAY_TRUSTED_PROXIES',), [])),
            helper_path=str(pick('helper_path', ('RELAY_HELPER_PATH',), '/usr/local/sbin/relay-engine')).strip(),
            location_labels=location_labels,
        )


class ConfigStore:
    """Live-reloading JSON config store so `relay passwd` takes effect without restart."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stamp: tuple[int, int, int] | None = None
        self._values: dict[str, Any] = {}

    def get(self) -> dict[str, Any]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return {}
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            self._values = json.loads(self.path.read_text(encoding='utf-8'))
            self._stamp = stamp
        return self._values


def write_config(values: dict[str, Any], path: Path, group_name: str | None = None) -> None:
    """Atomically write config.json with restrictive permissions on POSIX."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        try:
            os.chmod(path.parent, 0o750)
        except OSError:
            pass
    temp = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    temp.write_text(json.dumps(values, indent=2) + '\n', encoding='utf-8')
    if os.name != 'nt':
        try:
            os.chmod(temp, 0o640 if group_name else 0o600)
        except OSError:
            pass
        if group_name and grp is not None:
            try:
                os.chown(temp, -1, grp.getgrnam(group_name).gr_gid)
            except (KeyError, PermissionError, OSError):
                pass
    os.replace(temp, path)


def ensure_secret_key(path: Path) -> str:
    """Ensure config.json has a stable secret_key (or return ephemeral if read-only)."""
    store = ConfigStore(path)
    values = dict(store.get())
    if values.get('secret_key'):
        return str(values['secret_key'])
    key = secrets.token_hex(32)
    values['secret_key'] = key
    try:
        write_config(values, path)
    except OSError:
        pass
    return key
