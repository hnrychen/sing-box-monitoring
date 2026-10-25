#!/usr/bin/env python3
"""Update source table presentation without replacing the live dashboard."""
import argparse
import base64
import copy
import json
import os
import pathlib
import sys
import time
import urllib.request


def update_connections(dashboard, template):
    after = copy.deepcopy(dashboard)
    matches = [p for p in after['panels'] if p.get('title') == 'Source connections']
    if len(matches) != 1 or matches[0].get('type') != 'table':
        raise ValueError('exactly one existing Source connections table is required')
    panel = matches[0]
    host_matcher = dict(id='byRegexp', options='^(Host|instance|nodename)$')
    host_overrides = [o for o in panel['fieldConfig']['overrides'] if o.get('matcher') == host_matcher]
    for key in ('description', 'targets', 'transformations', 'fieldConfig', 'options'):
        panel[key] = copy.deepcopy(template[key])
    for target in panel['targets']:
        target['datasource'] = copy.deepcopy(panel['datasource'])
    if not any(o.get('matcher') == host_matcher for o in panel['fieldConfig']['overrides']):
        panel['fieldConfig']['overrides'].extend(host_overrides)
    from build_dashboard import dashboard as generated
    variable = next(v for v in generated['templating']['list'] if v['name'] == 'source_activity')
    variables = after.setdefault('templating', {}).setdefault('list', [])
    existing = [v for v in variables if v.get('name') == 'source_activity']
    if len(existing) > 1:
        raise ValueError('duplicate source activity variable')
    if existing:
        # Preserve a user's valid saved view selection.
        selected = existing[0].get('current', {}).get('value')
        updated = copy.deepcopy(variable)
        if selected in ('connections', 'recent_connections'):
            updated['current'] = copy.deepcopy(existing[0]['current'])
            for option in updated['options']:
                option['selected'] = option['value'] == selected
        variables[variables.index(existing[0])] = updated
    else:
        variables.append(copy.deepcopy(variable))
    return after


def update_description(dashboard, template):
    after = copy.deepcopy(dashboard)
    matches = [p for p in after['panels'] if p.get('title') == 'Source connections']
    if len(matches) != 1 or matches[0].get('type') != 'table':
        raise ValueError('exactly one existing Source connections table is required')
    matches[0]['description'] = template['description']
    return after


def update_tables(dashboard):
    from build_dashboard import dashboard as generated
    titles = {'Source connections', 'Source active-connection bytes'}
    templates = {p['title']: p for p in generated['panels'] if p['title'] in titles}
    after = update_connections(dashboard, templates['Source connections'])
    seen = set()
    for panel in after['panels']:
        if panel['title'] not in titles:
            continue
        if panel['title'] in seen:
            raise ValueError('duplicate source-table title')
        seen.add(panel['title'])
        template = templates[panel['title']]
        if panel['title'] == 'Source connections':
            continue
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
    if seen != titles:
        raise ValueError('both existing source tables are required')
    return after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-stdin', required=True, action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--connections-only', action='store_true', help='Leave the byte table and every other panel untouched')
    mode.add_argument('--description-only', action='store_true', help='Update only the Source connections description')
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
    if args.description_only:
        from build_dashboard import source_panel
        after = update_description(current['dashboard'], source_panel)
    elif args.connections_only:
        from build_dashboard import source_panel
        after = update_connections(current['dashboard'], source_panel)
    else:
        after = update_tables(current['dashboard'])
    root = pathlib.Path('/var/backups/sing-box-source-flags')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = root / ('dashboard-' + str(time.time_ns()) + '.json')
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(current, stream)
    if request('/api/dashboards/uid/sing-box-fleet')['dashboard'] != current['dashboard']:
        raise RuntimeError('dashboard changed during preparation')
    message = ('Document recent-payload TCP peer freshness' if args.description_only else
               'Distinguish active sessions from RTT-only TCP peers and sort source rows')
    result = request('/api/dashboards/db', dict(dashboard=after, folderUid=current['meta'].get('folderUid', ''),
                     overwrite=True, message=message))
    verified = request('/api/dashboards/uid/sing-box-fleet')['dashboard']
    if (verified['version'] != result['version'] or verified['panels'] != after['panels']
            or verified['templating'] != after['templating']):
        raise RuntimeError('source tables differ after publishing')
    print(json.dumps(dict(version=result['version'], backup=str(backup))))


if __name__ == '__main__':
    main()
