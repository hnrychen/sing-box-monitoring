import copy
import unittest

from build_dashboard import dashboard, network_templates
from probe_table import update_probe_table


class ProbeTableTests(unittest.TestCase):
    def setUp(self):
        self.template = next(p for p in network_templates if p['id'] == 1001)

    def test_endpoint_columns_share_only_host_protocol_grain(self):
        targets = self.template['targets']
        self.assertEqual([t['refId'] for t in targets], list('ABCDEFGHI'))
        for target in targets:
            self.assertTrue(target['instant'])
            self.assertFalse(target['range'])
            self.assertIn('by(instance,protocol)', target['expr'])
            self.assertNotIn('by(instance,service', target['expr'])
            self.assertNotIn('by(instance,protocol,target', target['expr'])
            for variable in ['$host', '$service', '$protocol']:
                self.assertIn(variable, target['expr'])
        for index, check in [(0, 'cloudflare'), (4, 'chatgpt')]:
            for target in targets[index:index+4]:
                self.assertIn('probe="' + check + '"', target['expr'])
            self.assertIn('result_fresh', targets[index]['expr'])
            self.assertIn('singbox_probe_success', targets[index+1]['expr'])
            self.assertIn('== 1', targets[index+1]['expr'])
            self.assertIn('max by', targets[index+2]['expr'])
        organize = self.template['transformations'][-1]['options']
        self.assertEqual(organize['renameByName']['Value #A'], 'Cloudflare')
        self.assertEqual(organize['renameByName']['Value #E'], 'ChatGPT')
        self.assertTrue(organize['excludeByName']['Value #I'])
        self.assertEqual(set(organize['renameByName']), {'instance','protocol'} | {'Value #' + x for x in 'ABCDEFGH'})

    def test_patch_preserves_flags_datasource_geometry_and_every_other_panel(self):
        before = copy.deepcopy(dashboard)
        panel = next(p for p in before['panels'] if p['id'] == 1001)
        panel['gridPos'] = dict(x=0, y=999, w=24, h=9)
        panel['datasource'] = dict(type='prometheus', uid='existing-private-ds')
        panel['custom_note'] = 'keep this'
        flags = dict(matcher=dict(id='byRegexp', options='^(Host|instance|nodename)$'), properties=[])
        panel['fieldConfig']['overrides'].append(flags)
        original = copy.deepcopy(before)
        after = update_probe_table(before, self.template)
        self.assertEqual(before, original)
        self.assertEqual(update_probe_table(after, self.template), after)
        for old, new in zip(before['panels'], after['panels']):
            if old['id'] != 1001:
                self.assertEqual(old, new)
            else:
                self.assertEqual(old['gridPos'], new['gridPos'])
                self.assertEqual(new['custom_note'], 'keep this')
                self.assertIn(flags, new['fieldConfig']['overrides'])
                for target in new['targets']:
                    self.assertEqual(target['datasource'], old['datasource'])
        # Explicit new aliases replace old aliases, without accumulating overrides.
        labelled = copy.deepcopy(self.template)
        new_flags = dict(flags, properties=[dict(id='custom.width', value=75)])
        labelled['fieldConfig']['overrides'].append(new_flags)
        replaced = update_probe_table(after, labelled)
        self.assertEqual(update_probe_table(replaced, labelled), replaced)
        overrides = next(p for p in replaced['panels'] if p['id'] == 1001)['fieldConfig']['overrides']
        self.assertIn(new_flags, overrides)
        self.assertNotIn(flags, overrides)

    def test_ambiguous_or_missing_table_is_rejected(self):
        for panels in [[], [dict(id=1001,title='Other',type='table')], [self.template,self.template]]:
            with self.assertRaises(ValueError):
                update_probe_table(dict(panels=panels), self.template)
