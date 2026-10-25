#!/usr/bin/env python3
"""Make existing connection panels share the per-session activity view."""
import argparse
import base64
import copy
import json
import os
import pathlib
import time
import sys
import urllib.request

COUNTS = {'Connections','Active connections by protocol','TCP / UDP / ICMP sessions',
          'Active routing paths','Service activity'}


def all_panels(items):
    for panel in items:
        yield panel
        yield from all_panels(panel.get('panels',[]))


def patch_dashboard(current):
    from build_dashboard import dashboard as generated
    templates = {p['title']:p for p in generated['panels'] if p['title'] in COUNTS | {'Source connections'}}
    after = copy.deepcopy(current)
    panels = list(all_panels(after['panels']))
    for title in COUNTS | {'Source connections'}:
        matches = [p for p in panels if p.get('title')==title]
        if len(matches)!=1:
            raise ValueError('expected one panel: '+title)
        panel = matches[0]
        panel['description'] = templates[title]['description']
        if title not in COUNTS:
            continue
        desired = {t['refId']:t['expr'] for t in templates[title]['targets']}
        if {t['refId'] for t in panel['targets']} != desired.keys():
            raise ValueError('unexpected query references: '+title)
        for target in panel['targets']:
            # Service activity has unrelated traffic/uptime queries B-D.
            if target['refId']!='A':
                continue
            expression = target.get('hostLabelBaseExpr',target['expr'])
            if '${source_activity}' not in expression:
                if title=='Connections':
                    expression = expression.replace('singbox_connections','singbox_route_${source_activity}')
                    expression = expression.replace('service=~"$service"}', 'service=~"$service",protocol=~"$protocol"}')
                elif title=='Service activity':
                    expression = expression.replace('singbox_connections','singbox_${source_activity}')
                else:
                    expression = expression.replace('singbox_route_connections','singbox_route_${source_activity}')
            if expression != desired['A']:
                raise ValueError('unexpected base expression: '+title)
            if 'hostLabelBaseExpr' in target:
                old = target['hostLabelBaseExpr']
                if old not in target['expr']:
                    raise ValueError('host label wrapper mismatch: '+title)
                target['expr'] = target['expr'].replace(old,expression)
                target['hostLabelBaseExpr'] = expression
            else:
                target['expr'] = expression
        if panel['type']=='timeseries':
            legend = panel.setdefault('options',{}).setdefault('legend',{})
            legend['calcs'] = ['last' if c=='lastNotNull' else c for c in legend.get('calcs',[])]
    variables = after['templating']['list']
    views = [v for v in variables if v['name']=='source_activity']
    if len(views)!=1 or views[0].get('current',{}).get('value') not in ('recent_connections','connections'):
        raise ValueError('expected existing valid activity view')
    views[0]['label'] = 'Connection view'
    return after


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--credentials-stdin',action='store_true',required=True)
    ap.parse_args()
    creds=json.load(sys.stdin)
    auth='Basic '+base64.b64encode((creds['GRAFANA_USER']+':'+creds['GRAFANA_PASSWORD']).encode()).decode()
    origin=creds.get('GRAFANA_URL','http://127.0.0.1:3000').rstrip('/')
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(path,data=None):
        req=urllib.request.Request(origin+path,headers={'Authorization':auth,'Content-Type':'application/json'},data=json.dumps(data).encode() if data is not None else None)
        with op.open(req,timeout=20) as r:return json.load(r)
    current=api('/api/dashboards/uid/sing-box-fleet')
    updated=patch_dashboard(current['dashboard'])
    root=pathlib.Path('/var/backups/sing-box-connection-activity');root.mkdir(parents=True,mode=0o700,exist_ok=True)
    backup=root/('dashboard-'+str(time.time_ns())+'.json')
    with os.fdopen(os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(current,f)
    if api('/api/dashboards/uid/sing-box-fleet')['dashboard']!=current['dashboard']:
        raise RuntimeError('concurrent dashboard edit')
    result=api('/api/dashboards/db',dict(dashboard=updated,folderUid=current['meta'].get('folderUid',''),overwrite=True,message='Unify connection activity across overview, protocol, network, route and source views'))
    saved=api('/api/dashboards/uid/sing-box-fleet')['dashboard']
    assert result['version']==saved['version']
    assert patch_dashboard(saved)==saved
    print(json.dumps(dict(version=saved['version'],uid=saved['uid'],backup=str(backup))))


if __name__=='__main__':
    main()
