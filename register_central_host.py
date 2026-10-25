#!/usr/bin/env python3
"""Add a previously scraped exporter to the existing live view and proxy probes.

Run on the central host as root. Private host/probe settings arrive on stdin.
Existing CA/token files must already be present in /opt/monitoring/sing-box.
"""
import argparse
import copy
import ipaddress
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time


def run(*args, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('registration command failed: ' + pathlib.Path(args[0]).name)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-stdin', required=True, action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    settings = json.load(sys.stdin)
    host = settings['host']
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', host):
        raise ValueError('invalid host label')
    address = str(ipaddress.IPv4Address(settings['address']))
    exporter_port = settings.get('exporter_port', 9119)
    if not isinstance(exporter_port, int) or isinstance(exporter_port, bool) or not 1 <= exporter_port <= 65535:
        raise ValueError('invalid exporter port')
    interval, timeout = settings.get('scrape_interval', 5), settings.get('scrape_timeout', 4)
    if not isinstance(interval, int) or not isinstance(timeout, int) or not 2 <= timeout <= interval <= 300:
        raise ValueError('invalid scrape timing')
    source = pathlib.Path(__file__).resolve().parent
    connections_path = pathlib.Path('/etc/sing-box-connections/config.json')
    probes_path = pathlib.Path('/etc/sing-box-probe/config.json')
    prometheus_path = pathlib.Path('/opt/monitoring/prometheus.yml')
    paths = (connections_path, probes_path, prometheus_path)
    originals = {path: path.read_bytes() for path in paths}
    connections = json.loads(originals[connections_path])
    probes = json.loads(originals[probes_path])
    secret_root = pathlib.Path('/opt/monitoring/sing-box')
    ca, token = secret_root / (host + '.crt'), secret_root / (host + '.token')
    if not ca.is_file() or not token.is_file():
        raise ValueError('register the authenticated Prometheus exporter first')
    new_source = dict(host=host, url='https://' + address + ':' + str(exporter_port), ca_file=str(ca), token_file=str(token))
    updated_connections = copy.deepcopy(connections)
    updated_connections['sources'] = [s for s in connections['sources'] if s['host'] != host] + [new_source]
    targets = settings['targets']
    if not targets or any(t.get('instance') != host or t.get('outbound', {}).get('server') != address for t in targets):
        raise ValueError('probe targets must belong to this host/address')
    updated_probes = copy.deepcopy(probes)
    updated_probes['targets'] = [t for t in probes['targets'] if t['instance'] != host] + targets
    from connections_view import View
    from proxy_probe import client_config, validate
    view = View(updated_connections)
    try:
        snapshot = view.fetch(host)
        if not snapshot['services'] or any(not s['healthy'] for s in snapshot['services']):
            raise RuntimeError('new exporter live snapshot is unhealthy')
    finally:
        view.pool.shutdown()
    validate(updated_probes)
    before = originals[prometheus_path].decode()
    job = re.compile(r'(?ms)^  - job_name: sing-box-' + re.escape(host) + r'\n.*?(?=^  - job_name:|^# END managed sing-box monitoring|\Z)')
    matches = list(job.finditer(before))
    if len(matches) != 1:
        raise ValueError('expected one existing Prometheus job for this host')
    snippet = matches[0].group()
    for field, value in (('scrape_interval', str(interval) + 's'), ('scrape_timeout', str(timeout) + 's'), ('sample_limit', '0')):
        snippet, count = re.subn(r'(?m)^    ' + field + r': .*$', '    ' + field + ': ' + value, snippet)
        if count != 1:
            raise ValueError('unexpected exporter scrape layout')
    after = before[:matches[0].start()] + snippet + before[matches[0].end():]
    backup = pathlib.Path('/var/backups/sing-box-fleet') / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    candidate = secret_root / ('fleet-candidate-' + str(time.time_ns()) + '.yml')
    candidate.write_text(after)
    os.chmod(candidate, 0o644)
    run('docker', 'exec', 'prometheus', '/bin/promtool', 'check', 'config', '/etc/prometheus/sing-box/' + candidate.name)
    probe_check = backup / 'probe-preflight.json'
    probe_check.write_text(json.dumps(client_config(updated_probes)))
    os.chmod(probe_check, 0o600)
    run(probes.get('sing_box_binary', '/usr/bin/sing-box'), 'check', '-c', str(probe_check))
    for path in paths:
        if path.read_bytes() != originals[path]:
            raise RuntimeError('central configuration changed during preflight')
        destination = backup / (path.parent.name + '-' + path.name)
        shutil.copy2(path, destination)
        os.chmod(destination, 0o600)
    try:
        run(sys.executable, str(source / 'install_connections.py'), '--config-stdin', data=json.dumps(updated_connections))
        run(sys.executable, str(source / 'install_proxy_probe.py'), '--config-stdin', data=json.dumps(updated_probes))
        # Preserve the inode of Prometheus's single-file Docker bind mount.
        prometheus_path.write_text(after)
        run('docker', 'kill', '--signal', 'HUP', 'prometheus')
    except Exception:
        for path in paths:
            path.write_bytes(originals[path])
        run('systemctl', 'restart', 'sing-box-connections', 'sing-box-probe')
        run('docker', 'kill', '--signal', 'HUP', 'prometheus')
        raise
    print(json.dumps(dict(host=host, live_sources=len(updated_connections['sources']), probe_targets=len(updated_probes['targets']), new_protocols=[t['protocol'] for t in targets], scrape_interval=interval, backup=str(backup))))


if __name__ == '__main__':
    main()
