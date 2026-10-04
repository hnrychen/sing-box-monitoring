"""Narrow, repeatable update of only the existing end-to-end results table."""
import copy


def update_probe_table(dashboard, template):
    after = copy.deepcopy(dashboard)
    matches = [p for p in after['panels'] if p.get('id') == 1001]
    if len(matches) != 1 or matches[0]['title'] != 'Latest proxy checks' or matches[0]['type'] != 'table':
        raise ValueError('expected the existing Latest proxy checks table')
    panel = matches[0]
    host_matcher = dict(id='byRegexp', options='^(Host|instance|nodename)$')
    host_overrides = [copy.deepcopy(o) for o in panel.get('fieldConfig', {}).get('overrides', [])
        if o.get('matcher') == host_matcher]
    for key in ('description', 'targets', 'transformations', 'fieldConfig', 'options'):
        panel[key] = copy.deepcopy(template[key])
    for target in panel['targets']:
        target['datasource'] = copy.deepcopy(panel['datasource'])
    if not any(o.get('matcher') == host_matcher for o in panel['fieldConfig']['overrides']):
        panel['fieldConfig']['overrides'].extend(host_overrides)
    return after
