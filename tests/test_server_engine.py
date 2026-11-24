"""Pure unit tests for the Linux root helper (server_engine.py)."""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from relay import server_engine

SAMPLE_WG_CONF = """
# Example WireGuard profile
[Interface]
PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Address = 10.2.0.2/32, 2001:db8::2/128
DNS = 10.2.0.1

[Peer]
PublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 198.51.100.10:51820
PersistentKeepalive = 25
"""


class ServerEngineTests(unittest.TestCase):
    def test_parse_wg_conf_and_endpoint(self):
        conf = server_engine.parse_wg_conf(SAMPLE_WG_CONF)
        self.assertEqual(conf['interface']['address'], '10.2.0.2/32, 2001:db8::2/128')
        self.assertEqual(conf['peer']['endpoint'], '198.51.100.10:51820')
        self.assertEqual(server_engine.parse_endpoint('198.51.100.10:51820'), ('198.51.100.10', 51820))
        self.assertEqual(server_engine.parse_endpoint('[2001:db8::1]:51820'), ('2001:db8::1', 51820))

    def test_render_nm_profile(self):
        conf = server_engine.parse_wg_conf(SAMPLE_WG_CONF)
        rendered = server_engine.render_nm_profile(conf, 'vpn-wg0', '00000000-0000-4000-8000-000000000001', 'wg0')
        self.assertIn('id=vpn-wg0', rendered)
        self.assertIn('interface-name=wg0', rendered)
        self.assertIn('[wireguard-peer.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=]', rendered)
        self.assertIn('endpoint=198.51.100.10:51820', rendered)
        self.assertIn('address1=10.2.0.2/32', rendered)
        self.assertIn('dns=10.2.0.1;', rendered)

    def test_build_nft_policy(self):
        policy, sig = server_engine.build_nft_policy(
            table='relay_guard',
            uid=999,
            vpn_interface='wg0',
            exception='meta skuid 999 meta mark 51820 ip daddr 198.51.100.10 udp dport 51820 accept',
            replace_existing=True,
        )
        self.assertTrue(policy.startswith('delete table inet relay_guard\n'))
        self.assertIn('meta skuid 999 oifname "lo" ct state new counter reject', policy)
        self.assertIn(f'meta skuid 999 oifname != "wg0" oifname != "lo" counter reject comment "Relay {sig}"', policy)

        with self.assertRaises(RuntimeError):
            server_engine.build_nft_policy('bad;table', 999, 'wg0')
        with self.assertRaises(RuntimeError):
            server_engine.build_nft_policy('relay_guard', 999, 'wg0;bad')

    def test_safe_job_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = '0123456789abcdef' * 2
            self.assertEqual(server_engine.safe_job_dir(root, valid), root.resolve() / valid)
            for bad in ['../x', 'short', 'zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz']:
                with self.assertRaises(RuntimeError):
                    server_engine.safe_job_dir(root, bad)

    def test_load_engine_config_and_location_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            locs = tmp_path / 'locations'
            locs.mkdir()
            valid_key = base64.b64encode(b'0' * 32).decode('ascii')
            (locs / 'switzerland.conf').write_text(
                f'[Interface]\nPrivateKey = {valid_key}\nAddress = 10.2.0.2/32\n'
                f'[Peer]\nPublicKey = {valid_key}\nAllowedIPs = 0.0.0.0/0\nEndpoint = 198.51.100.10:51820\n',
                encoding='utf-8',
            )
            cfg_file = tmp_path / 'engine.json'
            cfg_file.write_text(json.dumps({
                'vpn_interface': 'wg1',
                'locations_dir': str(locs),
                'wireguard_backend': 'wg-quick',
                'wg_quick_config': str(locs / 'switzerland.conf'),
            }), encoding='utf-8')
            cfg = server_engine.load_engine_config(cfg_file)
            self.assertEqual(cfg['vpn_interface'], 'wg1')
            listed = server_engine.location_list(cfg)
            self.assertEqual(listed['count'], 1)
            self.assertEqual(listed['active'], 'switzerland')
            self.assertTrue(listed['locations'][0]['active'])


if __name__ == '__main__':
    unittest.main()
