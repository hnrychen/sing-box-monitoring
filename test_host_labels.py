import json
import re
import unittest
from host_labels import apply_labels, selector_query


class HostLabelTests(unittest.TestCase):
    def test_selector_retains_raw_value(self):
        query = selector_query('up', 'instance', {'server-1': '🇸🇬 server-1'})
        self.assertIn('"display_host", "🇸🇬 server-1"', query)
        self.assertIn('"instance", ' + json.dumps('^server\\-1$'), query)

    def test_panels_preserve_queries_and_layout(self):
        from build_dashboard import dashboard
        labels = {'proxy': '🇭🇰 proxy'}
        after = apply_labels(dashboard, labels)
        for original, panel in zip(dashboard['panels'], after['panels']):
            for first, second in zip(original.get('targets', []), panel.get('targets', [])):
                if 'hostLabelBaseExpr' in second:
                    self.assertEqual(first['expr'], second['hostLabelBaseExpr'])
                    self.assertEqual(first['legendFormat'], second['hostLabelBaseLegend'])
                    self.assertIn('{{display_host}}', second['legendFormat'])
                else:
                    self.assertEqual(first, second)
            self.assertEqual(original['gridPos'], panel['gridPos'])
        self.assertEqual(after, apply_labels(after, labels))
        self.assertEqual(after['refresh'], '5s')
        host = next(v for v in after['templating']['list'] if v['name'] == 'host')
        self.assertIn('(?<text>', host['regex'])
        self.assertIn('(?<value>', host['regex'])

    def test_node_chain_and_current_value_preserved(self):
        dashboard = dict(templating=dict(list=[dict(name=name,current=dict(text='node1',value='node1')) for name in ('nodename','node')]),panels=[])
        after = apply_labels(dashboard, {'node1': '🇭🇰 node1'})
        for v in after['templating']['list']:
            self.assertEqual(v['current'], dict(text='🇭🇰 node1',value='node1'))
        self.assertIn('nodename="$nodename"', after['templating']['list'][1]['query']['query'])

    def test_alias_cannot_replace_hostname(self):
        for labels in ({'x': '🇸🇬'}, {'x': '🇸🇬 y'}, {'x': '🇸🇬 $ x'}):
            with self.assertRaises(ValueError):
                apply_labels({},labels)


if __name__ == '__main__':
    unittest.main()
