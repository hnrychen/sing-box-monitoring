import copy
import unittest
from unittest.mock import patch

import exporter


class SourceActivityTests(unittest.TestCase):
    def setUp(self):
        self.service = dict(name='core', inbounds={'ss': dict(type='shadowsocks')})
        # Exercise activity accounting even when connection_details is disabled.
        self.collector = exporter.Collector(dict(services=[self.service], source_mode='raw'))
        self.data = dict(uploadTotal=100, downloadTotal=200, memory=0, connections=[
            dict(id='old', upload=10, download=20, start='1970-01-01T00:01:40Z',
                 metadata=dict(type='shadowsocks/ss', network='tcp', sourceIP='192.0.2.1'))])

    def counts(self, now):
        with patch.object(exporter.time, 'time', return_value=now):
            lines = self.collector.render().decode().splitlines()
        return {name: sum(float(line.rsplit(' ', 1)[1]) for line in lines if line.startswith(name + '{'))
                for name in ('singbox_source_protocol_connections', 'singbox_source_protocol_recent_connections')}

    def test_restart_does_not_resurrect_old_or_unknown_activity(self):
        for start in ('1970-01-01T00:01:40Z', 'invalid', None):
            self.data['connections'][0]['start'] = start
            self.collector = exporter.Collector(dict(services=[self.service], source_mode='raw'))
            self.collector.update(self.service, self.data, now=1000)
            self.assertEqual(self.counts(1000)['singbox_source_protocol_connections'], 1)
            self.assertEqual(self.counts(1000)['singbox_source_protocol_recent_connections'], 0)

    def test_only_byte_changes_reactivate_and_boundary_is_inclusive(self):
        self.collector.update(self.service, self.data, now=1000)
        self.collector.update(self.service, self.data, now=1010)
        self.assertEqual(self.counts(1010)['singbox_source_protocol_recent_connections'], 0)
        self.data['connections'][0]['download'] += 1
        self.collector.update(self.service, self.data, now=1020)
        self.assertEqual(self.counts(1020)['singbox_source_protocol_recent_connections'], 1)
        self.collector.update(self.service, self.data, now=1320)
        self.assertEqual(self.counts(1320)['singbox_source_protocol_recent_connections'], 1)
        # Expiry is evaluated at render time, not just at the previous poll.
        self.assertEqual(self.counts(1320.001)['singbox_source_protocol_recent_connections'], 0)

    def test_recent_count_does_not_include_idle_siblings_and_closed_state_is_removed(self):
        self.collector.update(self.service, self.data, now=1000)
        self.data['connections'].append(dict(copy.deepcopy(self.data['connections'][0]), id='new'))
        self.collector.update(self.service, self.data, now=1010)
        self.assertEqual(self.counts(1010), dict(singbox_source_protocol_connections=2,
                                               singbox_source_protocol_recent_connections=1))
        self.data['connections'] = []
        self.collector.update(self.service, self.data, now=1020)
        self.assertEqual(self.collector.state['core']['source_activity'], {})
        self.assertEqual(self.counts(1020)['singbox_source_protocol_recent_connections'], 0)


if __name__ == '__main__':
    unittest.main()
