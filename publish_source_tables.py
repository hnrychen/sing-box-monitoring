#!/usr/bin/env python3
"""Replace source-table image columns with country-prefixed IP display labels."""
import argparse
import base64
import copy
import json
import os
import pathlib
import sys
import time
import urllib.request


def update_tables(dashboard):
    from build_dashboard import dashboard as generated
    titles = {'Source connections', 'Source active-connection bytes'}
    templates = {p['title']: p for p in generated['panels'] if p['title'] in titles}
    after = copy.deepcopy(dashboard)
    seen = set()
    for panel in after['panels']:
        if panel['title'] not in titles:
            continue
        if panel['title'] in seen:
            raise ValueError('duplicate source-table title')
        seen.add(panel['title'])
        template = templates[panel['title']]
        expressions = {t['refId']: t['expr'] for t in template['targets']}
        if {t['refId'] for t in panel['targets']} != expressions.keys():
            raise ValueError('unexpected source-table query layout')
        for target in panel['targets']:
            target['expr'] = expressions[target['refId']]
        organize = [t for t in panel['transformations'] if t['id'] == 'organize']
        if len(organize) != 1:
            raise ValueError('unexpected source-table transformations')
        options = organize[0]['options']
        options['renameByName'].pop('country_code', None)
        options['renameByName']['source_ip_display'] = 'Source IP'
        options['excludeByName'].update(country_code=True, source_ip=True)
        options['indexByName'] = copy.deepcopy(template['transformations'][-1]['options']['indexByName'])
        overrides = panel['fieldConfig']['overrides']
        overrides[:] = [o for o in overrides if o.get('matcher') != dict(id='byName', options='Flag')]
        if panel['title'] == 'Source connections':
            for override in overrides:
                if override.get('matcher') == dict(id='byName', options='Source IP'):
                    for prop in override['properties']:
                        if prop['id'] == 'custom.width':
                            prop['value'] = 165
    if seen != titles:
        raise ValueError('both existing source tables are required')
    return after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-stdin', required=True, action='store_true')
    args = parser.parse_args()
    credentials = json.load(sys.stdin)
    origin = credentials.get('GRAFANA_URL', 'http://127.0.0.1:3000').rstrip('/')
    auth = base64.b64encode((credentials['GRAFANA_USER'] + ':' + credentials['GRAFANA_PASSWORD']).encode()).decode()
    headers = {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, data=None):
        req = urllib.request.Request(origin + path, headers=headers, data=json.dumps(data).encode() if data else None)
        with opener.open(req, timeout=15) as response:
            return json.load(response)
    current = request('/api/dashboards/uid/sing-box-fleet')
    after = update_tables(current['dashboard'])
    root = pathlib.Path('/var/backups/sing-box-source-flags')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = root / ('dashboard-' + str(time.time_ns()) + '.json')
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(current, stream)
    if request('/api/dashboards/uid/sing-box-fleet')['dashboard'] != current['dashboard']:
        raise RuntimeError('dashboard changed during preparation')
    result = request('/api/dashboards/db', dict(dashboard=after, folderUid=current['meta'].get('folderUid', ''),
                     overwrite=True, message='Use small inline country flags in both source tables'))
    if request('/api/dashboards/uid/sing-box-fleet')['dashboard']['panels'] != after['panels']:
        raise RuntimeError('source tables differ after publishing')
    print(json.dumps(dict(version=result['version'], backup=str(backup))))


if __name__ == '__main__':
    main()
