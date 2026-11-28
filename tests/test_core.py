"""Unit tests for core validation, config loading, streaming ZIP, and DirectEngine."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from relay.config import ConfigStore, Settings, write_config
from relay.core import (
    UserError,
    check_password,
    download_error,
    effective_client_ip,
    hash_password,
    is_allowed_peer,
    is_page_host,
    list_files,
    password_stamp,
    resolve_inside,
    validate_link,
    zip_stream,
)
from relay.engine import DirectEngine, EngineDown, safe_job_dir


class CoreChecks(unittest.TestCase):
    def test_page_hosts_and_allowed_hosts(self):
        for good in ['192.168.1.50', '192.168.1.50:8080', 'localhost:8080', 'homeserver', '[::1]:80', 'box.lan']:
            self.assertTrue(is_page_host(good), good)
        for bad in ['evil.example.com', 'evil.example.com:80', '50.1.168.192.nip.io', '8.8.8.8', '']:
            self.assertFalse(is_page_host(bad), bad)
        self.assertTrue(is_page_host('mybox.tail123.ts.net', allowed_hosts=['*.ts.net']))
        self.assertTrue(is_page_host('relay.example.com:443', allowed_hosts=['relay.example.com']))
        self.assertFalse(is_page_host('ts.net', allowed_hosts=['*.ts.net']))

    def test_peer_gatekeeping_and_proxies(self):
        self.assertTrue(is_allowed_peer('127.0.0.1'))
        self.assertTrue(is_allowed_peer('192.168.1.44'))
        self.assertFalse(is_allowed_peer('8.8.8.8'))
        # Tailscale CGNAT (100.64.0.0/10) requires explicit opt-in.
        self.assertFalse(is_allowed_peer('100.64.0.12'))
        self.assertTrue(is_allowed_peer('100.64.0.12', allow_cgnat=True))
        self.assertTrue(is_allowed_peer('8.8.8.8', allow_public_clients=True))
        # X-Forwarded-For is ignored unless remote_addr is a trusted proxy.
        self.assertEqual(effective_client_ip('8.8.8.8', '192.168.1.10', trusted_proxies=['127.0.0.1']), '8.8.8.8')
        self.assertEqual(effective_client_ip('127.0.0.1', '192.168.1.10', trusted_proxies=['127.0.0.1']), '192.168.1.10')

    def test_links(self):
        self.assertEqual(validate_link(' https://example.org/a%20b.iso ')[1:], ('link', 'a b.iso'))
        self.assertEqual(validate_link('magnet:?xt=urn:btih:' + 'a' * 40 + '&dn=Show')[1:], ('magnet', 'Show'))
        for bad in [
            'file:///etc/passwd',
            'http://localhost:16874/jsonrpc',
            'http://127.0.0.1/',
            'http://192.168.1.1/',
            'http://[::1]/',
            'https://router.lan/',
            'magnet:?dn=nothing',
            'https://x.org/a\nhttps://y.org/b',
            '',
        ]:
            with self.assertRaises(UserError, msg=bad):
                validate_link(bad)
        self.assertEqual(validate_link('http://127.0.0.1/test.bin', allow_local_links=True)[1:], ('link', 'test.bin'))

    def test_download_errors(self):
        self.assertIn('Wait a few minutes', download_error('22', 'status=429'))
        self.assertIn('(503)', download_error('22', 'status=503'))
        self.assertEqual(download_error('3', 'Resource not found'), 'Nothing was found at that link (404).')
        self.assertEqual(download_error('99', 'Odd thing'), 'Odd thing')

    def test_files_stay_inside_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'job-files'
            (base / 'Show' / '.____padding_file').mkdir(parents=True, exist_ok=True)
            (base / 'Show' / 'ep1.mkv').write_bytes(b'one')
            (base / 'Show' / 'ep2.mkv.aria2').write_bytes(b'control')
            (base / 'Show' / '.____padding_file' / '0').write_bytes(b'\0')
            self.assertEqual([f['path'] for f in list_files(base)], ['Show/ep1.mkv'])
            self.assertEqual(resolve_inside(base, 'Show/ep1.mkv').read_bytes(), b'one')
            for bad in ['../secret.txt', 'Show/.____padding_file/0', '/etc/passwd', 'Show\\ep1.mkv', '']:
                with self.assertRaises(UserError, msg=bad):
                    resolve_inside(base, bad)

    def test_zip_stream_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'zip-job'
            (base / 'Album').mkdir(parents=True, exist_ok=True)
            (base / 'Album' / 'a.txt').write_bytes(b'alpha' * 1000)
            (base / 'Album' / 'b.bin').write_bytes(os.urandom(2 * 1024 * 1024))
            files = list_files(base)
            archive = zipfile.ZipFile(io.BytesIO(b''.join(zip_stream(base, files))))
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), ['Album/a.txt', 'Album/b.bin'])
            self.assertEqual(archive.read('Album/b.bin'), (base / 'Album' / 'b.bin').read_bytes())

    def test_passwords(self):
        stored = hash_password('correct horse')
        self.assertTrue(check_password('correct horse', stored))
        self.assertFalse(check_password('wrong horse', stored))
        self.assertFalse(check_password('x', 'garbage'))
        self.assertNotEqual(password_stamp(stored), password_stamp(hash_password('correct horse')))

    def test_settings_layering_and_config_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'config.json'
            write_config({'host_label': 'Lab', 'port': 9090, 'keep_days': 14, 'performance_profile': 'balanced'}, cfg)
            settings = Settings.load(config_path=cfg, overrides={'port': 9191})
            self.assertEqual(settings.host_label, 'Lab')
            self.assertEqual(settings.port, 9191)
            self.assertEqual(settings.keep_days, 14)
            self.assertEqual(settings.resolved_profile, 'balanced')
            self.assertEqual(settings.disk_cache, '64M')

            fast = Settings.load(config_path=cfg, overrides={'performance_profile': 'fast'})
            self.assertEqual(fast.resolved_profile, 'fast')
            self.assertEqual(fast.disk_cache, '256M')
            self.assertEqual(fast.min_split_size, '1M')
            self.assertEqual(fast.bt_max_peers, 250)
            self.assertEqual(fast.bt_request_peer_speed_limit, '100M')
            self.assertEqual(fast.bt_max_open_files, 512)
            self.assertTrue(fast.http_accept_gzip)

            store = ConfigStore(cfg)
            self.assertEqual(store.get()['keep_days'], 14)
            write_config({'host_label': 'Lab', 'port': 9090, 'keep_days': 3}, cfg)
            self.assertEqual(store.get()['keep_days'], 3)

    def test_direct_engine_cleanup_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'staging'
            root.mkdir()
            job_id = 'a' * 32
            job_dir = root / job_id
            job_dir.mkdir()
            (job_dir / 'file.bin').write_bytes(b'hello')

            settings = Settings.load(
                config_path=Path(tmp) / 'config.json',
                overrides={'staging_dir': str(root), 'data_dir': str(Path(tmp) / 'data')},
            )
            engine = DirectEngine(settings)
            engine.cleanup(job_id)
            self.assertFalse(job_dir.exists())

            for bad_id in ['../escape', 'not-hex', 'a' * 31, '.engine']:
                with self.assertRaises(EngineDown):
                    safe_job_dir(root, bad_id)


if __name__ == '__main__':
    unittest.main()
