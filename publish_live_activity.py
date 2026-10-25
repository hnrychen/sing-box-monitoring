#!/usr/bin/env python3
"""Patch only live-activity/RTT fields on the current Grafana dashboard.

Credentials are supplied as JSON on stdin; existing flags/layout/links and all
other panel customizations are preserved. RTT base expressions are updated too,
so later host-label refreshes cannot restore the raw socket queries. Back up
before a version-checked POST.
"""
import base64
import argparse
import copy
import json
import os
import pathlib
import sys
import time
import urllib.request
from build_dashboard import dashboard as template


TITLES = ('TCP RTT · socket-weighted mean', 'TCP RTT · current p95 / maximum',
          'TCP RTT sample coverage', 'TCP sample last-ACK age', 'Live destinations')


def patch_dashboard(original):
    result = copy.deepcopy(original)
    templates = {p['title']: p for p in template['panels']}
    found = set()

    def visit(panels):
        for panel in panels:
            visit(panel.get('panels', []))
            title = panel.get('title')
            if title not in TITLES:
                continue
            found.add(title)
            if title != 'Live destinations':
                for target in panel['targets']:
                    for field in ('expr', 'hostLabelBaseExpr'):
                        if field not in target:
                            continue
                        expression = target[field]
                        expression = expression.replace('singbox_tcp_rtt_', 'singbox_tcp_client_rtt_')
                        expression = expression.replace('singbox_tcp_ack_age_max_seconds', 'singbox_tcp_client_rtt_ack_age_max_seconds')
                        target[field] = expression
                panel['description'] = templates[title]['description']
                continue
            panel['description'] = templates[title]['description']
            for target in panel['targets']:
                if '${activity:queryparam}' not in target['url']:
                    target['url'] += '&${activity:queryparam}'
                if not any(c['selector'] == 'idle_seconds' for c in target['columns']):
                    index = next((i for i, c in enumerate(target['columns']) if c['selector'] == 'outbound'), len(target['columns']))
                    target['columns'].insert(index, dict(selector='idle_seconds', text='Idle', type='number'))
            overrides = panel['fieldConfig']['overrides']
            if not any(o['matcher'].get('options') == 'Idle' for o in overrides):
                overrides.append(dict(matcher=dict(id='byName', options='Idle'), properties=[dict(id='unit', value='s'), dict(id='decimals', value=0), dict(id='custom.width', value=85)]))
            for transform in panel.get('transformations', []):
                if transform['id'] == 'organize':
                    indices = transform['options'].get('indexByName', {})
                    if 'Idle' not in indices:
                        position = indices.get('Egress', len(indices))
                        for name in list(indices):
                            if indices[name] >= position:
                                indices[name] += 1
                        indices['Idle'] = position
    visit(result['panels'])
    if found != set(TITLES):
        raise ValueError('missing panels: ' + ', '.join(set(TITLES) - found))
    variables = result['templating']['list']
    if not any(v['name'] == 'activity' for v in variables):
        variables.append(copy.deepcopy(next(v for v in template['templating']['list'] if v['name'] == 'activity')))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-stdin', action='store_true', help='Read Grafana login JSON from standard input (the default)')
    parser.add_argument('--dry-run', action='store_true', help='Compare the live dashboard without saving or publishing')
    args = parser.parse_args()
    credentials = json.load(sys.stdin)
    authorization = 'Basic ' + base64.b64encode((credentials['GRAFANA_USER'] + ':' + credentials['GRAFANA_PASSWORD']).encode()).decode()

    def request(path, data=None):
        req = urllib.request.Request('http://127.0.0.1:3000' + path, headers={'Authorization': authorization,
            'Content-Type': 'application/json'}, data=None if data is None else json.dumps(data).encode())
        return json.load(urllib.request.urlopen(req, timeout=15))
    path = '/api/dashboards/uid/sing-box-fleet'
    current = request(path)
    original = current['dashboard']
    updated = patch_dashboard(original)
    if updated == original:
        print(json.dumps(dict(unchanged=True, version=original['version'])))
        return
    if args.dry_run:
        changed_panels = [after['title'] for before, after in zip(original['panels'], updated['panels']) if before != after]
        print(json.dumps(dict(changed=True, version=original['version'], panels=changed_panels,
                              variables_changed=original['templating'] != updated['templating'])))
        return
    backup = pathlib.Path('/var/backups/sing-box-monitoring')
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup, 0o700)
    saved = backup / ('dashboard-activity-' + str(time.time_ns()) + '.json')
    saved.write_text(json.dumps(current))
    os.chmod(saved, 0o600)
    if request(path)['dashboard'] != original:
        raise RuntimeError('dashboard changed during patch preparation')
    response = request('/api/dashboards/db', dict(dashboard=updated,
        folderUid=current.get('meta', {}).get('folderUid', ''), overwrite=False,
        message='Authenticated recent-payload RTT and recent/all destination activity views'))
    actual = request(path)['dashboard']
    if response.get('status') != 'success' or actual['version'] != response['version'] or actual['version'] <= original['version']:
        raise RuntimeError('dashboard version verification failed')
    if actual['panels'] != updated['panels'] or actual['templating'] != updated['templating']:
        raise RuntimeError('dashboard GET differs from targeted update')
    print(json.dumps(dict(status='verified', version=actual['version'], previous=original['version'], backup=str(saved))))


if __name__ == '__main__':
    main()
