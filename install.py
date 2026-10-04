#!/usr/bin/env python3
"""Idempotent native deployment. Run as root on Debian/Ubuntu, Python >=3.11."""
import argparse
import grp
import hashlib
import ipaddress
import json
import os
import pathlib
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request

ROOT = pathlib.Path('/etc/sing-box-exporter')
APP = pathlib.Path('/opt/sing-box-exporter')
STATE = pathlib.Path('/var/lib/sing-box-exporter')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def write(path, text, mode=0o640):
    path = pathlib.Path(path)
    temp = path.with_name(path.name + '.monitor-new')
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    os.chmod(temp, mode)
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor-ip', required=True)
    parser.add_argument('--certificate-ip', required=True, help='IP used by Prometheus for HTTPS verification')
    parser.add_argument('--listen', default='0.0.0.0:9119')
    parser.add_argument('--source-mode', choices=['off', 'subnet', 'raw'], default='off')
    parser.add_argument('--source-idle-ttl', type=int, default=3600, help='Seconds to retain inactive source counters; no IP-count limit')
    parser.add_argument('--poll-interval', type=int, default=5)
    parser.add_argument('--tcp-rtt', action='store_true', help='Enable unprivileged Linux TCP RTT snapshots')
    parser.add_argument('--connection-details', action='store_true', help='Enable authenticated current destination JSON; never metric labels')
    parser.add_argument('--tcp-poll-interval', type=int, default=30)
    parser.add_argument('--process-interval', type=int, default=30)
    parser.add_argument('--cpu-quota', type=int, default=5, help='Maximum percent of one CPU core; 0 disables the cap')
    parser.add_argument('--service', action='append', metavar='UNIT=/ABSOLUTE/CONFIG.JSON',
                        help='Repeat for custom systemd services; defaults to sing-box and sing-box-cn')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    monitor = str(ipaddress.IPv4Address(args.monitor_ip))
    certificate_ip = str(ipaddress.IPv4Address(args.certificate_ip))
    if args.source_idle_ttl < 60:
        parser.error('--source-idle-ttl must be at least 60 seconds')
    if any(not 2 <= v <= 300 for v in (args.poll_interval, args.tcp_poll_interval, args.process_interval)) or not 0 <= args.cpu_quota <= 100:
        parser.error('intervals must be 2..300 and CPU quota 0..100')
    services = [('sing-box', '/etc/sing-box/config.json'), ('sing-box-cn', '/etc/sing-box-cn/config.json')]
    if args.service:
        services = []
        for value in args.service:
            name, separator, path = value.partition('=')
            if not separator or not re.fullmatch(r'[A-Za-z0-9_.@:-]+', name) or not pathlib.Path(path).is_absolute():
                parser.error('--service must be UNIT=/absolute/config.json')
            services.append((name, path))
    if len(services) > 16 or len({s[0] for s in services}) != len(services):
        parser.error('at most 16 unique services are supported')
    host, port = args.listen.rsplit(':', 1)
    ipaddress.IPv4Address(host)
    if int(port) != 9119:
        parser.error('deployment firewall is fixed to port 9119')
    for binary in ('sing-box', 'openssl', 'nft', 'systemctl'):
        if not shutil.which(binary):
            parser.error('missing dependency: ' + binary)
    try:
        account = pwd.getpwnam('sing-box-exporter')
    except KeyError:
        run('useradd', '--system', '--home-dir', str(STATE), '--shell', '/usr/sbin/nologin', 'sing-box-exporter')
        account = pwd.getpwnam('sing-box-exporter')
    for directory in (ROOT, APP, STATE):
        directory.mkdir(parents=True, exist_ok=True)
    os.chown(STATE, account.pw_uid, account.pw_gid)
    os.chmod(ROOT, 0o750)
    os.chown(ROOT, 0, account.pw_gid)
    config_path = ROOT / 'config.json'
    old_config = json.loads(config_path.read_text()) if config_path.exists() else {}
    config = {'listen': args.listen, 'scrape_token': old_config.get('scrape_token', secrets.token_urlsafe(32)),
              'tls_cert': str(ROOT / 'server.crt'), 'tls_key': str(ROOT / 'server.key'),
              'poll_interval': args.poll_interval, 'source_mode': args.source_mode, 'source_idle_ttl': args.source_idle_ttl,
              'tcp_rtt': args.tcp_rtt, 'tcp_poll_interval': args.tcp_poll_interval, 'process_interval': args.process_interval,
              'connection_details': args.connection_details or old_config.get('connection_details', False),
              'max_connections': 4096, 'services': []}
    if args.tcp_rtt:
        # Preflight before changing any proxy config; retain explicit budgets.
        inventory = [i for _, path in services if pathlib.Path(path).exists()
                     for i in json.loads(pathlib.Path(path).read_text()).get('inbounds', [])]
        tcp_ports = {i.get('listen_port', 0) for i in inventory if i.get('listen_port', 0)
                     and i['type'] not in ('hysteria', 'hysteria2', 'tuic') and i.get('network') != 'udp'}
        if len(tcp_ports) > 32 or len(inventory) > 128:
            parser.error('inventory exceeds 32 TCP ports / 128 inbounds; review collector budgets first')
    changes = []
    backups = []
    try:
        for index, (name, filename) in enumerate(services):
            path = pathlib.Path(filename)
            if not path.exists() or subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode:
                if args.service:
                    raise RuntimeError(name + ': explicit service must be active and its config must exist')
                continue
            # Always read the current file; never use a backup as the baseline.
            before = path.read_bytes()
            stat = path.stat()
            data = json.loads(before)
            experimental = data.setdefault('experimental', {})
            api = experimental.setdefault('clash_api', {})
            address = api.get('external_controller') or f'127.0.0.1:{19090 + index}'
            api_host, api_port = address.rsplit(':', 1)
            if api_host != '127.0.0.1':
                raise RuntimeError(f'{name}: existing non-loopback API requires manual review')
            if not api.get('external_controller'):
                with socket.socket() as probe:
                    probe.bind((api_host, int(api_port)))
            api['external_controller'] = address
            api.setdefault('secret', secrets.token_urlsafe(32))
            if not api['secret']:
                api['secret'] = secrets.token_urlsafe(32)
            if json.loads(before) != data:
                backup_root = pathlib.Path('/var/backups/sing-box-monitoring')
                backup_root.mkdir(parents=True, exist_ok=True)
                os.chmod(backup_root, 0o700)
                # -C reads every JSON in its directory: backups MUST live elsewhere.
                backup = backup_root / (name + '-' + time.strftime('%Y%m%d%H%M%S') + '.json')
                shutil.copy2(path, backup)
                os.chmod(backup, 0o600)
                backups.append(str(backup))
                if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(before).digest():
                    raise RuntimeError('configuration changed during deployment')
                changes.append((name, path, before, stat))
                write(path, json.dumps(data, indent=2) + '\n', stat.st_mode & 0o777)
                os.chown(path, stat.st_uid, stat.st_gid)
                run('sing-box', 'check', '-D', '/var/lib/' + name, '-c', str(path))
                run('systemctl', 'restart', name)
            service = {'name': name, 'url': 'http://' + address, 'secret': api['secret'],
                       'inbounds': {(i.get('tag') or i['type']): {'type': i['type'], 'port': i.get('listen_port', 0),
                                    'listen': i.get('listen', '::'), 'network': i.get('network', '')}
                                    for i in data.get('inbounds', []) + data.get('endpoints', [])},
                       'outbounds': [(o.get('tag') or o['type']) for o in data.get('outbounds', []) + data.get('endpoints', [])]}
            pid = int(run('systemctl', 'show', name, '-p', 'MainPID', '--value').stdout.strip())
            service['tcp_uid'] = os.stat(f'/proc/{pid}').st_uid
            for attempt in range(30):
                try:
                    req = urllib.request.Request(service['url'] + '/connections', headers={'Authorization': 'Bearer ' + service['secret']})
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2) as response:
                        json.load(response)
                    break
                except Exception:
                    time.sleep(0.3)
            else:
                raise RuntimeError(name + ' API did not become healthy')
            config['services'].append(service)
        if not config['services']:
            raise RuntimeError('no active sing-box services found')
    except Exception:
        for name, path, before, stat in reversed(changes):
            write(path, before.decode(), stat.st_mode & 0o777)
            os.chown(path, stat.st_uid, stat.st_gid)
            subprocess.run(['systemctl', 'restart', name], check=False)
        raise
    crt, key = ROOT / 'server.crt', ROOT / 'server.key'
    if not crt.exists():
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes', '-days', '365',
            '-subj', '/CN=sing-box-exporter', '-addext', f'subjectAltName=IP:{certificate_ip},IP:127.0.0.1',
            '-keyout', str(key), '-out', str(crt))
    write(config_path, json.dumps(config, indent=2) + '\n')
    for path in (config_path, key, crt):
        os.chown(path, 0, account.pw_gid)
        os.chmod(path, 0o640)
    source = pathlib.Path(__file__).resolve().parent
    for filename in ('exporter.py', 'tcpdiag.py'):
        shutil.copyfile(source / filename, APP / filename)
        os.chmod(APP / filename, 0o644)
    firewall = f'''table inet sing_box_exporter_access {{
 chain input {{
  type filter hook input priority -10; policy accept;
  iifname "lo" tcp dport 9119 accept
  ip saddr {monitor} tcp dport 9119 accept
  tcp dport 9119 drop
 }}
}}
'''
    write(ROOT / 'access.nft', firewall, 0o644)
    write('/etc/systemd/system/sing-box-exporter-access.service', '''[Unit]
Description=Restrict sing-box exporter to monitoring server
After=network-pre.target
Before=sing-box-exporter.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table inet sing_box_exporter_access
ExecStart=/usr/sbin/nft -f /etc/sing-box-exporter/access.nft
ExecStop=-/usr/sbin/nft delete table inet sing_box_exporter_access
[Install]
WantedBy=multi-user.target
''', 0o644)
    write('/etc/systemd/system/sing-box-exporter.service', f'''[Unit]
Description=Read-only sing-box Prometheus exporter
After=network-online.target sing-box-exporter-access.service
Requires=sing-box-exporter-access.service
[Service]
User=sing-box-exporter
Group=sing-box-exporter
ExecStart=/usr/bin/python3 /opt/sing-box-exporter/exporter.py --config /etc/sing-box-exporter/config.json
Restart=always
RestartSec=3
StateDirectory=sing-box-exporter
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX{' AF_NETLINK' if args.tcp_rtt else ''}
CapabilityBoundingSet=
LockPersonality=yes
MemoryMax=64M
{'CPUQuota=' + str(args.cpu_quota) + '%' if args.cpu_quota else '# No CPU quota requested'}
CPUWeight=10
Nice=10
TasksMax=16
LimitNOFILE=128
[Install]
WantedBy=multi-user.target
''', 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'sing-box-exporter-access', 'sing-box-exporter')
    run('systemctl', 'restart', 'sing-box-exporter-access')
    run('systemctl', 'restart', 'sing-box-exporter')
    print(json.dumps({'services': [s['name'] for s in config['services']], 'listen': args.listen,
                      'source_mode': args.source_mode, 'source_ip_limit': None,
                      'poll_interval': args.poll_interval, 'cpu_quota': args.cpu_quota,
                      'tcp_rtt': args.tcp_rtt, 'tcp_poll_interval': args.tcp_poll_interval,
                      'backups': backups}))


if __name__ == '__main__':
    main()
