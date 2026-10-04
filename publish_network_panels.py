#!/usr/bin/env python3
"""Add only network/probe panels to current Grafana dashboard; preserve other edits."""
import argparse
import base64
import copy
import json
import os
import pathlib
import sys
import time
import urllib.request
from build_dashboard import network_templates
from network_layout import update_network_layout
from probe_table import update_probe_table


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-stdin', action='store_true', required=True)
    parser.add_argument('--backup-dir', type=pathlib.Path, required=True)
    parser.add_argument('--host-labels', type=pathlib.Path)
    parser.add_argument('--table-only', action='store_true', help='Update only the existing merged proxy-check table')
    args = parser.parse_args()
    credentials = json.load(sys.stdin)
    origin = credentials.get('GRAFANA_URL', 'http://127.0.0.1:3000').rstrip('/')
    auth = base64.b64encode((credentials['GRAFANA_USER'] + ':' + credentials['GRAFANA_PASSWORD']).encode()).decode()
    headers = {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, data=None):
        with opener.open(urllib.request.Request(origin + path, headers=headers,
                data=json.dumps(data).encode() if data else None), timeout=15) as response:
            return json.load(response)
    endpoint = '/api/dashboards/uid/sing-box-fleet'
    before = request(endpoint)
    templates = copy.deepcopy(network_templates)
    ds = next(p['datasource'] for p in before['dashboard']['panels'] if p['title'] == 'Service availability')
    for panel in templates:
        panel['datasource'] = ds
        for target in panel.get('targets', []):
            target['datasource'] = ds
    if args.host_labels:
        from host_labels import apply_labels
        templates = apply_labels(dict(panels=templates), json.loads(args.host_labels.read_text(encoding='utf-8-sig')))['panels']
    after = update_probe_table(before['dashboard'], next(p for p in templates if p['id'] == 1001)) if args.table_only else update_network_layout(before['dashboard'], templates)
    if after == before['dashboard']:
        print('Requested network panel update already deployed')
        return
    args.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = args.backup_dir / ('dashboard-' + str(time.time_ns()) + '.json')
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(before, stream)
    if request(endpoint)['dashboard'] != before['dashboard']:
        raise RuntimeError('dashboard changed during preparation')
    response = request('/api/dashboards/db', dict(dashboard=after, folderUid=before['meta'].get('folderUid', ''),
        overwrite=False, message='Merge proxy checks by host/protocol' if args.table_only else 'Add central proxy checks and passive TCP diagnostics'))
    if request(endpoint)['dashboard']['panels'] != after['panels']:
        raise RuntimeError('deployed network panels differ')
    print(json.dumps(dict(version=response['version'], backup=str(backup))))


if __name__ == '__main__':
    main()
