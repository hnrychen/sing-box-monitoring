import copy
import unittest
from normalize_inbound_tags import normalize


class InboundTagTests(unittest.TestCase):
    def test_only_listener_and_nested_inbound_selectors_change(self):
        before = dict(inbounds=[dict(type='shadowsocks', tag='ss-in', password='private', listen_port=10443)],
            outbounds=[dict(tag='ss-in', type='direct')],
            route=dict(rules=[dict(type='logical', rules=[dict(inbound=['ss-in', 'vless'], outbound='ss-in')])]),
            dns=dict(rules=[dict(inbound='ss-in', server='resolver')]))
        snapshot = copy.deepcopy(before)
        after, old = normalize(before)
        self.assertEqual(old, 'ss-in')
        self.assertEqual(before, snapshot)
        self.assertEqual(after['inbounds'][0], dict(type='shadowsocks', tag='shadowsocks-in', password='private', listen_port=10443))
        self.assertEqual(after['outbounds'], before['outbounds'])
        self.assertEqual(after['route']['rules'][0]['rules'][0], dict(inbound=['shadowsocks-in', 'vless'], outbound='ss-in'))
        self.assertEqual(after['dns']['rules'][0]['inbound'], 'shadowsocks-in')
        self.assertEqual(normalize(after)[0], after)

    def test_conflict_multiple_or_missing_tags_rejected(self):
        for inbounds in [[], [dict(type='shadowsocks')],
            [dict(type='shadowsocks',tag='ss-in'),dict(type='shadowsocks',tag='ss-other')],
            [dict(type='shadowsocks',tag='ss-in'),dict(type='vless',tag='shadowsocks-in')]]:
            with self.assertRaises(ValueError):
                normalize(dict(inbounds=inbounds))
