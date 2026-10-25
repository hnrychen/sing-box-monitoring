#!/usr/bin/env python3
"""Audit or repair only exporter socket UIDs; never restart proxy cores."""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time

BACKUP_ROOT = pathlib.Path('/var/backups/sing-box-monitoring')


def service_socket_uid(name):
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+', name):
        raise ValueError('invalid service name')
    def main_pid():
        return int(subprocess.check_output(
            ['systemctl', 'show', name, '-p', 'MainPID', '--value'], text=True).strip())
    for _ in range(3):
        pid = main_pid()
        if pid <= 0:
            raise RuntimeError(name + ': no running main process')
        try:
            status = pathlib.Path(f'/proc/{pid}/status').read_text()
        except FileNotFoundError:
            continue
        # /proc/PID ownership can be root for a non-dumpable process. Socket
        # creation uses the filesystem UID, the fourth Uid field, instead.
        fields = next((line.split()[1:] for line in status.splitlines()
                       if line.startswith('Uid:')), [])
        if len(fields) != 4 or any(not value.isdecimal() for value in fields):
            raise RuntimeError(name + ': invalid process UID information')
        if main_pid() == pid:
            return int(fields[3])
    raise RuntimeError(name + ': process changed during UID discovery')


def atomic_config(path, content, stat):
    path = pathlib.Path(path)
    if path.is_symlink():
        raise ValueError('refusing symlink configuration')
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = pathlib.Path(stream.name)
        stream.write(content)
    try:
        os.chmod(temporary, stat.st_mode & 0o777)
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def repair(path, apply=False):
    path = pathlib.Path(path)
    if path.is_symlink():
        raise ValueError('refusing symlink configuration')
    before, stat = path.read_bytes(), path.stat()
    config = json.loads(before)
    changes = []
    for service in config['services']:
        uid = service_socket_uid(service['name'])
        if service.get('tcp_uid') != uid:
            changes.append(dict(service=service['name'], before=service.get('tcp_uid'), after=uid))
            service['tcp_uid'] = uid
    result = dict(tcp_rtt=config.get('tcp_rtt', False), changes=changes, applied=False)
    if not apply or not changes:
        return result
    if not config.get('tcp_rtt', False):
        raise ValueError('TCP RTT is disabled; review exporter deployment settings')
    backup = BACKUP_ROOT / ('tcp-uid-' + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    saved = backup / 'exporter'
    saved.mkdir(mode=0o700)
    shutil.copy2(path, saved / 'config.json')
    (saved / 'config.json').chmod(0o600)
    if path.read_bytes() != before:
        raise RuntimeError('configuration changed during UID repair')
    after = (json.dumps(config, indent=2) + '\n').encode()
    atomic_config(path, after, stat)
    try:
        subprocess.run(['systemctl', 'restart', 'sing-box-exporter.service'], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'sing-box-exporter.service'], check=True)
    except Exception:
        if path.read_bytes() == after:
            atomic_config(path, before, stat)
            subprocess.run(['systemctl', 'restart', 'sing-box-exporter.service'], check=False)
        raise
    return dict(result, applied=True, backup=str(backup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=pathlib.Path, default=pathlib.Path('/etc/sing-box-exporter/config.json'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root to inspect service identities and protected configuration')
    print(json.dumps(repair(args.config, args.apply)))


if __name__ == '__main__':
    main()
