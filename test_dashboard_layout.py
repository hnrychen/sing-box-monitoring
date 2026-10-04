import copy
import unittest

from build_dashboard import dashboard
from dashboard_layout import update_layout


def panel(data, title):
    return next(p for p in data['panels'] if p['title'] == title)


def legacy():
    data = copy.deepcopy(dashboard)
    bands = []
    for low, high in ((1000, 1003), (1004, 1006)):
        owned = [p['gridPos'] for p in data['panels'] if low <= p['id'] <= high]
        bands.append((min(p['y'] for p in owned), max(p['y'] + p['h'] for p in owned)))
    data['panels'] = [p for p in data['panels'] if not 1000 <= p['id'] <= 1006]
    for start, end in sorted(bands, reverse=True):
        for p in data['panels']:
            if p['gridPos']['y'] >= end:
                p['gridPos']['y'] -= end - start
    items = data['panels']
    activity = panel(data, 'Service activity')
    service = panel(data, 'Service availability')
    tcp = panel(data, 'TCP collector availability')
    items.remove(service)
    items.remove(tcp)
    items[items.index(activity)] = service
    service['gridPos'] = dict(x=0, y=5, w=8, h=7)
    destination_index = items.index(panel(data, 'Active connection destinations'))
    items.insert(destination_index, tcp)
    tcp['gridPos'] = dict(x=16, y=63, w=8, h=7)
    panel(data, 'TCP RTT sample coverage')['gridPos'].update(w=8)
    panel(data, 'TCP sample last-ACK age')['gridPos'].update(x=8, w=8)
    health_index = items.index(panel(data, 'Service resources and collector health'))
    for p in items[health_index + 1:]:
        p['gridPos']['y'] -= 7
    return data


class DashboardLayoutTests(unittest.TestCase):
    def setUp(self):
        self.template = panel(dashboard, 'Service activity')

    def test_unique_ids_and_no_overlap(self):
        panels = dashboard['panels']
        self.assertEqual(len(panels), len({p['id'] for p in panels}))
        for i, left in enumerate(panels):
            a = left['gridPos']
            for right in panels[i + 1:]:
                b = right['gridPos']
                overlap = (a['x'] < b['x'] + b['w'] and b['x'] < a['x'] + a['w']
                           and a['y'] < b['y'] + b['h'] and b['y'] < a['y'] + a['h'])
                self.assertFalse(overlap, (left['title'], right['title']))

    def test_sections_and_stable_ids(self):
        section = None
        for p in dashboard['panels']:
            if p['type'] == 'row':
                section = p['title']
            if p['title'] in ('Service availability', 'TCP collector availability'):
                self.assertEqual(section, 'Service resources and collector health')
            if p['title'] == 'Service activity':
                self.assertEqual(section, 'Fleet overview')
        self.assertEqual(panel(dashboard, 'Service availability')['id'], 8)
        self.assertEqual(panel(dashboard, 'TCP collector availability')['id'], 27)

    def test_migration_idempotent_and_preserves_user_settings(self):
        before = legacy()
        before['links'] = [{'url': 'https://example.org'}]
        service = panel(before, 'Service availability')
        service['targets'][0]['expr'] = 'custom_query_with_display_hosts'
        service['fieldConfig']['userSetting'] = True
        one = update_layout(before, self.template)
        self.assertEqual(update_layout(one, self.template), one)
        self.assertEqual(one['links'], before['links'])
        for title in ('Service availability', 'TCP collector availability', 'sing-box CPU', 'Live destinations'):
            a, b = copy.deepcopy(panel(before, title)), copy.deepcopy(panel(one, title))
            a.pop('gridPos')
            b.pop('gridPos')
            self.assertEqual(a, b)
        self.assertEqual(panel(before, 'sing-box CPU')['gridPos']['y'], 82)  # input was not mutated
        self.assertEqual(panel(one, 'sing-box CPU')['gridPos']['y'], 89)

    def test_datasource_and_host_flags_preserved(self):
        before = legacy()
        ds = dict(type='prometheus', uid='existing-source')
        panel(before, 'Service availability')['datasource'] = ds
        flags = dict(matcher=dict(id='byRegexp', options='^(Host|instance|nodename)$'),
                     properties=[dict(id='mappings', value=[dict(type='value', options={'test': {'text': 'flag test'}})])])
        panel(before, 'Service uptime')['fieldConfig']['overrides'].append(flags)
        activity = panel(update_layout(before, self.template), 'Service activity')
        self.assertEqual(activity['datasource'], ds)
        self.assertTrue(all(t['datasource'] == ds for t in activity['targets']))
        self.assertIn(flags, activity['fieldConfig']['overrides'])

    def test_conflicting_content_rejected(self):
        before = legacy()
        before['panels'].append(dict(id=902, title='User panel'))
        with self.assertRaises(ValueError):
            update_layout(before, self.template)
        before = legacy()
        before['panels'].append(dict(id=999, title='User section', type='row'))
        with self.assertRaises(ValueError):
            update_layout(before, self.template)

    def test_activity_grain_units_and_filter_scope(self):
        targets = self.template['targets']
        self.assertEqual(len(targets), 4)
        for target in targets:
            self.assertTrue(target['instant'])
            self.assertFalse(target['range'])
            self.assertIn('instance=~"$host",service=~"$service"', target['expr'])
            self.assertNotIn('$protocol', target['expr'])
        for target in targets[1:3]:
            self.assertIn('sum by(instance,service)', target['expr'])
            self.assertIn('rate(singbox_traffic_bytes_total', target['expr'])
        units = {o['matcher']['options']: next(p['value'] for p in o['properties'] if p['id'] == 'unit')
                 for o in self.template['fieldConfig']['overrides']}
        self.assertEqual(units['Upload'], 'Bps')
        self.assertEqual(units['Download'], 'Bps')
        self.assertEqual(units['Uptime'], 's')

    def test_activity_queries_have_matching_join_labels(self):
        for target in self.template['targets']:
            self.assertTrue(target['expr'].startswith(('max by(instance,service)', 'sum by(instance,service)')))
        current = copy.deepcopy(dashboard)
        panel(current, 'Service activity')['targets'][3]['expr'] = 'singbox_process_uptime_seconds'
        updated = update_layout(current, self.template)
        self.assertEqual(panel(updated, 'Service activity')['targets'], self.template['targets'])
        self.assertEqual(update_layout(updated, self.template), updated)


if __name__ == '__main__':
    unittest.main()
