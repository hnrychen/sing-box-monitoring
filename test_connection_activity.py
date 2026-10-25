import copy
import unittest
from build_dashboard import dashboard
from host_labels import apply_labels
from publish_connection_activity import patch_dashboard, COUNTS


class ConnectionActivityTests(unittest.TestCase):
    def legacy(self):
        data=apply_labels(copy.deepcopy(dashboard),{'hk-yunyoo':'HK hk-yunyoo'})
        for p in data['panels']:
            if p['title'] not in COUNTS:
                continue
            for t in p['targets']:
                for key in ('expr','hostLabelBaseExpr'):
                    if key in t:t[key]=t[key].replace('singbox_route_${source_activity}','singbox_route_connections').replace('singbox_${source_activity}','singbox_connections')
                if p['title']=='Connections':
                    t['expr']='sum(singbox_connections{instance=~"$host",service=~"$service"})'
            if p['type']=='timeseries':p['options']['legend']['calcs']=['lastNotNull','max']
        next(v for v in data['templating']['list'] if v['name']=='source_activity')['label']='Source view'
        return data

    def test_patch_keeps_raw_labels_wrappers_layout_and_unrelated_panels(self):
        original=self.legacy();before=copy.deepcopy(original)
        result=patch_dashboard(original)
        self.assertEqual(before,original)
        self.assertEqual(result,patch_dashboard(result))
        self.assertEqual(result,apply_labels(result,{'hk-yunyoo':'HK hk-yunyoo'}))
        for old,new in zip(original['panels'],result['panels']):
            self.assertEqual(old['gridPos'],new['gridPos'])
            self.assertEqual(old['id'],new['id'])
            if old['title'] not in COUNTS|{'Source connections'}:self.assertEqual(old,new)
            elif old['title'] in COUNTS:
                self.assertIn('${source_activity}',new['targets'][0]['expr'])
                self.assertEqual(old['targets'][0]['datasource'],new['targets'][0]['datasource'])
        protocol=next(p for p in result['panels'] if p['title']=='Active connections by protocol')
        self.assertIn('last',protocol['options']['legend']['calcs'])
        self.assertNotIn('lastNotNull',protocol['options']['legend']['calcs'])

    def test_saved_all_view_is_retained_and_partial_dashboards_fail(self):
        original=self.legacy()
        v=next(v for v in original['templating']['list'] if v['name']=='source_activity')
        v['current']={'text':'All open sessions','value':'connections'}
        result=patch_dashboard(original)
        self.assertEqual(next(v for v in result['templating']['list'] if v['name']=='source_activity')['current'],v['current'])
        original['panels']=[p for p in original['panels'] if p['title']!='Active routing paths']
        with self.assertRaises(ValueError):patch_dashboard(original)


if __name__=='__main__':unittest.main()
