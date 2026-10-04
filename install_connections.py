#!/usr/bin/env python3
"""Install the optional central live JSON view. Sources/settings arrive on stdin."""
import argparse
import ipaddress
import json
import os
import pathlib
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path('/etc/sing-box-connections')
APP = pathlib.Path('/opt/sing-box-connections')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-stdin', required=True, action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    config = json.load(sys.stdin)
    address, port = config.get('listen', '127.0.0.1:9121').rsplit(':', 1)
    ip = ipaddress.ip_address(address)
    if not (ip.is_loopback or ip.is_private) or ip.is_unspecified or ip.is_multicast or not 1024 <= int(port) <= 65535:
        parser.error('bind explicitly to a private/loopback IP on an unprivileged port')
    if not 1 <= len(config['sources']) <= 16:
        parser.error('expected 1..16 sources')
    for source in config['sources']:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', source['host']):
            parser.error('invalid source host label')
    try:
        account = pwd.getpwnam('sing-box-connections')
    except KeyError:
        subprocess.run(['useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', 'sing-box-connections'], check=True)
        account = pwd.getpwnam('sing-box-connections')
    for path in (ROOT, APP):
        path.mkdir(exist_ok=True)
        os.chown(path, 0, account.pw_gid)
        os.chmod(path, 0o750)
    path = ROOT / 'config.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    if previous:
        backup = ROOT / ('config.backup-' + str(time.time_ns()) + '.json')
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    config['token'] = previous.get('token', secrets.token_urlsafe(32))
    for source in config['sources']:
        for field, extension in [('ca_file', 'crt'), ('token_file', 'token')]:
            origin = pathlib.Path(source[field])
            target = ROOT / (source['host'] + '.' + extension)
            if origin.resolve() != target.resolve():
                shutil.copyfile(origin, target)
            os.chown(target, 0, account.pw_gid)
            os.chmod(target, 0o640)
            source[field] = str(target)
    shutil.copyfile(pathlib.Path(__file__).with_name('connections_view.py'), APP / 'connections_view.py')
    os.chmod(APP / 'connections_view.py', 0o644)
    # Validate CA/token/URL settings before replacing the running configuration.
    from connections_view import View
    view = View(config)
    view.pool.shutdown()
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o640)
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream, indent=2)
    os.chown(path, 0, account.pw_gid)
    unit = pathlib.Path('/etc/systemd/system/sing-box-connections.service')
    unit.write_text('''[Unit]
Description=sing-box live connection JSON view
After=network-online.target
Wants=network-online.target
[Service]
User=sing-box-connections
Group=sing-box-connections
ExecStart=/usr/bin/python3 /opt/sing-box-connections/connections_view.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=96M
TasksMax=24
LimitNOFILE=128
Nice=10
[Install]
WantedBy=multi-user.target
''')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'sing-box-connections.service'], check=True)
    subprocess.run(['systemctl', 'restart', 'sing-box-connections.service'], check=True)
    print('Central live connection view installed at ' + config.get('listen', '127.0.0.1:9121'))


if __name__ == '__main__':
    main()
