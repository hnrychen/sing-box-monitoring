#!/usr/bin/env python3
"""Add only the loopback central probe job to current Prometheus config."""
import argparse
import json
import pathlib
import re
import shutil
import time
from install import run

START = '# BEGIN managed sing-box probes\n'
END = '# END managed sing-box probes\n'
BLOCK = '''  - job_name: sing-box-probes
    honor_labels: true
    scrape_interval: 5s
    scrape_timeout: 4s
    sample_limit: 2000
    label_limit: 12
    body_size_limit: 1MB
    static_configs:
      - targets: ['127.0.0.1:9122']
'''


def update(before):
    if START in before:
        first, rest = before.split(START, 1)
        _, last = rest.split(END, 1)
        return first + START + BLOCK + END + last
    if 'job_name: sing-box-probes' in before:
        raise ValueError('unmanaged probe job already exists')
    # Preserve arbitrary top-level key ordering; append inside scrape_configs.
    section = re.search(r'^scrape_configs:[ \t]*(?:#.*)?$', before, re.M)
    if section is None:
        raise ValueError('requires a block-style scrape_configs section')
    following = re.search(r'^[A-Za-z_][A-Za-z0-9_]*:', before[section.end():], re.M)
    end = section.end() + following.start() if following else len(before)
    return before[:end].rstrip() + '\n' + START + BLOCK + END + before[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=pathlib.Path, default=pathlib.Path('/opt/monitoring/prometheus.yml'))
    args = parser.parse_args()
    before = args.path.read_text()
    after = update(before)
    if after == before:
        print('Probe scrape configuration unchanged')
        return
    info = json.loads(run('docker', 'inspect', 'prometheus').stdout)[0]
    if info['HostConfig']['NetworkMode'] != 'host':
        raise ValueError('loopback probes require host-network Prometheus; integrate manually for other topologies')
    candidate = args.path.parent / 'sing-box-probe-candidate.yml'
    candidate.write_text(after)
    try:
        # Use the current running binary and mount topology. Candidate is kept
        # on host; docker cp gives promtool a private temporary input only.
        run('docker', 'cp', str(candidate), 'prometheus:/tmp/sing-box-probe-candidate.yml')
        run('docker', 'exec', 'prometheus', 'promtool', 'check', 'config', '/tmp/sing-box-probe-candidate.yml')
        if args.path.read_text() != before:
            raise RuntimeError('Prometheus config changed during preparation')
        backup = args.path.with_name('prometheus.probe-backup-' + str(time.time_ns()) + '.yml')
        shutil.copy2(args.path, backup)
        args.path.write_text(after)  # preserve the inode of the Docker bind mount.
        try:
            run('docker', 'kill', '--signal', 'HUP', 'prometheus')
        except Exception:
            args.path.write_text(before)
            raise
        print('Probe job added; unrelated jobs preserved; backup: ' + str(backup))
    finally:
        candidate.unlink(missing_ok=True)
        run('docker', 'exec', '--user', '0', 'prometheus', 'rm', '-f', '/tmp/sing-box-probe-candidate.yml')


if __name__ == '__main__':
    main()
