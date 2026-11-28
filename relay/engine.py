"""Pluggable aria2 engine adapters: DirectEngine (standalone) and HelperEngine (Linux guarded)."""
from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import Settings


class EngineDown(Exception):
    """aria2 or its helper is unavailable."""


class RPCError(Exception):
    """aria2 answered with an error."""


def find_aria2(configured: str = '') -> str | None:
    """Locate aria2c from explicit setting, PyInstaller bundle, or system PATH."""
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(configured)
        if found:
            return found
    # Check beside executable (useful for PyInstaller standalone bundles).
    exe_dir = Path(sys.executable).resolve().parent
    for name in ('aria2c.exe', 'aria2c'):
        sibling = exe_dir / name
        if sibling.is_file():
            return str(sibling)
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        for name in ('aria2c.exe', 'aria2c'):
            bundled = Path(meipass) / name
            if bundled.is_file():
                return str(bundled)
    return shutil.which('aria2c')


def pick_free_port(preferred: int = 16874) -> int:
    """Return preferred loopback port if free, else ask the OS for an ephemeral port."""
    if preferred > 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(('127.0.0.1', preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])


def nat_pmp_forward(gateway: str, interface: str = '') -> int | None:
    """Request a 60-second NAT-PMP UDP+TCP port mapping from a WireGuard VPN gateway."""
    if not gateway:
        return None
    port = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if interface and hasattr(socket, 'SO_BINDTODEVICE') and sys.platform.startswith('linux'):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode())
                except PermissionError:
                    pass
            sock.settimeout(1.5)
            sock.connect((gateway, 5351))
            for protocol in (1, 2):  # 1 = UDP (DHT), 2 = TCP (peers)
                sock.send(struct.pack('!BBHHHI', 0, protocol, 0, 0, 1, 60))
                _, answered, result, _, _, port, _ = struct.unpack('!BBHIHHI', sock.recv(64)[:16])
                if answered != 128 + protocol or result != 0:
                    return None
    except (OSError, struct.error):
        return None
    return port or None


def check_interface_up(interface: str) -> bool:
    """Cross-platform check whether a network interface exists and is up."""
    if not interface:
        return False
    try:
        if sys.platform.startswith('linux'):
            iface = subprocess.run(['ip', '-brief', 'link', 'show', interface],
                                   capture_output=True, text=True, timeout=5)
            if iface.returncode != 0:
                return False
            # Also confirm at least an IPv4 route via this interface if routing table is ready.
            route = subprocess.run(['ip', 'route', 'get', '1.1.1.1'],
                                   capture_output=True, text=True, timeout=5)
            if route.returncode == 0 and f'dev {interface}' in route.stdout:
                return True
            # Split-tunnel or interface-bound setup: interface UP/UNKNOWN with an address counts.
            addr = subprocess.run(['ip', '-brief', 'addr', 'show', interface],
                                  capture_output=True, text=True, timeout=5)
            return addr.returncode == 0 and ('UP' in addr.stdout or 'UNKNOWN' in addr.stdout)
        if sys.platform == 'darwin':
            res = subprocess.run(['ifconfig', interface], capture_output=True, text=True, timeout=5)
            return res.returncode == 0 and 'UP' in res.stdout and ('inet ' in res.stdout or 'inet6 ' in res.stdout)
        if os.name == 'nt':
            res = subprocess.run(['netsh', 'interface', 'show', 'interface', f'name={interface}'],
                                 capture_output=True, text=True, timeout=5)
            return res.returncode == 0 and 'Connected' in res.stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def check_health_url(url: str) -> bool:
    """Probe an external VPN health endpoint (such as Gluetun's local control server)."""
    if not url:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as resp:
            return 200 <= resp.status < 300 and bool(resp.read(256).strip())
    except (urllib.error.URLError, OSError):
        return False


def safe_job_dir(root: Path, identifier: str) -> Path:
    """Resolve a 32-hex job directory and reject traversal or symlinks."""
    if not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise EngineDown('Invalid job identifier.')
    resolved_root = root.resolve()
    target = resolved_root / identifier
    if target.is_symlink() or target.resolve().parent != resolved_root:
        raise EngineDown('Invalid job directory.')
    return target


class BaseEngine:
    """Shared aria2 JSON-RPC transport and engine contract."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.token: str | None = None
        self.rpc_port: int = settings.rpc_port
        self.health: dict[str, Any] = {'vpn_ready': settings.vpn_mode == 'none', 'vpn_mode': settings.vpn_mode}
        self.forward: int | None = None
        self.message: str = 'Starting the download engine…'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @property
    def rpc_url(self) -> str:
        return f'http://127.0.0.1:{self.rpc_port}/jsonrpc'

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def check_health(self) -> dict[str, Any]:
        raise NotImplementedError

    def cleanup(self, job_id: str) -> None:
        raise NotImplementedError

    def locations_list(self) -> dict[str, Any]:
        return {'available': False, 'locations': [], 'active': None}

    def location_set(self, slug: str) -> dict[str, Any]:
        raise EngineDown('VPN exit switching is not configured in this mode.')

    def rpc(self, method: str, *params: Any) -> Any:
        if not self.token:
            raise EngineDown(self.message or 'The download engine is not running.')
        body = json.dumps({
            'jsonrpc': '2.0',
            'id': 'relay',
            'method': method,
            'params': ['token:' + self.token, *params],
        }).encode('utf-8')
        request = urllib.request.Request(self.rpc_url, body, {'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=10) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                data = json.load(error)
            except ValueError:
                raise EngineDown(f'The download engine answered HTTP {error.code}.') from None
        except (urllib.error.URLError, OSError, ValueError):
            self.token = None
            self.message = 'The download engine stopped. Restarting it…'
            raise EngineDown(self.message) from None
        if 'error' in data:
            raise RPCError(data['error'].get('message', 'aria2 error'))
        return data['result']

    def quiet(self, method: str, *params: Any) -> Any:
        try:
            return self.rpc(method, *params)
        except RPCError:
            return None


class DirectEngine(BaseEngine):
    """Cross-platform standalone engine that directly spawns and supervises aria2c."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._proc: subprocess.Popen | None = None
        self._win_job: int | None = None
        self._lock = threading.RLock()
        self._state_dir = settings.staging_dir / '.engine'
        atexit.register(self.stop)

    def _attach_windows_kill_on_close(self, proc: subprocess.Popen) -> None:
        """Bind aria2c to a Windows Job Object so it exits automatically if Relay is killed."""
        if os.name != 'nt':
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(f, ctypes.c_uint64) for f in (
                    'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                    'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount',
                )]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ('PerProcessUserTimeLimit', ctypes.c_int64),
                    ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ('IoInfo', IO_COUNTERS),
                    ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t),
                ]

            if self._win_job is None:
                job = kernel32.CreateJobObjectW(None, None)
                if not job:
                    return
                info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                    kernel32.CloseHandle(job)
                    return
                self._win_job = int(job)

            kernel32.AssignProcessToJobObject(wintypes.HANDLE(self._win_job), wintypes.HANDLE(int(proc._handle)))
        except Exception:
            pass

    def _probe_rpc(self, token: str, port: int, method: str = 'aria2.getVersion') -> Any:
        body = json.dumps({
            'jsonrpc': '2.0',
            'id': 'probe',
            'method': method,
            'params': ['token:' + token],
        }).encode('utf-8')
        req = urllib.request.Request(f'http://127.0.0.1:{port}/jsonrpc', body, {'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=2) as resp:
                return json.load(resp).get('result')
        except Exception:
            return None

    def check_health(self) -> dict[str, Any]:
        mode = self.settings.vpn_mode
        if mode == 'none':
            ready = True
        elif mode == 'interface':
            ready = check_interface_up(self.settings.vpn_interface)
        elif mode == 'external':
            if self.settings.vpn_health_url:
                ready = check_health_url(self.settings.vpn_health_url)
            elif self.settings.vpn_interface:
                ready = check_interface_up(self.settings.vpn_interface)
            else:
                ready = True
        else:
            ready = check_interface_up(self.settings.vpn_interface)

        forwarded = nat_pmp_forward(self.settings.vpn_gateway, self.settings.vpn_interface) if (ready and self.settings.vpn_gateway) else None
        return {
            'vpn_ready': ready,
            'vpn_mode': mode,
            'vpn_interface': self.settings.vpn_interface,
            'guard': False,
            'root': str(self.settings.staging_dir),
            'port': self.rpc_port,
            'forward': forwarded,
        }

    def _build_aria2_options(self, token: str, port: int, session_file: Path, forward_port: int | None) -> dict[str, str]:
        s = self.settings
        options: dict[str, str] = {
            'enable-rpc': 'true',
            'rpc-listen-all': 'false',
            'rpc-listen-port': str(port),
            'rpc-secret': token,
            'rpc-allow-origin-all': 'false',
            'dir': str(s.staging_dir),
            'save-session': str(session_file),
            'input-file': str(session_file),
            'save-session-interval': '10',
            'max-concurrent-downloads': str(s.max_concurrent_downloads),
            'dht-file-path': str(self._state_dir / 'dht.dat'),
            'dht-file-path6': str(self._state_dir / 'dht6.dat'),
            'continue': 'true',
            'check-integrity': 'true',
            'auto-file-renaming': 'false',
            'allow-overwrite': 'false',
            'file-allocation': s.resolved_file_allocation,
            'max-connection-per-server': str(s.max_connection_per_server),
            'split': str(s.split),
            'min-split-size': s.min_split_size,
            'seed-time': str(s.seed_time),
            'bt-max-peers': str(s.bt_max_peers),
            'bt-request-peer-speed-limit': s.bt_request_peer_speed_limit,
            'bt-max-open-files': str(s.bt_max_open_files),
            'http-accept-gzip': 'true' if s.http_accept_gzip else 'false',
            'max-overall-upload-limit': s.max_upload_limit,
            'bt-enable-lpd': 'false',
            'enable-dht': 'true',
            'enable-dht6': 'true',
            'enable-peer-exchange': 'true',
            'follow-torrent': 'false',
            'follow-metalink': 'false',
            'rpc-save-upload-metadata': 'false',
            'rpc-max-request-size': '16M',
            'max-download-result': '1000',
            'keep-unfinished-download-result': 'true',
            'console-log-level': 'warn',
            'disk-cache': s.disk_cache,
            'connect-timeout': '15',
            'bt-tracker-connect-timeout': '10',
            'bt-tracker-timeout': '20',
        }
        if s.dht_entry_point:
            options['dht-entry-point'] = s.dht_entry_point
        if os.name != 'nt':
            options['umask'] = '0027'
        if s.vpn_interface:
            options['interface'] = s.vpn_interface
        if s.vpn_dns:
            options['async-dns'] = 'true'
            options['async-dns-server'] = s.vpn_dns
        if forward_port:
            options['listen-port'] = str(forward_port)
            options['dht-listen-port'] = str(forward_port)
        return options

    def start(self) -> None:
        with self._lock:
            aria2 = find_aria2(self.settings.aria2_binary)
            if not aria2:
                raise EngineDown(
                    'aria2c was not found. Install aria2 (e.g. `winget install aria2.aria2`, '
                    '`brew install aria2`, or `apt install aria2`) or set aria2_binary in config.json.'
                )
            state = self.check_health()
            self.health = state
            if self.settings.vpn_mode != 'none' and not state['vpn_ready']:
                target = self.settings.vpn_interface or self.settings.vpn_health_url or 'VPN'
                raise EngineDown(f'VPN ({target}) is disconnected. Downloads are paused until it reconnects.')

            self.settings.staging_dir.mkdir(parents=True, exist_ok=True)
            self._state_dir.mkdir(parents=True, exist_ok=True)
            if os.name != 'nt':
                try:
                    os.chmod(self._state_dir, 0o750)
                except OSError:
                    pass

            token_file = self._state_dir / 'token'
            if token_file.is_file():
                token = token_file.read_text(encoding='utf-8').strip()
            else:
                token = secrets.token_hex(32)
                token_file.write_text(token + '\n', encoding='utf-8')
                if os.name != 'nt':
                    try:
                        os.chmod(token_file, 0o600)
                    except OSError:
                        pass

            session_file = self._state_dir / 'session.txt'
            if not session_file.exists():
                session_file.touch()

            forward_port = state.get('forward')
            # If our child process is already alive and healthy with matching port, reuse it.
            if self._proc is not None and self._proc.poll() is None:
                ver = (self._probe_rpc(token, self.rpc_port) or {}).get('version')
                if ver:
                    if forward_port:
                        opts = self._probe_rpc(token, self.rpc_port, 'aria2.getGlobalOption') or {}
                        if opts.get('listen-port') != str(forward_port):
                            self.stop()
                        else:
                            self.token = token
                            self.forward = forward_port
                            self.message = ''
                            return
                    else:
                        self.token = token
                        self.forward = None
                        self.message = ''
                        return
                else:
                    self.stop()

            self.rpc_port = pick_free_port(self.settings.rpc_port)
            options = self._build_aria2_options(token, self.rpc_port, session_file, forward_port)
            conf_path = self._state_dir / 'aria2.conf'
            conf_path.write_text('\n'.join(f'{k}={v}' for k, v in options.items()) + '\n', encoding='utf-8')
            if os.name != 'nt':
                try:
                    os.chmod(conf_path, 0o600)
                except OSError:
                    pass

            creationflags = 0
            if os.name == 'nt':
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

            try:
                self._proc = subprocess.Popen(
                    [aria2, f'--conf-path={conf_path}'],
                    cwd=str(self._state_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise EngineDown(f'Could not launch aria2c: {exc}') from None
            self._attach_windows_kill_on_close(self._proc)

            version = None
            for _ in range(30):
                if self._proc.poll() is not None:
                    err = (self._proc.stderr.read().decode('utf-8', 'replace').strip() if self._proc.stderr else '')
                    self._proc = None
                    raise EngineDown(f'aria2c exited immediately: {err[-300:] or "unknown error"}')
                info = self._probe_rpc(token, self.rpc_port)
                if info and info.get('version'):
                    version = info['version']
                    break
                time.sleep(0.15)

            if not version:
                self.stop()
                raise EngineDown('aria2c started but did not answer JSON-RPC in time.')

            self.token = token
            self.forward = forward_port
            self.health = {**state, 'port': self.rpc_port, 'version': version}
            self.message = ''

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            token = self.token
            port = self.rpc_port
            self.token = None
            if proc is None:
                return
            if proc.poll() is None:
                if token:
                    self._probe_rpc(token, port, 'aria2.saveSession')
                    self._probe_rpc(token, port, 'aria2.forceShutdown')
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            if proc.stderr:
                try:
                    proc.stderr.close()
                except OSError:
                    pass

    def cleanup(self, job_id: str) -> None:
        target = safe_job_dir(self.settings.staging_dir, job_id)
        if not target.exists():
            return
        # On Windows, aria2c may take a fraction of a second to release file handles after forceRemove.
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                shutil.rmtree(target)
                return
            except OSError as exc:
                last_err = exc
                time.sleep(0.15 * (attempt + 1))
        raise EngineDown(f'Could not remove staging folder: {last_err}')


class HelperEngine(BaseEngine):
    """Hardened Linux server engine talking to a root-owned relay-engine helper via sudo."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.helper_path = settings.helper_path

    def helper(self, *args: str, stdin: str | None = None, timeout: int = 60) -> dict[str, Any]:
        try:
            done = subprocess.run(
                ['sudo', '-n', self.helper_path, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise EngineDown('sudo or the server helper was not found.') from None
        except subprocess.TimeoutExpired:
            raise EngineDown('The server helper timed out.') from None
        try:
            value = json.loads(done.stdout)
        except ValueError:
            raise EngineDown('The server helper is unavailable. ' + done.stderr[-200:].strip()) from None
        if value.get('error'):
            raise EngineDown(value['error'])
        return value

    def start(self) -> None:
        result = self.helper('start')
        self.token = result.pop('token')
        self.rpc_port = int(result.get('port', self.settings.rpc_port))
        self.health = result
        self.health.setdefault('vpn_mode', self.settings.vpn_mode)
        self.forward = result.get('forward')
        self.message = ''

    def check_health(self) -> dict[str, Any]:
        result = self.helper('health')
        result.setdefault('vpn_mode', self.settings.vpn_mode)
        return result

    def cleanup(self, job_id: str) -> None:
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise EngineDown('Invalid job identifier.')
        # Pass job ID on stdin so sudo logs do not record which download was deleted.
        self.helper('cleanup', stdin=job_id)

    def locations_list(self) -> dict[str, Any]:
        try:
            data = self.helper('location', 'list')
        except EngineDown:
            return {'available': False, 'locations': [], 'active': None}
        rows = [
            {
                'slug': row.get('slug', ''),
                'endpoint': row.get('endpoint', ''),
                'active': bool(row.get('active')),
            }
            for row in data.get('locations', [])
            if row.get('slug')
        ]
        return {'available': len(rows) > 0, 'locations': rows, 'active': data.get('active')}

    def location_set(self, slug: str) -> dict[str, Any]:
        return self.helper('location', 'set', slug, timeout=300)


def build_engine(settings: Settings) -> BaseEngine:
    if settings.engine_mode == 'helper':
        return HelperEngine(settings)
    return DirectEngine(settings)
