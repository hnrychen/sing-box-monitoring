#!/usr/bin/env python3
"""Opt-in HTTP/TLS/QUIC domain sniffing, with validation and per-service rollback."""
import argparse
import json
import os
import pathlib
import subprocess
import time

SNIFF = {'action': 'sniff', 'sniffer': ['http', 'tls', 'quic'], 'timeout': '100ms'}


def enable(config):
    rules = config.setdefault('route', {}).setdefault('rules', [])
    if any(rule.get('action') == 'sniff' for rule in rules):
        return False
    rules.insert(0, dict(SNIFF))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', action='append', required=True, metavar='UNIT=/ABSOLUTE/CONFIG.JSON')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    for item in args.service:
        unit, _, filename = item.partition('=')
        path = pathlib.Path(filename)
        if not unit or not path.is_absolute():
            parser.error('expected UNIT=/absolute/config.json')
        before = path.read_text()
        config = json.loads(before)
        if not enable(config):
            print(unit + ': existing sniff rule retained')
            continue
        root = pathlib.Path('/var/backups/sing-box-domain-sniff') / str(time.time_ns())
        root.mkdir(parents=True, mode=0o700)
        backup, candidate = root / 'config.json', root / 'candidate.json'
        for target, content in [(backup, before), (candidate, json.dumps(config, indent=2) + '\n')]:
            with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
                stream.write(content)
        subprocess.run(['sing-box', 'check', '-c', str(candidate)], check=True, capture_output=True)
        if path.read_text() != before:
            raise RuntimeError('config changed during sniff deployment')
        stat = path.stat()
        path.write_text(candidate.read_text())
        os.chown(path, stat.st_uid, stat.st_gid)
        try:
            subprocess.run(['systemctl', 'restart', unit], check=True)
            time.sleep(2)
            subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=True)
        except Exception:
            path.write_text(before)
            subprocess.run(['systemctl', 'restart', unit], check=True)
            raise
        print(unit + ': HTTP/TLS/QUIC sniffing enabled; backup ' + str(backup))


if __name__ == '__main__':
    main()
