#!/usr/bin/env python3
"""Standardize one Shadowsocks listener tag without changing credentials/routes."""
import argparse
import copy
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time


def normalize(config, tag='shadowsocks-in'):
    result = copy.deepcopy(config)
    listeners = [i for i in result.get('inbounds', []) if i['type'] == 'shadowsocks']
    if len(listeners) != 1:
        raise ValueError('requires exactly one Shadowsocks listener per service')
    old = listeners[0].get('tag')
    if not old:
        raise ValueError('listener requires an existing tag')
    if any(i.get('tag') == tag and i is not listeners[0] for i in result['inbounds']):
        raise ValueError('target tag conflicts with another listener')
    listeners[0]['tag'] = tag
    def selectors(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'inbound':
                    if isinstance(item, str) and item == old:
                        value[key] = tag
                    elif isinstance(item, list):
                        value[key] = [tag if name == old else name for name in item]
                else:
                    selectors(item)
        elif isinstance(value, list):
            for item in value:
                selectors(item)
    for section in ('route', 'dns'):
        selectors(result.get(section, {}))
    return result, old


def replace(path, content, stat):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = pathlib.Path(stream.name)
        stream.write(content)
    os.chmod(temporary, stat.st_mode & 0o777)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, help='UNIT=/absolute/current/config.json')
    parser.add_argument('--tag', default='shadowsocks-in')
    args = parser.parse_args()
    unit, sep, filename = args.service.partition('=')
    if os.geteuid() != 0 or not sep or not re.fullmatch(r'[A-Za-z0-9_.@:-]+', unit):
        parser.error('run as root with UNIT=/absolute/config.json')
    path = pathlib.Path(filename)
    if not path.is_absolute() or path.is_symlink() or not re.fullmatch(r'[A-Za-z0-9_.:-]+', args.tag):
        parser.error('invalid tag/config path')
    subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=True)
    before, stat = path.read_bytes(), path.stat()
    config = json.loads(before)
    after, old = normalize(config, args.tag)
    if after == config:
        print(json.dumps(dict(service=unit, tag=args.tag, changed=False)))
        return
    with tempfile.TemporaryDirectory(prefix='sing-box-tag-check-') as directory:
        candidate = pathlib.Path(directory) / 'config.json'
        candidate.write_text(json.dumps(after))
        candidate.chmod(0o600)
        checked = subprocess.run(['sing-box', 'check', '-D', '/var/lib/' + unit, '-c', str(candidate)], capture_output=True)
        if checked.returncode:
            raise ValueError('candidate rejected; private stderr withheld')
    exporter = pathlib.Path('/etc/sing-box-exporter/config.json')
    export_before, export_after, export_stat = None, None, None
    if exporter.exists():
        export_before, export_stat = exporter.read_bytes(), exporter.stat()
        export_after = json.loads(export_before)
        for service in export_after['services']:
            if service['name'] == unit and old in service.get('inbounds', {}):
                if args.tag in service['inbounds']:
                    raise ValueError('exporter inventory tag conflict')
                service['inbounds'][args.tag] = service['inbounds'].pop(old)
    backup = pathlib.Path('/var/backups/sing-box-inbound-tags') / str(time.time_ns())
    backup.mkdir(mode=0o700, parents=True)
    backup.parent.chmod(0o700)
    shutil.copy2(path, backup / 'core.json')
    (backup / 'core.json').chmod(0o600)
    if export_before is not None:
        shutil.copy2(exporter, backup / 'exporter.json')
        (backup / 'exporter.json').chmod(0o600)
    if path.read_bytes() != before or (export_before is not None and exporter.read_bytes() != export_before):
        raise RuntimeError('current configurations changed during preparation')
    try:
        replace(path, (json.dumps(after, indent=2) + '\n').encode(), stat)
        subprocess.run(['systemctl', 'restart', unit], check=True)
        time.sleep(1)
        subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=True)
        if export_after is not None:
            replace(exporter, (json.dumps(export_after, indent=2) + '\n').encode(), export_stat)
            subprocess.run(['systemctl', 'restart', 'sing-box-exporter'], check=True)
            time.sleep(1)
            subprocess.run(['systemctl', 'is-active', '--quiet', 'sing-box-exporter'], check=True)
    except Exception:
        replace(path, before, stat)
        subprocess.run(['systemctl', 'restart', unit], check=False)
        if export_before is not None:
            replace(exporter, export_before, export_stat)
            subprocess.run(['systemctl', 'restart', 'sing-box-exporter'], check=False)
        raise
    print(json.dumps(dict(service=unit, old=old, tag=args.tag, changed=True, backup=str(backup))))


if __name__ == '__main__':
    main()
