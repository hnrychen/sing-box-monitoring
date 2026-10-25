import copy
import unittest
from build_dashboard import dashboard
from host_labels import apply_labels
from publish_live_activity import patch_dashboard


class LiveActivityTests(unittest.TestCase):
    def test_targeted_patch_is_idempotent_and_preserves_customizations(self):
        original = copy.deepcopy(dashboard)
        original['links'] = [{'title': 'custom', 'url': '/custom'}]
        original['version'] = 99
        source = next(p for p in original['panels'] if p['title'] == 'Source connections')
        source['description'] = 'custom source description'
        live = next(p for p in original['panels'] if p['title'] == 'Live destinations')
        live['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options='Host'),
            properties=[dict(id='mappings', value=[dict(type='value', options={'server': {'text': 'flag server'}})])]))
        original['templating']['list'] = [v for v in original['templating']['list'] if v['name'] != 'activity']
        mean = next(p for p in original['panels'] if p['title'] == 'TCP RTT · socket-weighted mean')
        mean['targets'][0]['expr'] = mean['targets'][0]['expr'].replace('singbox_tcp_client_rtt_', 'singbox_tcp_rtt_')
        before = copy.deepcopy(original)
        patched = patch_dashboard(original)
        self.assertEqual(original, before)
        self.assertEqual(patched, patch_dashboard(patched))
        self.assertEqual(patched['links'], original['links'])
        self.assertEqual(patched['version'], 99)
        self.assertEqual(next(p for p in patched['panels'] if p['title'] == 'Source connections'), source)
        self.assertEqual(next(p for p in patched['panels'] if p['title'] == 'Live destinations')['fieldConfig']['overrides'], live['fieldConfig']['overrides'])
        self.assertIn('singbox_tcp_client_rtt_', next(p for p in patched['panels'] if p['title'] == mean['title'])['targets'][0]['expr'])

    def test_missing_panel_prevents_partial_publication(self):
        incomplete = copy.deepcopy(dashboard)
        incomplete['panels'] = [p for p in incomplete['panels'] if p['title'] != 'Live destinations']
        with self.assertRaises(ValueError):
            patch_dashboard(incomplete)

    def test_flag_label_base_queries_are_updated_and_stay_updated(self):
        labels = {'hk-yunyoo': '🇭🇰 hk-yunyoo'}
        original = apply_labels(dashboard, labels)
        titles = ('TCP RTT · socket-weighted mean', 'TCP RTT · current p95 / maximum',
                  'TCP RTT sample coverage', 'TCP sample last-ACK age')
        for panel in original['panels']:
            if panel['title'] not in titles:
                continue
            for target in panel['targets']:
                for field in ('expr', 'hostLabelBaseExpr'):
                    target[field] = target[field].replace('singbox_tcp_client_rtt_', 'singbox_tcp_rtt_')
                    target[field] = target[field].replace('singbox_tcp_client_rtt_ack_age_max_seconds',
                                                          'singbox_tcp_ack_age_max_seconds')
        updated = patch_dashboard(original)
        self.assertEqual(updated, patch_dashboard(updated))
        self.assertEqual(updated, apply_labels(updated, labels))
        for panel in updated['panels']:
            if panel['title'] not in titles:
                continue
            for target in panel['targets']:
                self.assertNotIn('singbox_tcp_rtt_', target['expr'])
                self.assertNotIn('singbox_tcp_rtt_', target['hostLabelBaseExpr'])
                self.assertEqual('singbox_tcp_client_rtt_' in target['expr'],
                                 'singbox_tcp_client_rtt_' in target['hostLabelBaseExpr'])

if __name__ == '__main__':
    unittest.main()
