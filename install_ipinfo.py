#!/usr/bin/env python3
"""Install the optional CENTRAL enrichment worker; never changes proxy agents."""
import argparse
import json
import os
import pathlib
import pwd
import re
import shutil
import subprocess
import sys
import time
import urllib.request


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def integrate(path, container):
    before = path.read_text()
    prefix, suffix = '# BEGIN managed source IPinfo\n', '# END managed source IPinfo\n'
    block = prefix + '''  - job_name: source-ipinfo
    scrape_interval: 5s
    scrape_timeout: 4s
    sample_limit: 0
    label_limit: 8
    body_size_limit: 1MB
    static_configs:
      - targets: ['127.0.0.1:9120']
''' + suffix
    if prefix in before:
        first, rest = before.split(prefix, 1)
        _, last = rest.split(suffix, 1)
        after = first + block + last
    else:
        if 'job_name: source-ipinfo' in before:
            raise RuntimeError('unmanaged IPinfo job; review before merging')
        start = re.search(r'(?m)^scrape_configs:\s*\n', before)
        if not start:
            raise RuntimeError('expected scrape_configs mapping')
        following = re.search(r'(?m)^[A-Za-z_][A-Za-z_0-9]*:', before[start.end():])
        index = start.end() + following.start() if following else len(before)
        after = before[:index].rstrip() + '\n' + block + '\n' + before[index:]
    info = json.loads(run('docker', 'inspect', container))[0]
    if info['HostConfig']['NetworkMode'] != 'host':
        raise RuntimeError('loopback worker integration requires host-network Prometheus; use a private network for other layouts')
    mounts = [m for m in info['Mounts'] if m['Type'] == 'bind' and m['Destination'] == '/etc/prometheus/sing-box']
    if len(mounts) != 1:
        raise RuntimeError('expected existing /etc/prometheus/sing-box bind; integrate the documented job manually')
    root = pathlib.Path(mounts[0]['Source'])
    staged = root / 'ipinfo-candidate.yml'
    staged.write_text(after)
    os.chmod(staged, 0o644)
    run('docker', 'run', '--rm', '--entrypoint', '/bin/promtool', '-v', f'{root}:/etc/prometheus/sing-box:ro',
        info['Image'], 'check', 'config', '/etc/prometheus/sing-box/ipinfo-candidate.yml')
    if path.read_text() != before:
        raise RuntimeError('Prometheus config changed during integration')
    backup = path.with_name('prometheus.ipinfo-backup-' + time.strftime('%Y%m%d%H%M%S') + '.yml')
    shutil.copy2(path, backup)
    try:
        path.write_text(after)  # Preserve inode of single-file Docker bind mount.
        run('docker', 'kill', '--signal', 'HUP', container)
    except Exception:
        path.write_text(before)
        run('docker', 'kill', '--signal', 'HUP', container)
        raise
    print('Validated and reloaded Prometheus; existing jobs/data retained.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--token-stdin', action='store_true', help='Read JSON {"token": "..."} from stdin; never pass tokens in arguments')
    parser.add_argument('--integrate-prometheus', type=pathlib.Path)
    parser.add_argument('--prometheus-container', default='prometheus')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    root, app = pathlib.Path('/etc/sing-box-ipinfo'), pathlib.Path('/opt/sing-box-ipinfo')
    unit_path = pathlib.Path('/etc/systemd/system/sing-box-ipinfo.service')
    if unit_path.exists() and '/opt/sing-box-ipinfo/ipinfo_exporter.py' not in unit_path.read_text():
        raise RuntimeError('existing service is not owned by this installer')
    try:
        account = pwd.getpwnam('sing-box-ipinfo')
    except KeyError:
        run('useradd', '--system', '--home', '/var/lib/sing-box-ipinfo', '--shell', '/usr/sbin/nologin', 'sing-box-ipinfo')
        account = pwd.getpwnam('sing-box-ipinfo')
    root.mkdir(exist_ok=True)
    os.chown(root, 0, account.pw_gid)
    os.chmod(root, 0o750)
    token_path = root / 'ipinfo.token'
    if args.token_stdin:
        token = json.load(sys.stdin)['token'].strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError('invalid token')
        with os.fdopen(os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640), 'w') as stream:
            stream.write(token + '\n')
        os.chown(token_path, 0, account.pw_gid)
        os.chmod(token_path, 0o640)
    elif not token_path.exists():
        parser.error('--token-stdin is required for first installation')
    config_path = root / 'config.json'
    config = json.loads(config_path.read_text()) if config_path.exists() else dict(
        listen='127.0.0.1:9120', prometheus_url='http://127.0.0.1:9090',
        token_file=str(token_path), cache_file='/var/lib/sing-box-ipinfo/cache.json', cache_ttl=604800)
    config.pop('max_ips', None)
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    os.chown(config_path, 0, account.pw_gid)
    os.chmod(config_path, 0o640)
    app.mkdir(exist_ok=True)
    shutil.copyfile(pathlib.Path(__file__).with_name('ipinfo_exporter.py'), app / 'ipinfo_exporter.py')
    os.chmod(app / 'ipinfo_exporter.py', 0o644)
    unit_path.write_text('''[Unit]
Description=Central cached IPinfo Lite source enrichment
After=network-online.target
Wants=network-online.target

[Service]
User=sing-box-ipinfo
Group=sing-box-ipinfo
ExecStart=/usr/bin/python3 /opt/sing-box-ipinfo/ipinfo_exporter.py
Restart=always
RestartSec=10
StateDirectory=sing-box-ipinfo
StateDirectoryMode=0700
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
MemoryMax=64M
CPUQuota=5%
CPUWeight=10
Nice=10
TasksMax=8
LimitNOFILE=64

[Install]
WantedBy=multi-user.target
''')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'sing-box-ipinfo')
    run('systemctl', 'restart', 'sing-box-ipinfo')
    for attempt in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:9120/metrics', timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('worker did not become ready; inspect sing-box-ipinfo.service')
    if args.integrate_prometheus:
        integrate(args.integrate_prometheus, args.prometheus_container)
    print('Central worker installed; token private; proxy services untouched.')


if __name__ == '__main__':
    main()
