import socket
import struct
import unittest
from unittest.mock import patch
import exporter
import tcpdiag


class TcpTests(unittest.TestCase):
    def setUp(self):
        self.service = dict(name='proxy', tcp_uid=1000, inbounds={
            'ss': dict(type='shadowsocks', port=10443),
            'hy2': dict(type='hysteria2', port=8443)})
        self.collector = exporter.Collector(dict(services=[self.service], source_mode='raw', max_sources=2, tcp_rtt=True))
        self.record = dict(local_port=10443, peer_port=45678, local_ip='192.0.2.10', peer_ip='198.51.100.1',
                           uid=1000, rtt_seconds=0.046, variation_seconds=0.003, ack_age_seconds=1)

    def test_mean_and_cumulative_snapshot_buckets(self):
        self.collector.tcp_update([self.record, dict(self.record, rtt_seconds=0.094)])
        group = self.collector.tcp['groups'][('proxy', 'shadowsocks', 'ss')]
        self.assertEqual(group['count'], 2)
        self.assertAlmostEqual(group['sum'] / group['count'], 0.07)
        self.assertEqual(group['buckets'][-1], 2)
        self.assertEqual(group['buckets'][3], 1)
        text = self.collector.render().decode()
        self.assertIn('# TYPE singbox_tcp_rtt_snapshot_bucket gauge', text)
        self.assertNotIn('protocol="hysteria2"', text)

    def test_empty_idle_and_uninitialized_are_not_zero_latency(self):
        self.collector.tcp_update([dict(self.record, ack_age_seconds=121), dict(self.record, rtt_seconds=0)])
        text = self.collector.render().decode()
        self.assertIn('singbox_tcp_established_sockets', text)
        self.assertNotIn('singbox_tcp_rtt_snapshot_sum', text)
        self.collector.tcp_update([])
        self.assertNotIn('singbox_tcp_rtt_max_seconds', self.collector.render().decode())

    def test_ownership_bind_and_ambiguous_listener_guard(self):
        self.collector.tcp_update([dict(self.record, uid=0)])
        self.assertEqual(self.collector.tcp['unmatched'], 1)
        self.service['inbounds']['ss']['listen'] = '192.0.2.11'
        self.collector.tcp_update([self.record])
        self.assertEqual(self.collector.tcp['unmatched'], 1)
        self.service['inbounds']['ss']['listen'] = '::'
        self.service['inbounds']['duplicate'] = dict(type='vless', port=10443)
        self.collector.tcp_update([self.record])
        self.assertEqual(self.collector.tcp['unmatched'], 1)

    def test_source_cardinality_and_privacy(self):
        self.collector.tcp_update([dict(self.record, peer_ip=f'2001:db8::{i:x}') for i in range(1000)])
        self.assertEqual(len(self.collector.tcp['sources']), 1000)
        self.assertNotIn('source_ip="other"', self.collector.render().decode())
        self.collector.config['source_mode'] = 'off'
        self.collector.tcp_update([self.record])
        text = self.collector.render().decode()
        self.assertNotIn('198.51.100.1', text)
        self.assertIn('source_ip="hidden"', text)

    def test_failure_and_staleness_omit_rtt(self):
        self.collector.tcp_update([self.record])
        with patch.object(tcpdiag, 'snapshot', side_effect=OSError('sensitive message')):
            self.collector.tcp_poll()
        text = self.collector.render().decode()
        self.assertIn('singbox_tcp_collector_up{} 0', text)
        self.assertNotIn('singbox_tcp_source_rtt', text)
        self.collector.tcp_update([self.record], now=1)
        self.assertNotIn('singbox_tcp_rtt_snapshot_sum', self.collector.render().decode())

    def test_decode_ipv4_and_mapped_ipv6_stable_offsets(self):
        for family, source, peer in ((socket.AF_INET, '192.0.2.10', '198.51.100.1'),
                                     (socket.AF_INET6, '::ffff:192.0.2.10', '::ffff:198.51.100.1')):
            payload = bytearray(72)
            payload[0:2] = bytes([family, 1])
            struct.pack_into('!HH', payload, 4, 10443, 12345)
            payload[8:24] = socket.inet_pton(family, source).ljust(16, b'\0')
            payload[24:40] = socket.inet_pton(family, peer).ljust(16, b'\0')
            struct.pack_into('=I', payload, 64, 1000)
            info = bytearray(104)
            struct.pack_into('=I', info, 56, 1234)
            struct.pack_into('=II', info, 68, 46000, 3000)
            decoded = tcpdiag.decode(payload + struct.pack('=HH', 108, 2) + info)
            self.assertEqual(decoded['peer_ip'], '198.51.100.1')
            self.assertEqual(decoded['local_port'], 10443)
            self.assertAlmostEqual(decoded['rtt_seconds'], 0.046)
            self.assertAlmostEqual(decoded['ack_age_seconds'], 1.234)

    def test_malformed_attributes_rejected(self):
        with self.assertRaises(ValueError):
            tcpdiag.decode(b'')
        payload = bytearray(72)
        payload[0:2] = bytes([socket.AF_INET, 1])
        with self.assertRaises(ValueError):
            tcpdiag.decode(payload + struct.pack('=HH', 0, 2))
        with self.assertRaises(ValueError):
            tcpdiag.snapshot([70000])

    @unittest.skipUnless(hasattr(socket, 'AF_NETLINK'), 'Linux SOCK_DIAG required')
    def test_kernel_dump_errors_are_rejected(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def bind(self, *args): pass
            def settimeout(self, *args): pass
            def sendto(self, packet, *args):
                self.sequence = struct.unpack_from('=I', packet, 8)[0]
            def recvmsg(self, *args):
                header = struct.pack('=IHHII', 20, kind, nlflags, self.sequence, 0)
                return header + struct.pack('=i', error), [], recvflags, (0, 0)
        for kind, nlflags, recvflags, error, exception in (
            (3, 0x10, 0, 0, ValueError), (2, 0, 0, -1, OSError),
            (3, 0, socket.MSG_TRUNC, 0, ValueError)):
            with self.subTest(kind=kind, flags=nlflags, error=error), patch.object(tcpdiag.socket, 'socket', return_value=FakeSocket()):
                with self.assertRaises(exception):
                    tcpdiag.snapshot([10443])

    @unittest.skipUnless(hasattr(socket, 'AF_NETLINK'), 'Linux SOCK_DIAG required')
    def test_real_unprivileged_loopback_tcp_info(self):
        for family, address in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
            try:
                listener = socket.socket(family, socket.SOCK_STREAM)
                listener.bind((address, 0))
            except OSError:
                if family == socket.AF_INET6:
                    continue
                raise
            with listener:
                listener.listen()
                with socket.create_connection((address, listener.getsockname()[1])) as client:
                    accepted, _ = listener.accept()
                    with accepted:
                        expected = struct.unpack_from('=I', accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104), 68)[0]
                        rows = tcpdiag.snapshot([listener.getsockname()[1]])
                        row = next(r for r in rows if r['peer_port'] == client.getsockname()[1])
                        self.assertEqual(row['rtt_seconds'], expected / 1_000_000)
                        with self.assertRaises(ValueError):
                            tcpdiag.snapshot([listener.getsockname()[1]], max_sockets=0)
                        with self.assertRaises(ValueError):
                            tcpdiag.snapshot([listener.getsockname()[1]], max_bytes=1)


if __name__ == '__main__':
    unittest.main()
