#!/usr/bin/env python3
"""Generic native central probe deployment; private JSON configuration via stdin."""
import argparse
import json
import os
import pathlib
import pwd
import shutil
import subprocess
import sys
import tempfile

from install import run, write
from proxy_probe import client_config, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-stdin', required=True, action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    config = json.load(sys.stdin)
    config.update(state_file='/var/lib/sing-box-probe/counters.json', runtime_dir='/run/sing-box-probe',
                  sing_box_binary=shutil.which('sing-box'), curl_binary=shutil.which('curl'))
    if not config['sing_box_binary'] or not config['curl_binary']:
        raise ValueError('sing-box and curl must already be installed')
    validate(config)
    with tempfile.TemporaryDirectory(prefix='sing-box-probe-preflight-') as directory:
        path = pathlib.Path(directory) / 'client.json'
        path.write_text(json.dumps(client_config(config)))
        os.chmod(path, 0o600)
        checked = subprocess.run([config['sing_box_binary'], 'check', '-c', str(path)], capture_output=True)
        if checked.returncode:
            raise ValueError('probe client config rejected; private stderr withheld')
    try:
        account = pwd.getpwnam('sing-box-probe')
    except KeyError:
        run('useradd', '--system', '--home-dir', '/var/lib/sing-box-probe', '--shell', '/usr/sbin/nologin', 'sing-box-probe')
        account = pwd.getpwnam('sing-box-probe')
    root, app = pathlib.Path('/etc/sing-box-probe'), pathlib.Path('/opt/sing-box-probe')
    root.mkdir(exist_ok=True, mode=0o750)
    app.mkdir(exist_ok=True, mode=0o755)
    os.chown(root, 0, account.pw_gid)
    os.chmod(root, 0o750)
    config_path = root / 'config.json'
    write(config_path, json.dumps(config, indent=2) + '\n', 0o640)
    os.chown(config_path, 0, account.pw_gid)
    shutil.copyfile(pathlib.Path(__file__).with_name('proxy_probe.py'), app / 'proxy_probe.py')
    os.chmod(app / 'proxy_probe.py', 0o644)
    write('/etc/systemd/system/sing-box-probe.service', '''[Unit]
Description=Central scheduled end-to-end sing-box HTTPS checks
After=network-online.target
Wants=network-online.target
[Service]
User=sing-box-probe
Group=sing-box-probe
ExecStart=/usr/bin/python3 /opt/sing-box-probe/proxy_probe.py --config /etc/sing-box-probe/config.json
Restart=always
RestartSec=5
RuntimeDirectory=sing-box-probe
RuntimeDirectoryMode=0700
StateDirectory=sing-box-probe
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
CapabilityBoundingSet=
LockPersonality=yes
MemoryMax=192M
CPUQuota=20%
CPUWeight=10
Nice=10
TasksMax=128
LimitNOFILE=256
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
''', 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'sing-box-probe')
    run('systemctl', 'restart', 'sing-box-probe')
    print(json.dumps(dict(targets=len(config['targets']), checks=len(config['checks']),
                         interval=config.get('interval', 60), listen=config['listen'])))


if __name__ == '__main__':
    main()
