#!/usr/bin/env python3
"""Import dashboard into existing Grafana; credentials supplied via environment."""
import base64
import json
import os
import pathlib
import urllib.request
import argparse
import sys


def publish():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials-stdin', action='store_true')
    parser.add_argument('--inspect', action='store_true', help='Read the current dashboard without publishing')
    parser.add_argument('--backup', help='Save current dashboard before overwriting, mode 0600')
    parser.add_argument('--preserve-links', action='store_true', help='Preserve existing local dashboard links')
    parser.add_argument('--host-labels', type=pathlib.Path, help='Optional raw-host to prefixed display-label JSON')
    args = parser.parse_args()
    if args.credentials_stdin:
        credentials = json.load(sys.stdin)
        os.environ.update({k: str(v) for k, v in credentials.items() if k in ('GRAFANA_URL', 'GRAFANA_USER', 'GRAFANA_PASSWORD')})
    origin = os.environ.get('GRAFANA_URL', 'http://127.0.0.1:3000').rstrip('/')
    credential = base64.b64encode((os.environ['GRAFANA_USER'] + ':' + os.environ['GRAFANA_PASSWORD']).encode()).decode()
    headers = {'Authorization': 'Basic ' + credential, 'Content-Type': 'application/json'}
    current = None
    if args.inspect or args.backup or args.preserve_links:
        with urllib.request.urlopen(urllib.request.Request(origin + '/api/dashboards/uid/sing-box-fleet', headers=headers), timeout=10) as response:
            current = json.load(response)
        if args.backup:
            with os.fdopen(os.open(args.backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                json.dump(current, stream, indent=2)
        if args.inspect:
            print(json.dumps(current))
            return
    with urllib.request.urlopen(urllib.request.Request(origin + '/api/datasources', headers=headers), timeout=10) as response:
        sources = json.load(response)
    candidates = [s for s in sources if s['type'] == 'prometheus']
    if not candidates:
        raise RuntimeError('No Prometheus datasource found')
    source = next((s for s in candidates if s.get('isDefault')), candidates[0])
    dashboard = json.loads(pathlib.Path(__file__).with_name('dashboard.json').read_text())
    if args.host_labels:
        from host_labels import apply_labels
        dashboard = apply_labels(dashboard, json.loads(args.host_labels.read_text(encoding='utf-8-sig')))
    if args.preserve_links:
        dashboard['links'] = current['dashboard'].get('links', [])
    dashboard['templating']['list'][0]['current'] = dict(text=source['name'], value=source['uid'])
    body = json.dumps(dict(dashboard=dashboard, overwrite=True, message='Deploy sing-box fleet monitoring')).encode()
    with urllib.request.urlopen(urllib.request.Request(origin + '/api/dashboards/db', data=body, headers=headers), timeout=15) as response:
        result = json.load(response)
    print(json.dumps(result))


if __name__ == '__main__':
    publish()
