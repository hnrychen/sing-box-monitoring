import copy
import unittest
from build_dashboard import dashboard, network_templates
from network_layout import update_network_layout
from test_dashboard_layout import legacy


class NetworkLayoutTests(unittest.TestCase):
    def test_owned_panels_correct_sections_and_all_filters(self):
        section = ''
        count = 0
        for panel in dashboard['panels']:
            if panel['type'] == 'row':
                section = panel['title']
            if 1001 <= panel['id'] <= 1006:
                count += 1
                self.assertEqual(section, 'End-to-end proxy checks' if panel['id'] <= 1003 else 'Client transport latency · TCP')
                for target in panel['targets']:
                    for name in ('$host', '$service', '$protocol'):
                        self.assertIn(name, target['expr'])
        self.assertEqual(count, 6)
        rows = [p['title'] for p in dashboard['panels'] if p['type'] == 'row']
        index = rows.index('Service resources and collector health')
        self.assertEqual(rows[index - 1], 'End-to-end proxy checks')

    def test_narrow_migration_preserves_current_queries_and_aliases(self):
        before = legacy()
        before['links'] = [{'url': 'https://example.org'}]
        original = copy.deepcopy(before)
        after = update_network_layout(before, network_templates)
        self.assertEqual(before, original)
        self.assertEqual(update_network_layout(after, network_templates), after)
        a = {p['id']: p for p in before['panels']}
        b = {p['id']: p for p in after['panels']}
        for identity, item in a.items():
            left, right = copy.deepcopy(item), copy.deepcopy(b[identity])
            left.pop('gridPos')
            right.pop('gridPos')
            self.assertEqual(left, right)
        self.assertEqual(after['links'], before['links'])

    def test_partial_migration_rejected(self):
        before = legacy()
        before['panels'].append(copy.deepcopy(network_templates[0]))
        with self.assertRaises(ValueError):
            update_network_layout(before, network_templates)

    def test_existing_probe_section_relocates_without_changing_settings(self):
        before = copy.deepcopy(dashboard)
        probe = [p for p in before['panels'] if 1000 <= p['id'] <= 1003]
        start = min(p['gridPos']['y'] for p in probe)
        for p in probe:
            before['panels'].remove(p)
            p['gridPos']['y'] -= start
            p['custom_note'] = 'retain this'
        for p in before['panels']:
            if p['gridPos']['y'] >= start + 19:
                p['gridPos']['y'] -= 19
        protocol = next(p for p in before['panels'] if p['title'] == 'Protocols, connections and routing')
        destination = protocol['gridPos']['y']
        for p in before['panels']:
            if p['gridPos']['y'] >= destination:
                p['gridPos']['y'] += 19
        for p in probe:
            p['gridPos']['y'] += destination
        index = before['panels'].index(protocol)
        before['panels'][index:index] = probe
        after = update_network_layout(before, network_templates)
        expected = copy.deepcopy(dashboard)
        for p in expected['panels']:
            if 1000 <= p['id'] <= 1003:
                p['custom_note'] = 'retain this'
        self.assertEqual(after, expected)
        self.assertEqual(update_network_layout(after, network_templates), after)
