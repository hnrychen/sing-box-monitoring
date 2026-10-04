import copy
import concurrent.futures
import threading
import time
import unittest
from unittest.mock import patch

import exporter
from connections_view import View, country_metadata, group_rows, source_label
from enable_domain_sniffing import enable
from publish_connections import insert_section


class ConnectionViewTests(unittest.TestCase):
    def setUp(self):
        self.service = dict(name='sing-box', inbounds={'ss': {'type': 'shadowsocks'}}, outbounds=['direct'])
        self.collector = exporter.Collector(dict(services=[self.service], source_mode='raw', connection_details=True, poll_interval=2))
        self.data = dict(uploadTotal=10, downloadTotal=20, memory=0, connections=[
            dict(id='test', metadata=dict(type='shadowsocks/ss', network='tcp', sourceIP='192.0.2.1',
                destinationIP='2001:db8::1', destinationPort='443', host='example.com'),
                upload=10, download=20, chains=['direct'], start='2026-10-01T00:00:00Z')])

    def test_metadata_is_live_json_and_never_metric_labels(self):
        self.collector.update(self.service, self.data)
        rows = group_rows({'proxy': self.collector.connection_snapshot()}, {})
        self.assertEqual(rows[0]['domain'], 'example.com')
        self.assertEqual(rows[0]['destination'], '[2001:db8::1]:443')
        self.assertEqual(rows[0]['upload_bytes'], 10)
        self.assertNotIn('example.com', self.collector.render().decode())
        self.assertNotIn('2001:db8::1', self.collector.render().decode())

    def test_closed_sessions_disappear_on_next_snapshot(self):
        self.collector.update(self.service, self.data)
        self.data['connections'] = []
        self.collector.update(self.service, self.data)
        self.assertEqual(self.collector.connection_snapshot()['rows'], [])

    def test_failure_never_serves_previous_destinations(self):
        self.collector.update(self.service, self.data)
        with patch.object(exporter, 'fetch', side_effect=OSError()):
            self.collector.poll(self.service)
        snapshot = self.collector.connection_snapshot()
        self.assertEqual(snapshot['rows'], [])
        with self.assertRaises(ValueError):
            group_rows({'proxy': snapshot}, {})

    def test_stale_and_empty_have_different_semantics(self):
        self.collector.update(self.service, self.data, now=time.time() - 100)
        self.assertFalse(self.collector.connection_snapshot()['services'][0]['healthy'])
        self.data['connections'] = []
        self.collector.update(self.service, self.data)
        self.assertEqual(group_rows({'proxy': self.collector.connection_snapshot()}, {}), [])

    def test_conservative_poll_interval_has_matching_freshness(self):
        self.collector.config['poll_interval'] = 30
        self.collector.update(self.service, self.data, now=time.time() - 20)
        self.assertEqual(len(group_rows({'proxy': self.collector.connection_snapshot()}, {})), 1)

    def test_shared_cache_and_filtered_host_failure(self):
        self.collector.update(self.service, self.data)
        snapshot = self.collector.connection_snapshot()
        view = View.__new__(View)
        view.clients = {'proxy': None, 'failed': None}
        view.lock = threading.Lock()
        view.last_fetch = 0
        view.snapshots, view.failures = {}, set()
        def fetch_snapshot(host):
            if host == 'failed':
                raise OSError()
            return snapshot
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            view.pool = pool
            with patch.object(view, 'fetch', side_effect=fetch_snapshot) as fetch:
                with self.assertRaises(ValueError):
                    view.rows({})
                self.assertEqual(len(view.rows({'var-host': ['proxy']})), 1)
                self.assertEqual(fetch.call_count, 2)

    def test_grouping_and_all_filters_keep_traffic_totals(self):
        second = copy.deepcopy(self.data['connections'][0])
        second['id'] = 'second'
        self.data['connections'].append(second)
        self.collector.update(self.service, self.data)
        snapshots = {'proxy': self.collector.connection_snapshot()}
        rows = group_rows(snapshots, {'var-host': ['proxy'], 'var-service': ['sing-box'], 'var-protocol': ['shadowsocks']})
        self.assertEqual((rows[0]['active'], rows[0]['upload_bytes'], rows[0]['download_bytes']), (2, 20, 40))
        self.assertEqual(group_rows(snapshots, {'var-host': ['another']}), [])
        self.assertEqual(group_rows(snapshots, {'var-service': ['another']}), [])
        self.assertEqual(group_rows(snapshots, {'var-protocol': ['vless']}), [])
        self.assertEqual(group_rows(snapshots, {'var-host': ['.*']}), rows)

    def test_unknown_domains_stay_blank_and_control_text_is_removed(self):
        self.data['connections'][0]['metadata']['host'] = 'example.com\n\x00'
        self.collector.update(self.service, self.data)
        self.assertEqual(self.collector.connection_snapshot()['rows'][0]['domain'], 'example.com')
        del self.data['connections'][0]['metadata']['host']
        self.collector.update(self.service, self.data)
        self.assertEqual(self.collector.connection_snapshot()['rows'][0]['domain'], '')

    def test_disabled_feature_does_not_keep_destination_rows(self):
        self.collector.config['connection_details'] = False
        self.collector.update(self.service, self.data)
        self.assertEqual(self.collector.state['sing-box']['details'], [])

    def test_country_flags_preserve_raw_identity_and_unknowns(self):
        countries = country_metadata(b'source_ipinfo_info{source_ip="8.8.8.8",country_code="US",as_name="Google LLC"} 1\n')
        self.assertEqual(source_label('8.8.8.8', countries), '\U0001f1fa\U0001f1f8 8.8.8.8')
        self.assertEqual(source_label('::ffff:8.8.8.8', countries), '\U0001f1fa\U0001f1f8 ::ffff:8.8.8.8')
        for address in ('192.0.2.1', 'other', 'hash-example'):
            self.assertEqual(source_label(address, countries), address)
        self.assertEqual(source_label('8.8.8.8', {'8.8.8.8': 'INVALID'}), '8.8.8.8')

    def test_country_metrics_validate_metadata_and_ignore_unrelated_series(self):
        raw = b'''source_ipinfo_info{source_ip="8.8.8.8",country_code="USA"} 1
source_ipinfo_info{source_ip="127.0.0.1",country_code="US"} 1
source_ipinfo_info{source_ip="9.9.9.9",country_code="US"} 0
source_ipinfo_lookup_timestamp_seconds{source_ip="8.8.8.8",country_code="US"} 1
source_ipinfo_info{source_ip="1.1.1.1",country_code="AU",as_name="Escaped \\"name\\""} 1
'''
        self.assertEqual(country_metadata(raw), {'1.1.1.1': 'AU'})

    def test_country_enrichment_failure_does_not_break_live_view(self):
        view = View.__new__(View)
        view.ipinfo_url = 'http://127.0.0.1:9120/metrics'
        with patch.object(view, 'local_client', create=True) as opener:
            opener.open.side_effect = OSError()
            self.assertEqual(view.fetch_countries(), {})

    def test_sniff_addition_is_idempotent_and_keeps_existing_rules(self):
        config = dict(route=dict(rules=[dict(domain_suffix=['example.org'], outbound='direct')]))
        old_rule = copy.deepcopy(config['route']['rules'][0])
        self.assertTrue(enable(config))
        self.assertEqual(config['route']['rules'][1], old_rule)
        self.assertFalse(enable(config))
        self.assertEqual(len(config['route']['rules']), 2)

    def test_targeted_dashboard_insertion_preserves_other_panels_and_is_idempotent(self):
        original = dict(panels=[dict(id=1, title='Client transport latency', gridPos=dict(y=50, h=20)),
            dict(id=2, title='Service resources and collector health', gridPos=dict(y=70, h=1)),
            dict(id=3, title='CPU', gridPos=dict(y=71, h=7), custom='user edit')])
        section = [dict(id=900, title='Active connection destinations', gridPos=dict(y=70, h=1)),
                   dict(id=901, title='Live destinations', gridPos=dict(y=71, h=10))]
        result = insert_section(original, section)
        self.assertEqual(result['panels'][-1]['custom'], 'user edit')
        self.assertEqual(result['panels'][-1]['gridPos']['y'], 82)
        self.assertEqual(result, insert_section(result, section))
        self.assertEqual(original['panels'][-1]['gridPos']['y'], 71)


if __name__ == '__main__':
    unittest.main()
