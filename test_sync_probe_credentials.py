import copy
import unittest

from sync_probe_credentials import outbound, update


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.node = dict(type='hysteria2', server='192.0.2.1', port=8443,
                         password='new-test-password', obfs='salamander',
                         **{'obfs-password': 'test-obfs', 'skip-cert-verify': False,
                            'sni': '192.0.2.1', 'alpn': ['h3']})

    def test_tls_trust_is_current_and_verification_stays_enabled(self):
        old = outbound(self.node)
        old['password'] = 'old-test-password'
        old['tls']['certificate'] = ['stale-test-ca']
        config = dict(targets=[dict(instance='test', protocol='hysteria2', outbound=old)])
        self.assertEqual(update(config, [self.node]), [dict(instance='test', protocol='hysteria2')])
        self.assertEqual(config['targets'][0]['outbound'], outbound(self.node))
        self.assertNotIn('certificate', config['targets'][0]['outbound']['tls'])
        self.assertFalse(config['targets'][0]['outbound']['tls']['insecure'])

    def test_shadowsocks_and_other_config_untouched(self):
        config = dict(interval=60, checks=[dict(name='test')],
                      targets=[dict(instance='test', protocol='shadowsocks', outbound=dict(password='test'))])
        original = copy.deepcopy(config)
        self.assertEqual(update(config, [self.node]), [])
        self.assertEqual(config, original)

    def test_idempotent(self):
        config = dict(targets=[dict(instance='test', protocol='hysteria2', outbound=outbound(self.node))])
        self.assertEqual(update(config, [self.node]), [])

    def test_ambiguous_endpoint_fails(self):
        other = dict(self.node, password='different-test-password')
        with self.assertRaises(ValueError):
            update(dict(targets=[]), [self.node, other])

    def test_missing_endpoint_fails(self):
        config = dict(targets=[dict(instance='test', protocol='hysteria2', outbound=outbound(self.node))])
        with self.assertRaises(ValueError):
            update(config, [])

    def test_certificate_pin_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            outbound(dict(self.node, fingerprint='test-pin'))

    def test_vless_reality(self):
        node = dict(type='vless', server='192.0.2.2', port=443, uuid='test-uuid',
                    tls=True, servername='example.com', flow='xtls-rprx-vision',
                    **{'client-fingerprint': 'chrome',
                       'reality-opts': {'public-key': 'test-key', 'short-id': 'abcd'}})
        result = outbound(node)
        self.assertEqual(result['uuid'], 'test-uuid')
        self.assertEqual(result['tls']['reality']['public_key'], 'test-key')
        self.assertEqual(result['tls']['server_name'], 'example.com')


if __name__ == '__main__':
    unittest.main()
