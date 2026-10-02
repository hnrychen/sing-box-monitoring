import copy
import tempfile
import time
import unittest
from unittest.mock import patch
import exporter


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.service = dict(name='sing-box', inbounds={'ss': {'type': 'shadowsocks', 'port': 10443}}, outbounds=['relay'])
        self.collector = exporter.Collector(dict(services=[self.service], source_mode='raw', max_sources=2))
        self.snapshot = dict(uploadTotal=100, downloadTotal=200, memory=1024, connections=[
            dict(id='a', metadata=dict(type='shadowsocks/ss', network='tcp', sourceIP='192.0.2.1'),
                 upload=100, download=200, start='2026-10-01T00:00:00Z', chains=['relay'])])

    def test_exact_totals_and_baseline(self):
        self.collector.update(self.service, self.snapshot)
        state = self.collector.state['sing-box']
        self.assertEqual(state['totals'], (100, 200))
        self.assertEqual(sum(v[0] for v in state['observed'].values()), 0)
        self.assertIn('protocol="shadowsocks"', self.collector.render().decode())

    def test_delta_and_departed_connections(self):
        self.collector.update(self.service, self.snapshot)
        second = copy.deepcopy(self.snapshot)
        second['uploadTotal'] = 170
        second['connections'][0]['upload'] = 150
        self.collector.update(self.service, second)
        self.assertEqual(sum(v[0] for v in self.collector.state['sing-box']['observed'].values()), 50)
        second['connections'] = []
        second['uploadTotal'] = 300
        self.collector.update(self.service, second)
        self.assertEqual(self.collector.state['sing-box']['totals'][0], 300)
        self.assertEqual(sum(v[0] for v in self.collector.state['sing-box']['observed'].values()), 50)

    def test_resets_do_not_generate_negative_counters(self):
        self.collector.update(self.service, self.snapshot)
        self.snapshot.update(uploadTotal=5, downloadTotal=0, connections=[])
        self.collector.update(self.service, self.snapshot)
        self.assertEqual(self.collector.state['sing-box']['resets'], 1)

    def test_source_label_cap(self):
        self.assertEqual(self.collector.source('192.0.2.1'), '192.0.2.1')
        self.assertEqual(self.collector.source('192.0.2.2'), '192.0.2.2')
        self.assertEqual(self.collector.source('192.0.2.3'), 'other')
        self.assertEqual(self.collector.source('garbage'), 'unknown')

    def test_subnet_privacy(self):
        self.collector.config['source_mode'] = 'subnet'
        self.assertEqual(self.collector.source('192.0.2.3'), '192.0.2.0/24')

    def test_failure_omits_stale_traffic(self):
        self.collector.update(self.service, self.snapshot)
        with patch.object(exporter, 'fetch', side_effect=OSError('private secret')):
            self.collector.poll(self.service)
        text = self.collector.render().decode()
        self.assertIn('singbox_up{service="sing-box"} 0', text)
        self.assertNotIn('singbox_traffic_bytes_total', text)

    def test_stale_success_becomes_unhealthy(self):
        self.collector.update(self.service, self.snapshot, now=time.time() - 100)
        self.assertIn('singbox_up{service="sing-box"} 0', self.collector.render().decode())

    def test_escape_labels(self):
        self.assertEqual(exporter.labels({'source': 'a"\\\nb'}), '{source="a\\"\\\\\\nb"}')

    def test_connection_budget_rejects_snapshot(self):
        self.collector.config['max_connections'] = 0
        with self.assertRaises(ValueError):
            self.collector.update(self.service, self.snapshot)

    def test_persistent_source_slots(self):
        import json
        import pathlib
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'slots.json'
            path.write_text(json.dumps(['192.0.2.1', '192.0.2.2']))
            config = dict(self.collector.config, source_state=str(path))
            other = exporter.Collector(config)
            self.assertEqual(other.source('192.0.2.3'), 'other')

    def test_arbitrary_configured_protocol(self):
        self.service['inbounds'] = {'quic-in': {'type': 'tuic'}}
        self.snapshot['connections'][0]['metadata']['type'] = 'tuic/quic-in'
        self.snapshot['connections'][0]['metadata']['network'] = 'udp'
        self.collector.update(self.service, self.snapshot)
        text = self.collector.render().decode()
        self.assertIn('protocol="tuic",inbound="quic-in",network="udp"', text)

    def test_route_cardinality_limit(self):
        self.service['inbounds'] = {}
        self.service['outbounds'] = [f'out-{i}' for i in range(300)]
        self.snapshot['connections'] = []
        for i in range(300):
            self.snapshot['connections'].append(dict(id=str(i), metadata=dict(type=f'p-{i}/in-{i}', network='tcp', sourceIP='192.0.2.1'),
                                                    upload=1, download=2, chains=[f'out-{i}']))
        self.collector.update(self.service, self.snapshot)
        self.assertLessEqual(len(self.collector.state['sing-box']['groups']), 129)
        self.collector.update(self.service, self.snapshot)
        self.assertLessEqual(len(self.collector.state['sing-box']['observed']), 129)
        self.assertEqual(sum(v[0] for v in self.collector.state['sing-box']['groups'].values()), 300)
        self.assertLessEqual(len(self.collector.state['sing-box']['source_protocols']), 129)
        self.assertEqual(sum(self.collector.state['sing-box']['source_protocols'].values()), 300)

    def test_source_protocol_counts_reconcile(self):
        self.service['inbounds']['hy2'] = dict(type='hysteria2', port=8443)
        second = copy.deepcopy(self.snapshot['connections'][0])
        second.update(id='b', metadata=dict(type='hysteria2/hy2', network='udp', sourceIP='192.0.2.1'))
        self.snapshot['connections'].append(second)
        self.collector.update(self.service, self.snapshot)
        groups = self.collector.state['sing-box']['source_protocols']
        self.assertEqual(groups[('shadowsocks', '192.0.2.1')], 1)
        self.assertEqual(groups[('hysteria2', '192.0.2.1')], 1)
        self.assertEqual(sum(groups.values()), self.collector.state['sing-box']['connections'])
        self.assertIn('singbox_source_protocol_connections', self.collector.render().decode())

    def test_failed_update_keeps_previous_counter_state(self):
        self.collector.update(self.service, self.snapshot)
        bad = copy.deepcopy(self.snapshot)
        bad['connections'][0]['upload'] = 150
        bad['connections'].append(dict(metadata={}))
        with self.assertRaises(KeyError):
            self.collector.update(self.service, bad)
        self.assertEqual(sum(v[0] for v in self.collector.state['sing-box']['observed'].values()), 0)


if __name__ == '__main__':
    unittest.main()
