#!/usr/bin/env python3
"""Optional Grafana-only host aliases; no changes to stored metric identities."""
import argparse
import base64
import copy
import json
import os
import pathlib
import re
import sys
import time
import urllib.request


def validate(labels):
    if not isinstance(labels, dict) or not labels or len(labels) > 256:
        raise ValueError('expected 1..256 raw-host/display-label pairs')
    for host, display in labels.items():
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', host):
            raise ValueError('invalid raw host')
        if not isinstance(display, str) or not display.endswith(' ' + host) or len(display) > 160 or any(c in display for c in '\n\r"\\$'):
            raise ValueError('display label must prefix and retain the original hostname')


def display_expression(expression, field, labels):
    query = f'label_replace(({expression}), "display_host", "$1", "{field}", "(.*)")'
    for host, display in sorted(labels.items()):
        pattern = json.dumps('^' + re.escape(host) + '$')
        query = f'label_replace({query}, "display_host", {json.dumps(display, ensure_ascii=False)}, "{field}", {pattern})'
    return query


def selector_query(expression, field, labels):
    return 'query_result(' + display_expression(expression, field, labels) + ')'


def apply_labels(dashboard, labels):
    validate(labels)
    result = copy.deepcopy(dashboard)
    variables = result.get('templating', {}).get('list', [])
    for variable in variables:
        field = None
        if variable['name'] == 'host':
            expression, field = 'max by(instance)(singbox_up)', 'instance'
        elif variable['name'] == 'nodename':
            expression, field = 'max by(nodename)(node_uname_info{job="$job"})', 'nodename'
        elif variable['name'] == 'node':
            expression, field = 'max by(instance)(node_uname_info{job="$job",nodename="$nodename"})', 'instance'
        if field:
            query = selector_query(expression, field, labels)
            variable['definition'] = query
            variable['query'] = dict(query=query, refId='HostDisplayVariableQuery')
            variable['regex'] = '/display_host="(?<text>[^"]+)".*' + field + '="(?<value>[^"]+)"/'
            variable['options'] = []
            current = variable.get('current', {})
            value = current.get('value')
            if isinstance(value, str) and value in labels:
                current['text'] = labels[value]
            elif isinstance(value, list):
                current['text'] = [labels.get(v, v) for v in value]
    matcher = dict(id='byRegexp', options='^(Host|instance|nodename)$')
    def panels(items):
        for panel in items:
            panels(panel.get('panels', []))
            if panel.get('type') == 'table':
                config = panel.setdefault('fieldConfig', {})
                overrides = config.setdefault('overrides', [])
                overrides[:] = [o for o in overrides if o.get('matcher') != matcher]
                overrides.append(dict(matcher=matcher, properties=[
                    dict(id='mappings', value=[dict(type='value', options={host: dict(text=display) for host, display in labels.items()})]),
                    dict(id='custom.width', value=70)]))
            elif panel.get('type') == 'timeseries':
                transforms = panel.setdefault('transformations', [])
                for host, display in sorted(labels.items()):
                    regex = '^' + re.escape(host) + '(?= |$)'
                    legacy = '^' + re.escape(host) + r'(?=\s|$)'
                    transforms[:] = [t for t in transforms if not (t.get('id') == 'renameByRegex' and t.get('options', {}).get('regex') in (regex, legacy))]
                # Prometheus's explicit legend aliases can override transformed
                # field names. Add a display-only query-result label instead;
                # raw instance labels, storage and numeric values stay intact.
                for target in panel.get('targets', []):
                    legend = target.get('hostLabelBaseLegend', target.get('legendFormat', ''))
                    if '{{instance}}' not in legend:
                        continue
                    expression = target.get('hostLabelBaseExpr', target['expr'])
                    target['hostLabelBaseExpr'] = expression
                    target['hostLabelBaseLegend'] = legend
                    target['expr'] = display_expression(expression, 'instance', labels)
                    target['legendFormat'] = legend.replace('{{instance}}', '{{display_host}}')
    panels(result.get('panels', []))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--labels', type=pathlib.Path, required=True)
    parser.add_argument('--uid', action='append', required=True, help='Existing dashboard UID; repeat for multiple dashboards')
    parser.add_argument('--credentials-stdin', action='store_true', required=True)
    parser.add_argument('--backup-dir', type=pathlib.Path, required=True)
    args = parser.parse_args()
    labels = json.loads(args.labels.read_text(encoding='utf-8-sig'))
    validate(labels)
    credentials = json.load(sys.stdin)
    origin = credentials.get('GRAFANA_URL', 'http://127.0.0.1:3000').rstrip('/')
    auth = base64.b64encode((credentials['GRAFANA_USER'] + ':' + credentials['GRAFANA_PASSWORD']).encode()).decode()
    headers = dict(Authorization='Basic ' + auth, **{'Content-Type': 'application/json'})
    def request(path, body=None):
        with urllib.request.urlopen(urllib.request.Request(origin + path, data=json.dumps(body).encode() if body else None, headers=headers), timeout=15) as response:
            return json.load(response)
    args.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for uid in args.uid:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', uid):
            raise ValueError('invalid UID')
        current = request('/api/dashboards/uid/' + uid)
        after = apply_labels(current['dashboard'], labels)
        path = args.backup_dir / (uid + '-' + str(time.time_ns()) + '.json')
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            json.dump(current, stream)
        latest = request('/api/dashboards/uid/' + uid)
        if latest['dashboard'] != current['dashboard']:
            raise RuntimeError('dashboard changed during update')
        published = request('/api/dashboards/db', dict(dashboard=after, folderUid=current['meta'].get('folderUid', ''), overwrite=True, message='Prefix host display labels with country flags'))
        actual = request('/api/dashboards/uid/' + uid)['dashboard']
        assert actual['templating']['list'] == after['templating']['list']
        assert actual['panels'] == after['panels']
        print(json.dumps(dict(title=actual['title'], version=published['version'], backup=str(path))))


if __name__ == '__main__':
    main()
