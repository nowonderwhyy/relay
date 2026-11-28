"""Core download orchestration, validation, password hashing, and streaming ZIP."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from .config import Settings
from .engine import BaseEngine, EngineDown, RPCError
from .store import Store

MAX_TORRENT = 10 * 1024 * 1024
ACTIVE = ('queued', 'downloading', 'paused')
HISTORY = ('complete', 'cancelled', 'error')

ARIA2_ERRORS = {
    '2': 'The download timed out.',
    '3': 'Nothing was found at that link (404).',
    '9': 'The drive is full.',
    '13': 'A file with that name already exists.',
    '19': 'That website could not be found.',
    '24': 'That link needs a login.',
}

HTTP_ERRORS = {
    401: 'That link needs a login.',
    403: 'The website refused the download (403). The link may have expired or need a login.',
    404: 'Nothing was found at that link (404).',
    410: 'That link has expired (410).',
    429: 'The website is limiting downloads right now (429). Wait a few minutes, then retry.',
}

CGNAT_NET = ipaddress.ip_network('100.64.0.0/10')


class UserError(Exception):
    """A problem the person using the page can fix."""


def download_error(code: str, message: str | None) -> str:
    """Translate aria2 error codes and HTTP statuses into plain sentences."""
    if code == '22' and (match := re.search(r'status=(\d{3})', message or '')):
        status = int(match.group(1))
        if status in HTTP_ERRORS:
            return HTTP_ERRORS[status]
        if status >= 500:
            return f'The website is having problems ({status}). Retry later.'
        return f'The website answered with an error ({status}).'
    return ARIA2_ERRORS.get(code) or message or 'The download failed.'


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2 ** 14, r=8, p=1)
    return f'scrypt$16384$8$1${salt.hex()}${digest.hex()}'


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split('$')
        actual = hashlib.scrypt(
            password.encode('utf-8'),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
        )
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(actual.hex(), digest)


def password_stamp(stored: str) -> str:
    """Changes whenever the password is set, rotated, or removed, signing everyone out."""
    return hashlib.sha256(stored.encode('utf-8')).hexdigest()[:16] if stored else 'open'


# ------------------------------------------------------------ input checks

def same_person(a: str | None, b: str | None) -> bool:
    """Names ignore case so 'Alex' on a phone and 'alex' on a laptop share one list."""
    return bool(a) and bool(b) and a.casefold() == b.casefold()


def clean_name(value: Any) -> str:
    name = ' '.join(str(value or '').split())[:24]
    name = ''.join(ch for ch in name if ch.isprintable())
    if not name:
        raise UserError('Enter your name so everyone can tell whose downloads are whose.')
    return name


def is_local_host(host: str) -> bool:
    host = host.strip('[]').lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.local', '.localhost', '.lan', '.home', '.internal')):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def _host_matches_pattern(host: str, pattern: str) -> bool:
    pattern = pattern.strip().lower().rstrip('.')
    if not pattern:
        return False
    if pattern.startswith('*.'):
        suffix = pattern[1:]  # e.g. '.ts.net'
        return host.endswith(suffix) and len(host) > len(suffix)
    return host == pattern


def is_page_host(host: str, allowed_hosts: Iterable[str] = ()) -> bool:
    """Block DNS rebinding by allowing only local addresses, bare LAN names, or configured hosts."""
    host = (host or '').strip().lower()
    if host.startswith('['):
        end = host.find(']')
        host = host[1:end] if end != -1 else ''
    else:
        host = host.rsplit(':', 1)[0]
    host = host.rstrip('.')
    if not host:
        return False
    if '.' not in host or is_local_host(host):
        return True
    return any(_host_matches_pattern(host, pat) for pat in allowed_hosts)


def _ip_in_networks(address: ipaddress.IPv4Address | ipaddress.IPv6Address, cidrs: Iterable[str]) -> bool:
    for item in cidrs:
        try:
            if '/' in item:
                if address in ipaddress.ip_network(item, strict=False):
                    return True
            else:
                if address == ipaddress.ip_address(item):
                    return True
        except ValueError:
            continue
    return False


def effective_client_ip(remote_addr: str | None, x_forwarded_for: str | None, trusted_proxies: Iterable[str] = ()) -> str:
    """Resolve client IP, honoring X-Forwarded-For only when the direct peer is a trusted proxy."""
    direct = (remote_addr or '').strip()
    if not direct or not x_forwarded_for or not trusted_proxies:
        return direct
    try:
        peer = ipaddress.ip_address(direct)
    except ValueError:
        return direct
    if not _ip_in_networks(peer, trusted_proxies):
        return direct
    hops = [part.strip() for part in x_forwarded_for.split(',') if part.strip()]
    # Walk from right to left and return the first hop that is not itself a trusted proxy.
    for hop in reversed(hops):
        try:
            hop_ip = ipaddress.ip_address(hop)
        except ValueError:
            return direct
        if not _ip_in_networks(hop_ip, trusted_proxies):
            return str(hop_ip)
    return hops[0] if hops else direct


def is_allowed_peer(
    remote_addr: str | None,
    *,
    allow_cgnat: bool = False,
    allow_public_clients: bool = False,
    allowed_networks: Iterable[str] = (),
) -> bool:
    """Return True if the client IP is allowed to access Relay."""
    try:
        peer = ipaddress.ip_address((remote_addr or '').strip())
    except ValueError:
        return False
    if allow_public_clients:
        return True
    if peer.is_private or peer.is_loopback:
        return True
    if allow_cgnat and isinstance(peer, ipaddress.IPv4Address) and peer in CGNAT_NET:
        return True
    if allowed_networks and _ip_in_networks(peer, allowed_networks):
        return True
    return False


def validate_link(text: str | None, *, allow_local_links: bool = False) -> tuple[str, str, str]:
    """Return (link, kind, suggested name) for a pasted link or raise UserError."""
    text = (text or '').strip()
    if not text:
        raise UserError('Paste a link first.')
    if len(text) > 8192 or any(ch in text for ch in '\r\n\t'):
        raise UserError('Paste one link at a time.')
    parts = urllib.parse.urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme == 'magnet':
        query = urllib.parse.parse_qs(parts.query)
        if not any(x.lower().startswith(('urn:btih:', 'urn:btmh:')) for x in query.get('xt', [])):
            raise UserError('That magnet link has no torrent identifier.')
        return text, 'magnet', (query.get('dn') or ['Torrent'])[0][:200]
    if scheme not in {'http', 'https', 'ftp'}:
        raise UserError('Paste a web (http/https), FTP, or magnet link.')
    if not parts.hostname:
        raise UserError('That link is missing a website address.')
    if not allow_local_links and is_local_host(parts.hostname):
        raise UserError('Relay downloads from the internet, not from local network addresses.')
    name = urllib.parse.unquote(PurePosixPath(parts.path).name) or parts.hostname
    return text, 'link', name[:200]


def is_padding(relative: str) -> bool:
    """BitTorrent padding files are filler, not content."""
    return any(part.startswith(('.____padding_file', '_____padding_file')) for part in PurePosixPath(relative).parts)


def list_files(base: Path) -> list[dict[str, Any]]:
    files = []
    if not base.is_dir():
        return files
    for directory, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for filename in sorted(filenames):
            full = Path(directory) / filename
            relative = full.relative_to(base).as_posix()
            if filename.endswith('.aria2') or is_padding(relative) or full.is_symlink() or not full.is_file():
                continue
            files.append({'path': relative, 'size': full.stat().st_size})
    return files


def resolve_inside(base: Path, relative: str) -> Path:
    """Resolve a relative download file path, refusing symlinks and path traversal."""
    if not relative or '\\' in relative or '\x00' in relative or is_padding(relative):
        raise UserError('That file is not part of this download.')
    candidate = base / relative
    for parent in [candidate, *candidate.parents]:
        if parent == base:
            break
        if parent.is_symlink():
            raise UserError('That file is not part of this download.')
    real = candidate.resolve()
    if not real.is_relative_to(base.resolve()) or not real.is_file():
        raise UserError('That file is not part of this download.')
    return real


class _Sink:
    """Write-only target that lets zipfile stream without seeking."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.position = 0

    def write(self, data: bytes | bytearray | memoryview) -> int:
        chunk = bytes(data)
        self.chunks.append(chunk)
        self.position += len(chunk)
        return len(chunk)

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b''.join(self.chunks)
        self.chunks.clear()
        return data


def zip_stream(base: Path, files: list[dict[str, Any]]) -> Iterator[bytes]:
    """Yield a store-only ZIP64 stream so multi-file downloads begin instantly."""
    sink = _Sink()
    with zipfile.ZipFile(sink, 'w', zipfile.ZIP_STORED, allowZip64=True) as archive:
        for item in files:
            full = resolve_inside(base, item['path'])
            info = zipfile.ZipInfo.from_file(full, arcname=item['path'])
            info.compress_type = zipfile.ZIP_STORED
            with full.open('rb') as source, archive.open(info, 'w', force_zip64=info.file_size > 0x7FFFFFFF) as target:
                while block := source.read(4 * 1024 * 1024):
                    target.write(block)
                    if sink.position and sink.chunks:
                        yield sink.take()
            yield sink.take()
    yield sink.take()


def disposition(name: str) -> str:
    plain = name.encode('ascii', 'ignore').decode().replace('"', '').strip() or 'download'
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


# --------------------------------------------------------------- orchestrator

class Relay:
    """Keeps the job ledger in step with aria2 and carries out user actions."""

    STATUS_KEYS = [
        'gid', 'status', 'totalLength', 'completedLength', 'downloadSpeed', 'connections',
        'numSeeders', 'errorCode', 'errorMessage', 'followedBy', 'bittorrent',
    ]

    def __init__(self, store: Store, engine: BaseEngine, settings: Settings):
        self.store = store
        self.engine = engine
        self.settings = settings
        self.live: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.health_at = 0.0
        self.retry_at = 0.0
        self.tidy_at = 0.0
        self.switching = False  # True while a VPN exit change is in progress
        self._stop_event = threading.Event()

    @property
    def root(self) -> Path:
        return self.settings.staging_dir

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    @property
    def keep_days(self) -> int:
        return self.settings.keep_days

    @property
    def trackers(self) -> str:
        return ','.join(self.settings.fallback_trackers)

    def stop(self) -> None:
        self._stop_event.set()
        self.engine.stop()

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                print('relay tick failed:', repr(exc), file=sys.stderr, flush=True)
            self._stop_event.wait(1.0)

    def tick(self) -> None:
        if self.switching:
            return
        now = time.monotonic()
        if now >= self.health_at:
            self.health_at = now + 15
            try:
                self.engine.health = self.engine.check_health()
            except EngineDown as exc:
                self.engine.health = {'vpn_ready': False, 'vpn_mode': self.settings.vpn_mode}
                self.engine.message = str(exc)
            # If VPN is required and dropped while direct engine is running, stop aria2 immediately.
            if (self.settings.vpn_mode != 'none'
                    and not self.engine.health.get('vpn_ready')
                    and self.engine.token):
                self.engine.stop()
                self.engine.message = 'The VPN is disconnected, so downloads are paused until it reconnects.'
            elif self.engine.health.get('forward') not in (None, self.engine.forward):
                # VPN forwarded port changed after reconnect: re-adopt engine on the new port.
                self.engine.token = None

        if not self.engine.token:
            if now < self.retry_at:
                return
            try:
                self.engine.start()
                self.apply_speed_limit()
                self.forget_strays()
            except EngineDown as exc:
                self.engine.message = str(exc)
                self.retry_at = now + 10
                return

        with self.lock:
            for job in self.store.all():
                if job['status'] in ACTIVE:
                    try:
                        self.sync(job)
                    except RPCError as exc:
                        self.store.update(job['id'], status='error', error=str(exc))
                    except EngineDown:
                        break
        if now >= self.tidy_at:
            self.tidy_at = now + 600
            self.tidy()

    def submit(self, job: dict[str, Any]) -> None:
        folder = self.root / job['id']
        folder.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt' and self.settings.engine_mode == 'helper':
            try:
                os.chmod(folder, 0o2770)
            except OSError:
                pass
        gid = secrets.token_hex(8)
        options: dict[str, str] = {
            'dir': str(folder),
            'gid': gid,
            'seed-time': str(self.settings.seed_time),
            'allow-overwrite': 'false',
            'auto-file-renaming': 'false',
            'follow-torrent': 'mem',
            'follow-metalink': 'mem',
        }
        if job['kind'] == 'torrent':
            torrent = (self.data_dir / 'torrents' / (job['id'] + '.torrent')).read_bytes()
            # Never announce a private torrent to public fallback trackers.
            if self.trackers and b'7:privatei1e' not in torrent:
                options['bt-tracker'] = self.trackers
            gid = self.engine.rpc('aria2.addTorrent', base64.b64encode(torrent).decode('ascii'), [], options)
        else:
            if self.trackers and job['kind'] == 'magnet':
                options['bt-tracker'] = self.trackers
            gid = self.engine.rpc('aria2.addUri', [job['source']], options)
        self.engine.quiet('aria2.saveSession')
        self.store.update(job['id'], gid=gid, status='downloading', error='')

    def sync(self, job: dict[str, Any]) -> None:
        if not job['gid']:
            if job['status'] == 'queued':
                self.submit(job)
            return
        try:
            data = self.engine.rpc('aria2.tellStatus', job['gid'], self.STATUS_KEYS)
        except RPCError:
            self.lost(job)
            return
        if data.get('followedBy'):
            gid = data['followedBy'][-1]
            self.engine.quiet('aria2.removeDownloadResult', job['gid'])
            self.store.update(job['id'], gid=gid)
            job['gid'] = gid
            data = self.engine.rpc('aria2.tellStatus', gid, self.STATUS_KEYS)
        info = data.get('bittorrent', {}).get('info', {})
        name = info.get('name') or job['name']
        total, completed = int(data.get('totalLength', 0)), int(data.get('completedLength', 0))
        speed = int(data.get('downloadSpeed', 0))
        state = data['status']
        if job['kind'] != 'link' and not info and state == 'active':
            phase = 'Getting torrent info'
        elif data.get('bittorrent') and state == 'active':
            phase = f"{data.get('numSeeders', '0')} seeds · {data.get('connections', '0')} peers"
        elif state == 'active':
            phase = f"{data.get('connections', '0')} connections"
        else:
            phase = ''
        self.live[job['id']] = {
            'speed': speed,
            'phase': phase,
            'eta': int((total - completed) / speed) if speed and total > completed else None,
        }
        fields: dict[str, Any] = {'name': name, 'total': total, 'completed': completed}
        if state == 'complete':
            fields.update(self.finish(job))
            self.engine.quiet('aria2.removeDownloadResult', job['gid'])
        elif state == 'error':
            self.engine.quiet('aria2.removeDownloadResult', job['gid'])
            fields.update(
                status='error',
                gid='',
                error=download_error(str(data.get('errorCode', '')), data.get('errorMessage')),
            )
        elif state == 'removed':
            fields.update(status='cancelled')
        elif state == 'active' and job['status'] == 'paused':
            pass  # forcePause takes a moment to close connections; avoid status flicker
        else:
            fields.update(status={'active': 'downloading', 'waiting': 'queued', 'paused': 'paused'}[state])
        self.store.update(job['id'], **fields)

    def finish(self, job: dict[str, Any]) -> dict[str, Any]:
        files = list_files(self.root / job['id'])
        size = sum(f['size'] for f in files)
        self.live.pop(job['id'], None)
        return {
            'status': 'complete',
            'finished': time.time(),
            'files': json.dumps(files),
            'total': size,
            'completed': size,
            'error': '',
        }

    def lost(self, job: dict[str, Any]) -> None:
        """If aria2 restarted and dropped a finished job, check disk before marking error."""
        folder = self.root / job['id']
        unfinished = folder.is_dir() and any(p.name.endswith('.aria2') for p in folder.rglob('*.aria2'))
        if list_files(folder) and not unfinished:
            self.store.update(job['id'], **self.finish(job))
        else:
            self.store.update(
                job['id'],
                status='error',
                gid='',
                error='The engine lost track of this download. Retry to continue where it stopped.',
            )

    def tidy(self) -> None:
        """Erase expired downloads after keep_days and orphaned staging folders after a day."""
        now = time.time()
        known = {job['id'] for job in self.store.all()}
        for folder in (self.root.iterdir() if self.root.is_dir() else []):
            if (re.fullmatch(r'[0-9a-f]{32}', folder.name)
                    and folder.name not in known
                    and now - folder.stat().st_mtime > 86400):
                try:
                    self.engine.cleanup(folder.name)
                except EngineDown:
                    pass
        for job in self.store.all():
            age = now - (job['finished'] or job['created'])
            if job['status'] in HISTORY and age > self.keep_days * 86400:
                try:
                    self.act(job['id'], 'remove')
                except EngineDown:
                    continue
        try:
            self.forget_strays()
        except (EngineDown, RPCError):
            pass

    def forget_strays(self) -> None:
        """Drop any aria2 download not present in the job ledger."""
        keys = ['gid', 'following']
        with self.lock:
            known = {job['gid'] for job in self.store.all() if job['gid']}
            held = (
                self.engine.rpc('aria2.tellActive', keys)
                + self.engine.rpc('aria2.tellWaiting', 0, 1000, keys)
                + self.engine.rpc('aria2.tellStopped', 0, 1000, keys)
            )
            strays = [
                item['gid'] for item in held
                if item['gid'] not in known and item.get('following') not in known
            ]
            for gid in strays:
                self.engine.quiet('aria2.forceRemove', gid)
                self.engine.quiet('aria2.removeDownloadResult', gid)
            if strays:
                self.engine.quiet('aria2.saveSession')

    def apply_speed_limit(self) -> None:
        self.engine.quiet(
            'aria2.changeGlobalOption',
            {'max-overall-download-limit': self.store.setting('speed_limit', '0')},
        )

    def add(self, owner: str, kind: str, source: str, name: str, torrent: bytes | None = None) -> str:
        job_id = uuid.uuid4().hex
        if torrent is not None:
            folder = self.data_dir / 'torrents'
            folder.mkdir(parents=True, exist_ok=True)
            if os.name != 'nt':
                try:
                    os.chmod(folder, 0o700)
                except OSError:
                    pass
            (folder / (job_id + '.torrent')).write_bytes(torrent)
        job = {
            'id': job_id,
            'gid': '',
            'kind': kind,
            'source': source,
            'name': name,
            'owner': owner,
            'created': time.time(),
            'status': 'queued',
        }
        with self.lock:
            self.store.add(job)
            if self.engine.token:
                try:
                    self.submit(self.store.get(job_id))
                except (EngineDown, RPCError) as exc:
                    self.store.update(job_id, status='error', error=str(exc))
        return job_id

    def act(self, job_id: str, action: str) -> None:
        with self.lock:
            job = self.store.get(job_id)
            if not job:
                raise UserError('That download is gone.')
            status = job['status']
            if action == 'pause' and status in {'downloading', 'queued'} and job['gid']:
                self.engine.rpc('aria2.forcePause', job['gid'])
                self.store.update(job_id, status='paused')
            elif action == 'resume' and status == 'paused':
                self.engine.rpc('aria2.unpause', job['gid'])
                self.store.update(job_id, status='downloading')
            elif action == 'cancel' and status in ACTIVE:
                if job['gid']:
                    self.engine.quiet('aria2.forceRemove', job['gid'])
                    self.engine.quiet('aria2.removeDownloadResult', job['gid'])
                self.store.update(
                    job_id,
                    status='cancelled',
                    gid='',
                    error=f'Partial files are kept for {self.keep_days} days. Retry to continue.',
                )
            elif action == 'retry' and status in {'error', 'cancelled'}:
                if job['gid']:
                    self.engine.quiet('aria2.forceRemove', job['gid'])
                    self.engine.quiet('aria2.removeDownloadResult', job['gid'])
                self.store.update(job_id, status='queued', gid='', error='')
            elif action == 'remove':
                if job['gid'] and status in ACTIVE:
                    self.engine.quiet('aria2.forceRemove', job['gid'])
                    self.engine.quiet('aria2.removeDownloadResult', job['gid'])
                self.engine.cleanup(job_id)
                (self.data_dir / 'torrents' / (job_id + '.torrent')).unlink(missing_ok=True)
                self.store.delete(job_id)
                self.live.pop(job_id, None)
            else:
                raise UserError('That action is not available right now.')
            try:
                self.engine.quiet('aria2.saveSession')
            except EngineDown:
                pass

    def clear_history(self, job_ids: Iterable[str]) -> int:
        cleared = 0
        for job_id in job_ids:
            job = self.store.get(job_id)
            if job and job['status'] in HISTORY:
                self.act(job_id, 'remove')
                cleared += 1
        return cleared

    def public(self, job: dict[str, Any]) -> dict[str, Any]:
        live = self.live.get(job['id'], {})
        files = json.loads(job['files'] or '[]')
        expires = None
        if job['status'] == 'complete' and job['finished']:
            expires = job['finished'] + self.keep_days * 86400
        return {
            'id': job['id'],
            'name': job['name'],
            'kind': job['kind'],
            'owner': job['owner'],
            'status': job['status'],
            'error': job['error'],
            'total': job['total'],
            'completed': job['completed'],
            'created': job['created'],
            'finished': job['finished'],
            'speed': live.get('speed', 0),
            'eta': live.get('eta'),
            'phase': live.get('phase', ''),
            'files': files if len(files) <= 200 else files[:200],
            'file_count': len(files),
            'expires': expires,
        }
