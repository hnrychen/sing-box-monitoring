import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from ipinfo_exporter import Enrichment, metadata, public_ip


class IpinfoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        (self.root / 'token').write_text('test-not-a-real-token')
        self.config = dict(token_file=str(self.root / 'token'), cache_file=str(self.root / 'cache'), max_ips=2)
        self.worker = Enrichment(self.config)

    def test_private_and_placeholder_ips_not_sent(self):
        for ip in ('hidden', 'other', 'unknown', '10.0.0.1', '127.0.0.1', '192.0.2.1', '224.0.0.1', 'fc00::1'):
            self.assertIsNone(public_ip(ip))
        self.assertEqual(public_ip('::ffff:8.8.8.8'), '8.8.8.8')
        self.assertEqual(public_ip('2001:4860:4860::8888'), '2001:4860:4860::8888')

    def test_metadata_validation(self):
        for values in (dict(ip='1.1.1.1'), dict(ip='8.8.8.8', country_code='USA'), dict(ip='8.8.8.8', asn='not-asn')):
            with self.assertRaises(ValueError):
                metadata('8.8.8.8', values)
        self.assertEqual(metadata('8.8.8.8', dict(ip='8.8.8.8', as_name='a\nb'))['as_name'], 'ab')

    @patch('ipinfo_exporter.time.sleep')
    def test_cached_lookup_and_restart(self, _):
        self.worker.discover = lambda: ['8.8.8.8']
        calls = []
        self.worker.get_json = lambda request, budget: calls.append(request) or dict(ip='8.8.8.8', country_code='US', asn='AS15169', as_name='Google LLC')
        self.worker.cycle(1000)
        self.worker.cycle(1100)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('test-not-a-real-token', self.worker.render(1100).decode())
        self.assertIn('AS15169', self.worker.render(1100).decode())
        restarted = Enrichment(self.config)
        self.assertEqual(restarted.cache['8.8.8.8']['data']['country_code'], 'US')
        restarted.current = ['8.8.8.8']
        self.assertNotIn('source_ipinfo_info{', restarted.render(1000 + 2592001).decode())

    @patch('ipinfo_exporter.time.sleep')
    def test_failure_backoff_and_previous_good_attribution(self, _):
        self.worker.discover = lambda: ['8.8.8.8']
        self.worker.cache['8.8.8.8'] = dict(data=dict(country_code='US', asn='AS15169', as_name='Google'), checked=1, retry=0, failures=0)
        def fail(*_):
            raise urllib.error.HTTPError('https://api.ipinfo.io/lite/8.8.8.8', 429, '', {}, None)
        self.worker.get_json = fail
        self.worker.cycle(700000)
        self.worker.cycle(700060)
        self.assertEqual(self.worker.requests, 1)
        self.assertEqual(self.worker.errors, 1)
        self.assertIn('AS15169', self.worker.render(700060).decode())

    def test_unavailable_prometheus_does_not_drop_cache(self):
        self.worker.cache['8.8.8.8'] = dict(data=dict(country_code='US', asn='AS15169', as_name='Google'), checked=1000, retry=0, failures=0)
        self.worker.current = ['8.8.8.8']
        self.worker.discover = lambda: (_ for _ in ()).throw(ValueError('failed'))
        self.worker.cycle(1001)
        self.assertIn('AS15169', self.worker.render(1001).decode())
        self.assertIn('source_ipinfo_discovery_up{} 0', self.worker.render(1001).decode())

    @patch('ipinfo_exporter.time.sleep')
    def test_cache_has_no_count_cap_and_expires_only_old_inactive(self, _):
        for ip in ('8.8.8.8', '1.1.1.1'):
            self.worker.cache[ip] = dict(data=None, checked=1000, retry=0, failures=0)
        self.worker.discover = lambda: ['9.9.9.9']
        self.worker.get_json = lambda *_: dict(ip='9.9.9.9', country_code='US', asn='AS19281')
        self.worker.cycle(1100)
        self.assertEqual(len(self.worker.cache), 3)
        self.assertIn('9.9.9.9', self.worker.cache)
        self.worker.cycle(1100 + 604801)
        self.assertNotIn('8.8.8.8', self.worker.cache)
        self.assertIn('9.9.9.9', self.worker.cache)

    def test_redirect_and_remote_prometheus_refused(self):
        with self.assertRaises(ValueError):
            Enrichment(dict(self.config, prometheus_url='http://example.com:9090'))
        from ipinfo_exporter import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, None, None, 'https://evil.example'))

    def test_discovery_budget_and_deduplication(self):
        ips = ['8.8.8.8', '8.8.8.8', '1.1.1.1', 'other', '10.0.0.1']
        def response(*_):
            return dict(status='success', data=dict(resultType='vector', result=[dict(metric=dict(source_ip=ip)) for ip in ips]))
        self.worker.get_json = response
        self.assertEqual(self.worker.discover(), ['1.1.1.1', '8.8.8.8'])
        ips.append('9.9.9.9')
        self.assertEqual(self.worker.discover(), ['1.1.1.1', '8.8.8.8', '9.9.9.9'])

    def test_discovery_and_restart_over_legacy_ip_limit(self):
        ips = [f'2606:4700::{i:x}' for i in range(1, 1001)]
        self.worker.get_json = lambda *_: dict(status='success', data=dict(resultType='vector',
            result=[dict(metric=dict(source_ip=ip)) for ip in ips]))
        self.assertEqual(len(self.worker.discover()), 1000)
        self.worker.cache = {ip: dict(data=None, checked=1000, retry=0, failures=0) for ip in ips}
        self.worker.save()
        self.assertEqual(len(Enrichment(self.config).cache), 1000)

    def test_scrapes_never_make_external_requests(self):
        self.worker.get_json = lambda *_: self.fail('HTTP call inside scrape')
        for _ in range(20):
            self.worker.render(1000)
        self.assertEqual(self.worker.requests, 0)

    @patch('ipinfo_exporter.time.sleep')
    def test_fast_pacing_and_cold_priority(self, sleep):
        self.assertEqual(self.worker.discovery_interval, 2)
        self.worker.lookup_batch = 1
        self.worker.discover = lambda: ['1.1.1.1', '8.8.8.8']
        self.worker.cache['1.1.1.1'] = dict(data=dict(country_code='US', asn='', as_name=''), checked=1, retry=0, failures=0)
        calls = []
        self.worker.get_json = lambda request, budget: calls.append(request.full_url) or dict(ip='8.8.8.8', country_code='US')
        self.worker.cycle(700000)
        self.assertEqual(calls, ['https://api.ipinfo.io/lite/8.8.8.8'])
        sleep.assert_called_once_with(0.1)

    def test_invalid_pacing_refused(self):
        for settings in (dict(discovery_interval=0), dict(lookup_spacing=0), dict(lookup_batch=0)):
            with self.assertRaises(ValueError):
                Enrichment(dict(self.config, **settings))

    def test_flags_and_table_policy(self):
        import base64
        from build_dashboard import dashboard
        self.assertEqual(dashboard['title'], 'sing-box monitor')
        self.assertEqual(dashboard['refresh'], '5s')
        tables = [p for p in dashboard['panels'] if p['title'] in ('Source connections', 'Source active-connection bytes')]
        self.assertEqual(len(tables), 2)
        for table in tables:
            self.assertEqual(table['gridPos']['w'], 19 if table['title'] == 'Source connections' else 12)
            options = table['transformations'][-1]['options']
            self.assertLess(options['indexByName']['country_code'], options['indexByName']['source_ip'])
            field = next(o for o in table['fieldConfig']['overrides'] if o['matcher']['options'] == 'Flag')
            mappings = next(p['value'] for p in field['properties'] if p['id'] == 'mappings')[0]['options']
            for code in ('HK', 'CN', 'US', 'SG'):
                self.assertTrue(base64.b64decode(mappings[code]['text'].split(',', 1)[1]).startswith(b'\x89PNG\r\n\x1a\n'))
        connections = next(p for p in tables if p['title'] == 'Source connections')
        options = connections['transformations'][-1]['options']
        self.assertEqual(options['renameByName']['as_name'], 'Organization')
        self.assertTrue(options['excludeByName']['asn'])
        budget = next(p for p in dashboard['panels'] if p['title'] == 'Source IPs')
        bandwidth = next(p for p in dashboard['panels'] if p['title'] == 'Observed source bandwidth · sampled')
        self.assertEqual(budget['gridPos'], dict(x=19, y=38, w=5, h=8))
        self.assertEqual(bandwidth['gridPos'], dict(x=12, y=46, w=12, h=8))
        self.assertNotIn('topk', bandwidth['targets'][0]['expr'])
        self.assertNotIn('slots_limit', json.dumps(dashboard))

    def test_active_bytes_one_row_with_separate_direction_columns(self):
        from build_dashboard import dashboard
        table = next(p for p in dashboard['panels'] if p['title'] == 'Source active-connection bytes')
        self.assertEqual(len(table['targets']), 2)
        for target, direction in zip(table['targets'], ('upload', 'download')):
            self.assertIn(f'direction="{direction}"', target['expr'])
            self.assertIn('sum by(instance,source_ip)', target['expr'])
            self.assertNotIn('sum by(instance,source_ip,direction)', target['expr'])
            self.assertTrue(target['instant'])
            self.assertFalse(target['range'])
        self.assertEqual(table['transformations'][0]['id'], 'merge')
        options = table['transformations'][1]['options']
        self.assertEqual(options['renameByName']['Value #A'], 'Upload')
        self.assertEqual(options['renameByName']['Value #B'], 'Download')
        self.assertTrue(options['excludeByName']['direction'])
        self.assertEqual(table['fieldConfig']['defaults']['unit'], 'bytes')
        self.assertEqual(table['options']['sortBy'][0]['displayName'], 'Download')


if __name__ == '__main__':
    unittest.main()
