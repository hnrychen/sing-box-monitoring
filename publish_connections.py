#!/usr/bin/env python3
"""Configure Infinity and insert only the live destination section into current Grafana."""
import argparse
import base64
import copy
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request


def insert_section(dashboard, section):
    result = copy.deepcopy(dashboard)
    panels = result['panels']
    titles = {'Active connection destinations', 'Live destinations'}
    existing = [p for p in panels if p['title'] in titles]
    old_height = sum(p['gridPos']['h'] for p in existing)
    panels[:] = [p for p in panels if p['title'] not in titles]
    index = next(i for i, p in enumerate(panels) if p['title'] == 'Service resources and collector health')
    start = panels[index]['gridPos']['y'] - old_height
    height = sum(p['gridPos']['h'] for p in section)
    for p in panels[index:]:
        p['gridPos']['y'] += height - old_height
    section = copy.deepcopy(section)
    occupied = {p['id'] for p in panels}
    if any(p['id'] in occupied for p in section):
        raise ValueError('live panel ID conflict; inspect current dashboard')
    offset = start - section[0]['gridPos']['y']
    for p in section:
        p['gridPos']['y'] += offset
    panels[index:index] = section
    result['refresh'] = '5s'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-stdin', required=True, action='store_true')
    parser.add_argument('--config', default='/etc/sing-box-connections/config.json')
    parser.add_argument('--bridge-url', required=True, help='Private URL reachable from the Grafana server/container')
    parser.add_argument('--host-labels', type=pathlib.Path)
    args = parser.parse_args()
    config = json.loads(pathlib.Path(args.config).read_text())
    credentials = json.load(sys.stdin)
    origin = credentials.get('GRAFANA_URL', 'http://127.0.0.1:3000').rstrip('/')
    auth = base64.b64encode((credentials['GRAFANA_USER'] + ':' + credentials['GRAFANA_PASSWORD']).encode()).decode()
    headers = {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, data=None, method=None):
        req = urllib.request.Request(origin + path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
        with opener.open(req, timeout=15) as response:
            return json.load(response)

    source = dict(uid='sing-box-connections', name='sing-box connections', type='yesoreyeram-infinity-datasource',
        access='proxy', url=args.bridge_url.rstrip('/'), isDefault=False,
        jsonData=dict(auth_method='bearerToken', allowedHosts=[args.bridge_url.rstrip('/')],
                      timeoutInSeconds=8, proxyType='none', allowDangerousHTTPMethods=False),
        secureJsonData=dict(bearerToken=config['token']))
    try:
        before_source = request('/api/datasources/uid/sing-box-connections')
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        request('/api/datasources', source)
    else:
        if before_source['type'] != source['type']:
            raise ValueError('datasource UID belongs to another plugin')
        request('/api/datasources/uid/sing-box-connections', source, 'PUT')
    current = request('/api/dashboards/uid/sing-box-fleet')
    from build_dashboard import dashboard
    section = [p for p in dashboard['panels'] if p['title'] in ('Active connection destinations', 'Live destinations')]
    if args.host_labels:
        from host_labels import apply_labels
        section = apply_labels(dict(panels=section), json.loads(args.host_labels.read_text(encoding='utf-8-sig')))['panels']
    after = insert_section(current['dashboard'], section)
    backup_dir = pathlib.Path('/var/backups/sing-box-connections')
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / ('dashboard-' + str(time.time_ns()) + '.json')
    with os.fdopen(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
        json.dump(current, stream)
    latest = request('/api/dashboards/uid/sing-box-fleet')
    if latest['dashboard'] != current['dashboard']:
        raise RuntimeError('Grafana dashboard changed during update')
    result = request('/api/dashboards/db', dict(dashboard=after, folderUid=current['meta'].get('folderUid', ''), overwrite=True,
                     message='Add current connection destinations between TCP latency and service resources'))
    actual = request('/api/dashboards/uid/sing-box-fleet')['dashboard']
    if actual['panels'] != after['panels']:
        raise RuntimeError('published destination section differs')
    print(json.dumps(dict(url=result.get('url'), version=result.get('version'), backup=str(backup))))


if __name__ == '__main__':
    main()
