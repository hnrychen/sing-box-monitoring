import copy
import unittest

from build_dashboard import (dashboard, source_panel, source_active, source_rtt, source_state,
                             source_ips_panel, source_quic, source_without_rtt,
                             source_quic_without_rtt, source_rtt_display)
from publish_source_tables import update_connections, update_description, update_tables


class SourceTableTests(unittest.TestCase):
    def test_source_ip_count_labels_do_not_imply_connections_or_slots(self):
        self.assertEqual(source_ips_panel['title'], 'Source IP counts')
        self.assertEqual([t['legendFormat'] for t in source_ips_panel['targets']], [
            '{{instance}} · Routed IPs', '{{instance}} · Retained IPs'])
        self.assertEqual([t['expr'] for t in source_ips_panel['targets']], [
            'singbox_exporter_source_ips_active{instance=~"$host"}',
            'singbox_exporter_source_ips_tracked{instance=~"$host"}'])
        self.assertIn('including active IPs and inactive IPs', source_ips_panel['description'])
        self.assertIn('Only the Host filter applies', source_ips_panel['description'])
        self.assertIn('RTT-only peers', source_ips_panel['description'])

    def test_current_identity_filters_and_four_disjoint_states(self):
        variable = next(v for v in dashboard['templating']['list'] if v['name'] == 'source_activity')
        self.assertEqual(variable['current']['value'], 'recent_connections')
        self.assertEqual({o['value'] for o in variable['options']}, {'connections', 'recent_connections'})
        self.assertIn('singbox_source_protocol_${source_activity}', source_active)
        targets = source_panel['targets']
        self.assertEqual([t['refId'] for t in targets], ['A', 'B', 'C'])
        for target in targets:
            self.assertTrue(target['instant'])
            self.assertFalse(target['range'])
            for variable in ('$host', '$service', '$protocol'):
                self.assertIn(variable, target['expr'])
            self.assertIn('source_ip_display', target['expr'])
            self.assertNotIn('topk(', target['expr'])
        self.assertIn('> 0', source_active)
        self.assertIn('> 0', source_rtt)
        self.assertIn(f'({source_active}) and on(instance,service,protocol,source_ip) ({source_rtt})', source_state)
        self.assertIn(f'({source_without_rtt}) and on(instance,service,protocol,source_ip) ({source_quic})', source_state)
        self.assertIn(f'({source_without_rtt}) unless on(instance,service,protocol,source_ip) ({source_quic})', source_state)
        self.assertIn(f'({source_rtt}) unless on(instance,service,protocol,source_ip) ({source_active})', source_state)
        self.assertEqual(source_panel['options']['sortBy'], [
            dict(displayName='State', desc=False), dict(displayName='Active', desc=True)])
        fields = {}
        for override in source_panel['fieldConfig']['overrides']:
            fields.setdefault(override['matcher']['options'], {}).update(
                {p['id']: p['value'] for p in override['properties']})
        self.assertEqual(fields['Mean RTT']['noValue'], 'No sample')
        self.assertEqual(fields['Mean RTT']['mappings'], [dict(type='value', options={'-1': dict(text='N/A')})])
        self.assertEqual(fields['Active']['noValue'], '—')
        self.assertEqual(fields['State']['mappings'][0]['options'], {
            '1': dict(text='Active + RTT'), '2': dict(text='Active / QUIC'),
            '3': dict(text='API active / no RTT'), '4': dict(text='TCP peer only')})
        self.assertIn('protocol=~"hysteria|hysteria2|tuic"', source_quic)
        self.assertEqual(source_rtt_display, f'({source_rtt}) or (({source_quic_without_rtt}) * 0 - 1)')
        self.assertIn(' * 0 - 1)', targets[1]['expr'])
        self.assertNotIn(' * 0 - 1)', targets[0]['expr'])

    def test_narrow_patch_preserves_flags_datasource_and_other_panels(self):
        before = copy.deepcopy(dashboard)
        panel = next(p for p in before['panels'] if p['title'] == 'Source connections')
        panel['targets'] = panel['targets'][:2]  # Old deployed A/B layout.
        panel['gridPos']['y'] = 999
        panel['datasource'] = dict(type='prometheus', uid='private-ds')
        panel['custom_note'] = 'preserve'
        flags = dict(matcher=dict(id='byRegexp', options='^(Host|instance|nodename)$'),
                     properties=[dict(id='custom.width', value=112)])
        panel['fieldConfig']['overrides'].append(flags)
        original = copy.deepcopy(before)
        after = update_connections(before, source_panel)
        self.assertEqual(before, original)
        self.assertEqual(after, update_connections(after, source_panel))
        for old, new in zip(before['panels'], after['panels']):
            if old['title'] != 'Source connections':
                self.assertEqual(old, new)
            else:
                self.assertEqual(new['gridPos'], old['gridPos'])
                self.assertEqual(new['id'], old['id'])
                self.assertEqual(new['custom_note'], 'preserve')
                self.assertIn(flags, new['fieldConfig']['overrides'])
                self.assertEqual([t['datasource'] for t in new['targets']], [old['datasource']] * 3)
        self.assertEqual(update_tables(after), after)

    def test_description_only_patch_preserves_live_queries_and_flags(self):
        before = copy.deepcopy(dashboard)
        panel = next(p for p in before['panels'] if p['title'] == 'Source connections')
        panel['targets'][0]['expr'] = 'live-label-rewrite'
        panel['fieldConfig']['overrides'].append(dict(matcher=dict(id='byRegexp', options='Host'), properties=[]))
        panel['description'] = 'old source behavior'
        after = update_description(before, source_panel)
        expected = copy.deepcopy(before)
        next(p for p in expected['panels'] if p['title'] == 'Source connections')['description'] = source_panel['description']
        self.assertEqual(after, expected)
        self.assertEqual(after, update_description(after, source_panel))

    def test_source_view_preserves_saved_selection_and_other_variables(self):
        before = copy.deepcopy(dashboard)
        variable = next(v for v in before['templating']['list'] if v['name'] == 'source_activity')
        variable['current'] = dict(text='All open sessions', value='connections')
        before['templating']['list'].append(dict(name='unrelated', current=dict(value='keep')))
        after = update_connections(before, source_panel)
        self.assertEqual(after['templating']['list'][-1], before['templating']['list'][-1])
        saved = next(v for v in after['templating']['list'] if v['name'] == 'source_activity')
        self.assertEqual(saved['current'], variable['current'])
        self.assertEqual([o['value'] for o in saved['options'] if o['selected']], ['connections'])
        self.assertEqual(after, update_connections(after, source_panel))

    def test_missing_ambiguous_or_wrong_panel_is_rejected(self):
        for panels in ([], [source_panel, source_panel], [dict(title='Source connections', type='stat')]):
            with self.assertRaises(ValueError):
                update_connections(dict(panels=panels), source_panel)


if __name__ == '__main__':
    unittest.main()
