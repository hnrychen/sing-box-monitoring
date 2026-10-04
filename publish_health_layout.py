#!/usr/bin/env python3
"""Update only the existing Grafana activity/health layout; credentials via stdin."""
import argparse
import base64
import json
import os
import pathlib
import sys
import time
import urllib.request

from build_dashboard import dashboard as generated
from dashboard_layout import update_layout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-stdin', required=True, action='store_true')
    parser.add_argument('--uid', default='sing-box-fleet')
    parser.add_argument('--backup-dir', type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not args.uid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in args.uid):
        raise ValueError('invalid dashboard UID')
    credentials = json.load(sys.stdin)
    origin = credentials.get('GRAFANA_URL', 'http://127.0.0.1:3000').rstrip('/')
    auth = base64.b64encode((credentials['GRAFANA_USER'] + ':' + credentials['GRAFANA_PASSWORD']).encode()).decode()
    headers = {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, data=None):
        req = urllib.request.Request(origin + path, headers=headers, data=json.dumps(data).encode() if data else None)
        with opener.open(req, timeout=15) as response:
            return json.load(response)

    path = '/api/dashboards/uid/' + args.uid
    current = request(path)
    template = next(p for p in generated['panels'] if p['title'] == 'Service activity')
    after = update_layout(current['dashboard'], template)
    if after == current['dashboard']:
        print(json.dumps(dict(version=after['version'], changed=False)))
        return
    args.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = args.backup_dir / ('dashboard-' + str(time.time_ns()) + '.json')
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(current, stream)
    if request(path)['dashboard'] != current['dashboard']:
        raise RuntimeError('dashboard changed during preparation')
    published = request('/api/dashboards/db', dict(dashboard=after, folderUid=current['meta'].get('folderUid', ''),
        overwrite=False, message='Group availability graphs under health; add per-service activity table'))
    if request(path)['dashboard']['panels'] != after['panels']:
        raise RuntimeError('dashboard layout differs after publishing')
    print(json.dumps(dict(version=published['version'], changed=True, backup=str(backup))))


if __name__ == '__main__':
    main()
