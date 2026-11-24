"""Web API, isolation, gatekeeping, retention, and location switcher tests."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path

from relay.config import ConfigStore, Settings, write_config
from relay.core import Relay, hash_password
from relay.engine import BaseEngine
from relay.store import Store
from relay.web import create_app

LAN = {'REMOTE_ADDR': '192.168.1.44'}
POST = {'X-Relay': '1'}


class FakeEngine(BaseEngine):
    def __init__(self, settings: Settings, locations=None):
        super().__init__(settings)
        self.token = 'test-token'
        self.health = {'vpn_ready': True, 'vpn_mode': settings.vpn_mode}
        self.forward = None
        self.message = ''
        self.calls = []
        self._locations = locations or []
        self._active_location = self._locations[0]['slug'] if self._locations else None

    def start(self):
        self.calls.append(('start', ()))
        self.token = 'test-token'

    def check_health(self):
        self.calls.append(('health', ()))
        return dict(self.health)

    def cleanup(self, job_id: str):
        self.calls.append(('cleanup', (job_id,)))

    def rpc(self, method, *params):
        self.calls.append((method, params))
        if method in ('aria2.tellActive', 'aria2.tellWaiting', 'aria2.tellStopped'):
            return []
        return {'aria2.addUri': 'aaaaaaaaaaaaaaaa', 'aria2.addTorrent': 'bbbbbbbbbbbbbbbb'}.get(method, 'OK')

    quiet = rpc

    def locations_list(self):
        return {
            'available': len(self._locations) > 0,
            'active': self._active_location,
            'locations': [
                {**loc, 'active': loc['slug'] == self._active_location}
                for loc in self._locations
            ],
        }

    def location_set(self, slug: str):
        self.calls.append(('location_set', (slug,)))
        self._active_location = slug
        return {'switched': slug, 'vpn_ready': True, 'forward': 51234}


class WebPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg_path = root / 'config.json'
        self.settings = Settings.load(
            config_path=self.cfg_path,
            overrides={
                'staging_dir': str(root / 'staging'),
                'data_dir': str(root / 'data'),
                'host_label': 'TestBox',
                'vpn_mode': 'guard',
            },
        )
        self.settings.staging_dir.mkdir(parents=True, exist_ok=True)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        write_config({'secret_key': 'k' * 64, 'password_hash': hash_password('household pass')}, self.cfg_path)
        self.engine = FakeEngine(self.settings)
        self.store = Store(self.settings.data_dir / 'relay.db')
        self.relay = Relay(self.store, self.engine, self.settings)
        self.app = create_app(self.relay, ConfigStore(self.cfg_path), self.settings)
        self.client = self.app.test_client()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def sign_in(self, name='Alex', client=None):
        response = (client or self.client).post(
            '/api/login',
            json={'name': name, 'password': 'household pass'},
            headers=POST,
            environ_base=LAN,
        )
        self.assertEqual(response.status_code, 200)

    def add_job(self, owner, status):
        job_id = uuid.uuid4().hex
        self.relay.store.add({
            'id': job_id,
            'gid': '',
            'kind': 'link',
            'source': 'https://example.org/' + status,
            'name': status,
            'owner': owner,
            'created': time.time(),
            'status': status,
        })
        return job_id

    def visible(self, client):
        return [job['id'] for job in client.get('/api/status', environ_base=LAN).get_json()['jobs']]

    def test_people_only_see_their_own_downloads(self):
        self.sign_in('Alex')
        alexs = self.add_job('Alex', 'complete')
        (self.settings.staging_dir / alexs).mkdir(exist_ok=True)
        (self.settings.staging_dir / alexs / 'notes.txt').write_bytes(b'alex only')
        self.relay.store.update(alexs, files=json.dumps([{'path': 'notes.txt', 'size': 9}]))
        sams = self.add_job('Sam', 'downloading')
        self.assertEqual(self.visible(self.client), [alexs])

        sam = self.app.test_client()
        self.sign_in('Sam', sam)
        self.assertEqual(self.visible(sam), [sams])
        self.assertEqual(sam.get(f'/files/{alexs}/notes.txt', environ_base=LAN).status_code, 404)
        self.assertEqual(sam.get(f'/zip/{alexs}', environ_base=LAN).status_code, 404)
        self.assertEqual(sam.post(f'/api/jobs/{alexs}/remove', headers=POST, environ_base=LAN).status_code, 404)
        self.assertIsNotNone(self.relay.store.get(alexs))

        phone = self.app.test_client()
        self.sign_in('alex', phone)
        self.assertEqual(self.visible(phone), [alexs])
        with phone.get(f'/files/{alexs}/notes.txt', environ_base=LAN) as resp:
            self.assertEqual(resp.data, b'alex only')

    def test_clear_history_keeps_active_downloads(self):
        self.sign_in('Alex')
        keep = {self.add_job('Alex', s) for s in ('downloading', 'paused', 'queued')}
        gone = {self.add_job('Alex', 'complete'), self.add_job('ALEX', 'cancelled'), self.add_job('Alex', 'error')}
        other = self.add_job('Sam', 'complete')
        torrent = self.settings.data_dir / 'torrents' / (min(gone) + '.torrent')
        torrent.parent.mkdir(parents=True, exist_ok=True)
        torrent.write_bytes(b'd4:infod4:name1:xee')

        response = self.client.post('/api/clear', headers=POST, environ_base=LAN)
        self.assertEqual(response.get_json()['cleared'], 3)
        self.assertEqual({j['id'] for j in self.relay.store.all()}, keep | {other})
        for job_id in gone:
            self.assertIn(('cleanup', (job_id,)), self.engine.calls)
        self.assertFalse(torrent.exists())
        self.assertEqual(self.client.post('/api/clear', environ_base=LAN).status_code, 403)

    def test_gatekeeping_and_healthz(self):
        self.assertEqual(self.client.get('/healthz', environ_base=LAN).status_code, 200)
        self.assertEqual(self.client.get('/api/status', environ_base={'REMOTE_ADDR': '8.8.8.8'}).status_code, 403)
        self.assertEqual(self.client.get('/api/status', environ_base=LAN).status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={}, environ_base=LAN).status_code, 403)
        wrong = self.client.post('/api/login', json={'name': 'Alex', 'password': 'nope'}, headers=POST, environ_base=LAN)
        self.assertEqual(wrong.status_code, 400)
        with self.client.get('/', environ_base=LAN) as home:
            self.assertEqual(home.status_code, 200)
        self.assertEqual(self.client.get('/', base_url='http://evil.example.com', environ_base=LAN).status_code, 403)
        me = self.client.get('/api/me', environ_base=LAN).get_json()
        self.assertTrue(me['needs_password'])
        self.assertEqual(me['host_label'], 'TestBox')

    def test_name_is_enough_without_password_and_live_reload(self):
        write_config({'secret_key': 'k' * 64}, self.cfg_path)
        me = self.client.get('/api/me', environ_base=LAN).get_json()
        self.assertEqual((me['signed_in'], me['needs_password']), (False, False))
        self.assertEqual(self.client.post('/api/login', json={'name': '  '}, headers=POST, environ_base=LAN).status_code, 400)
        self.assertEqual(self.client.post('/api/login', json={'name': 'Sam'}, headers=POST, environ_base=LAN).status_code, 200)
        self.assertEqual(self.client.get('/api/status', environ_base=LAN).get_json()['me'], 'Sam')
        # Enabling a password invalidates existing sessions live.
        write_config({'secret_key': 'k' * 64, 'password_hash': hash_password('household pass')}, self.cfg_path)
        self.assertEqual(self.client.get('/api/status', environ_base=LAN).status_code, 401)

    def test_add_link_range_download_and_zip(self):
        self.sign_in('Alex')
        response = self.client.post('/api/jobs', json={'link': 'https://example.org/file.bin'}, headers=POST, environ_base=LAN)
        self.assertEqual(response.status_code, 200)
        job_id = response.get_json()['id']
        (self.settings.staging_dir / job_id / 'file.bin').write_bytes(b'0123456789')
        self.relay.store.update(job_id, **self.relay.finish(self.relay.store.get(job_id)))

        with self.client.get(f'/files/{job_id}/file.bin', headers={'Range': 'bytes=2-4'}, environ_base=LAN) as part:
            self.assertEqual((part.status_code, part.data), (206, b'234'))
        with self.client.get(f'/zip/{job_id}', environ_base=LAN) as archive:
            self.assertEqual(zipfile.ZipFile(io.BytesIO(archive.data)).read('file.bin'), b'0123456789')

    def test_private_torrents_never_get_fallback_trackers(self):
        self.sign_in('Alex')

        def options_for(**req):
            self.client.post('/api/jobs', headers=POST, environ_base=LAN, **req)
            return self.engine.calls[-2][1][-1]

        magnet = options_for(json={'link': 'magnet:?xt=urn:btih:' + 'a' * 40})
        self.assertIn('udp://tracker.opentrackr.org', magnet['bt-tracker'])
        public = options_for(data={'torrent': (io.BytesIO(b'd4:infod4:name1:xee'), 'x.torrent')})
        self.assertIn('bt-tracker', public)
        private = options_for(data={'torrent': (io.BytesIO(b'd4:infod4:name1:x7:privatei1eee'), 'x.torrent')})
        self.assertNotIn('bt-tracker', private)
        self.assertNotIn('bt-tracker', options_for(json={'link': 'https://example.org/big.iso'}))

    def test_old_downloads_and_orphans_erased(self):
        week_old = time.time() - self.settings.keep_days * 86400 - 60
        old = {self.add_job('Alex', s) for s in ('complete', 'cancelled', 'error')}
        recent, running = self.add_job('Alex', 'complete'), self.add_job('Alex', 'downloading')
        for job_id in old | {running}:
            self.relay.store.update(job_id, created=week_old, finished=week_old if job_id in old else None)
        self.relay.store.update(recent, finished=time.time())
        orphan = self.settings.staging_dir / ('c' * 32)
        orphan.mkdir(exist_ok=True)
        os.utime(orphan, (0, 0))

        self.relay.tidy()
        self.assertEqual({j['id'] for j in self.relay.store.all()}, {recent, running})
        self.assertIn(('cleanup', ('c' * 32,)), self.engine.calls)
        for job_id in old:
            self.assertIn(('cleanup', (job_id,)), self.engine.calls)

    def test_forwarded_port_rotation_and_strays(self):
        starts = []
        self.engine.forward = 50001

        def fake_start():
            starts.append(self.engine.health['forward'])
            self.engine.token = 'test-token'
            self.engine.forward = self.engine.health['forward']

        self.engine.start = fake_start
        for port in (50001, None, 50002, 50002):
            self.engine.check_health = lambda port=port: {'vpn_ready': True, 'forward': port}
            self.relay.health_at = 0
            self.relay.tick()
        self.assertEqual(starts, [50002])

        job_id = self.add_job('Alex', 'downloading')
        self.relay.store.update(job_id, gid='1' * 16)
        held = {
            'aria2.tellActive': [{'gid': '1' * 16}, {'gid': '2' * 16}],
            'aria2.tellWaiting': [{'gid': '3' * 16, 'following': '1' * 16}],
            'aria2.tellStopped': [{'gid': '4' * 16}],
        }
        self.engine.rpc = lambda method, *params: held.get(method, 'OK')
        self.relay.forget_strays()
        removed = {params[0] for method, params in self.engine.calls if method == 'aria2.forceRemove'}
        self.assertEqual(removed, {'2' * 16, '4' * 16})

    def test_location_picker_optional_and_switching(self):
        self.sign_in('Alex')
        # Unconfigured locations -> available=False cleanly.
        empty = self.client.get('/api/location', environ_base=LAN).get_json()
        self.assertFalse(empty['available'])
        self.assertEqual(self.client.post('/api/location', json={'slug': 'ch'}, headers=POST, environ_base=LAN).status_code, 400)

        # Configure two locations on the engine -> available=True and switch works.
        self.engine._locations = [
            {'slug': 'switzerland', 'endpoint': '198.51.100.1:51820'},
            {'slug': 'netherlands', 'endpoint': '198.51.100.2:51820'},
        ]
        self.engine._active_location = 'switzerland'
        avail = self.client.get('/api/location', environ_base=LAN).get_json()
        self.assertTrue(avail['available'])
        self.assertEqual(avail['active'], 'switzerland')
        self.assertEqual(avail['locations'][0]['label'], 'Switzerland')

        sw = self.client.post('/api/location', json={'slug': 'netherlands'}, headers=POST, environ_base=LAN)
        self.assertEqual(sw.status_code, 200)
        self.assertEqual(sw.get_json()['active'], 'netherlands')
        self.assertIn(('location_set', ('netherlands',)), self.engine.calls)


if __name__ == '__main__':
    unittest.main()
