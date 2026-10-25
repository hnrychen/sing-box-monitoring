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

    def test_order_is_display_only_and_retains_dynamic_hosts(self):
        labels = {'second': '🇭🇰 second', 'first': '🇭🇰 first'}
        dashboard = dict(templating=dict(list=[dict(name='host', sort=1,
            current=dict(text='All', value='$__all'))]), panels=[])
        after = apply_labels(dashboard, labels, ['first', 'second'])
        variable = after['templating']['list'][0]
        query = variable['query']['query']
        self.assertIn('sort(', query)
        self.assertIn('label_replace(vector(1), "instance", "first"', query)
        self.assertIn('label_replace(vector(2), "instance", "second"', query)
        self.assertIn('or ((max by(instance)(singbox_up)) * 0 + 3)', query)
        self.assertEqual(variable['sort'], 0)
        self.assertEqual(variable['current'], dashboard['templating']['list'][0]['current'])
        self.assertEqual(after, apply_labels(after, labels, ['first', 'second']))
        for order in (['first', 'first'], ['missing']):
            with self.assertRaises(ValueError):
                apply_labels(dashboard, labels, order)


if __name__ == '__main__':
    unittest.main()
