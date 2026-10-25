#!/usr/bin/env python3
"""Recoverable Go exporter rollout, keeping the existing account/config/TLS/unit.

Shadow mode is loopback-only and never enrolled in Prometheus. Promotion changes
only the collector's ExecStart; it does not restart proxy cores or route monitors.
"""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request

APP = pathlib.Path('/opt/sing-box-exporter')
BINARY = APP / 'sing-box-exporter-go'
CONFIG = pathlib.Path('/etc/sing-box-exporter/config.json')
SHADOW_CONFIG = CONFIG.with_name('go-shadow.json')
DROPIN = pathlib.Path('/etc/systemd/system/sing-box-exporter.service.d/implementation.conf')
SHADOW_UNIT = pathlib.Path('/etc/systemd/system/sing-box-exporter-shadow.service')
BACKUPS = pathlib.Path('/var/backups/sing-box-monitoring')
UNIT = 'sing-box-exporter.service'
SHADOW = 'sing-box-exporter-shadow.service'


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


def atomic_bytes(target, data, mode=0o644, owner=None):
    target = pathlib.Path(target)
    if target.is_symlink():
        raise ValueError('refusing symlink target')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = pathlib.Path(stream.name)
        stream.write(data)
    try:
        temporary.chmod(mode)
        if owner:
            os.chown(temporary, *owner)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def validate_binary(path):
    path = pathlib.Path(path).resolve(strict=True)
    header = path.read_bytes()[:20]
    if len(header) < 20 or header[:5] != b'\x7fELF\x02' or header[5] != 1:
        raise ValueError('expected a Linux 64-bit little-endian executable')
    machine = int.from_bytes(header[18:20], 'little')
    architectures = {62: 'x86_64', 183: 'aarch64'}
    if machine not in architectures:
        raise ValueError('expected a Linux amd64 or arm64 executable')
    if run('uname', '-m') != architectures[machine]:
        raise ValueError('binary architecture differs from host')
    version = run(str(path), '--version')
    if not version.endswith('-go') or '\n' in version:
        raise ValueError('unexpected exporter version')
    return path, version


def production():
    import pwd
    command = run('systemctl', 'show', UNIT, '-p', 'ExecStart', '--value')
    if str(APP / 'exporter.py') not in command and str(BINARY) not in command:
        raise ValueError('refusing nonstandard production exporter')
    account = run('systemctl', 'show', UNIT, '-p', 'User', '--value')
    if account != 'sing-box-exporter':
        raise ValueError('unexpected exporter service account')
    return pwd.getpwnam(account)


def health(config_path=CONFIG, unit=UNIT):
    config = json.loads(config_path.read_text())
    context = ssl.create_default_context(cafile=config['tls_cert'])
    context.check_hostname = False  # Local loopback, pinned live exporter cert.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    port = int(config['listen'].rsplit(':', 1)[1])
    for attempt in range(20):
        try:
            run('systemctl', 'is-active', '--quiet', unit)
            req = urllib.request.Request(f'https://127.0.0.1:{port}/healthz',
                                         headers={'Authorization': 'Bearer ' + config['scrape_token']})
            with opener.open(req, timeout=4) as response:
                if response.status == 200:
                    return
        except Exception:
            if attempt == 19:
                raise RuntimeError('collector failed authenticated health check') from None
            time.sleep(1)


def backup():
    root = BACKUPS / ('native-exporter-' + str(time.time_ns()))
    root.mkdir(parents=True, mode=0o700)
    BACKUPS.chmod(0o700)
    entries = []
    for target in (BINARY, DROPIN):
        if target.is_symlink():
            raise ValueError('refusing symlink target')
        entry = dict(target=str(target), exists=target.exists())
        if target.exists():
            stat = target.stat()
            entry.update(mode=stat.st_mode & 0o777, uid=stat.st_uid, gid=stat.st_gid)
            shutil.copy2(target, root / target.name)
        entries.append(entry)
    (root / 'manifest.json').write_text(json.dumps(entries))
    return root


def validate_backup(root, go_only=False):
    root = pathlib.Path(root).resolve(strict=True)
    if BACKUPS.resolve() not in root.parents or not root.name.startswith('native-exporter-'):
        raise ValueError('expected managed native-exporter backup')
    entries = json.loads((root / 'manifest.json').read_text())
    if {entry['target'] for entry in entries} != {str(BINARY), str(DROPIN)}:
        raise ValueError('unexpected restore targets')
    if go_only:
        # Refuse old Python-only backups before touching the running Go service.
        binary = next(entry for entry in entries if entry['target'] == str(BINARY))
        if not binary['exists']:
            raise ValueError('Go-only rollback requires a previous Go binary')
        validate_binary(root / BINARY.name)
        dropin = next(entry for entry in entries if entry['target'] == str(DROPIN))
        unit = (root / DROPIN.name if dropin['exists'] else
                pathlib.Path('/etc/systemd/system/sing-box-exporter.service')).read_text()
        if str(BINARY) not in unit or 'exporter.py' in unit:
            raise ValueError('Go-only rollback requires a Go startup command')
    return root, entries


def restore(root, go_only=False):
    root, entries = validate_backup(root, go_only)
    for entry in entries:
        target = pathlib.Path(entry['target'])
        if target.is_symlink():
            raise ValueError('refusing symlink recovery target')
        if entry['exists']:
            atomic_bytes(target, (root / target.name).read_bytes(), entry['mode'], (entry['uid'], entry['gid']))
        else:
            target.unlink(missing_ok=True)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'restart', UNIT)
    health()


def shadow(binary, port=19119):
    account = production()
    origin, version = validate_binary(binary)
    if not 1024 <= port <= 65535 or port == int(json.loads(CONFIG.read_text())['listen'].rsplit(':', 1)[1]):
        raise ValueError('invalid shadow port')
    if SHADOW_UNIT.exists() and 'Managed sing-box exporter comparison' not in SHADOW_UNIT.read_text():
        raise ValueError('refusing unrelated shadow unit')
    if BINARY.exists() and str(BINARY) in run('systemctl', 'show', UNIT, '-p', 'ExecStart', '--value'):
        raise ValueError('shadow is for the Python-to-Go rollout; use native promote for subsequent Go upgrades')
    with socket.socket() as check:
        check.bind(('127.0.0.1', port))
    atomic_bytes(BINARY, origin.read_bytes(), 0o755)
    config = json.loads(CONFIG.read_text());config['listen'] = '127.0.0.1:' + str(port)
    atomic_bytes(SHADOW_CONFIG, json.dumps(config).encode(), 0o640, (0, account.pw_gid))
    atomic_bytes(SHADOW_UNIT, ('''[Unit]
Description=Managed sing-box exporter comparison
After=network-online.target sing-box-exporter-access.service
Requires=sing-box-exporter-access.service
[Service]
User=sing-box-exporter
Group=sing-box-exporter
ExecStart=/opt/sing-box-exporter/sing-box-exporter-go --config /etc/sing-box-exporter/go-shadow.json
Environment=GOMAXPROCS=1 GOMEMLIMIT=40MiB
Restart=on-failure
RestartSec=3
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
MemoryMax=64M
CPUWeight=10
Nice=10
TasksMax=16
LimitNOFILE=128
UMask=0077
''').encode())
    try:
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', SHADOW)
        health(SHADOW_CONFIG, SHADOW)
    except Exception:
        remove_shadow()
        raise
    print(json.dumps(dict(shadow='healthy', version=version, port=port)))


def promote(binary):
    production();origin, version = validate_binary(binary)
    saved = backup()
    try:
        atomic_bytes(BINARY, origin.read_bytes(), 0o755)
        atomic_bytes(DROPIN, ('[Service]\nExecStart=\nExecStart=' + str(BINARY) +
                              ' --config ' + str(CONFIG) + '\nEnvironment=GOMAXPROCS=1 GOMEMLIMIT=40MiB\n').encode())
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', UNIT)
        health()
    except Exception:
        restore(saved)
        raise
    print(json.dumps(dict(promoted=version, backup=str(saved), sha256=hashlib.sha256(BINARY.read_bytes()).hexdigest())))


def remove_shadow():
    if SHADOW_UNIT.exists():
        if 'Managed sing-box exporter comparison' not in SHADOW_UNIT.read_text():
            raise ValueError('refusing unrelated shadow unit')
        run('systemctl', 'stop', SHADOW)
        SHADOW_UNIT.unlink()
        SHADOW_CONFIG.unlink(missing_ok=True)
        run('systemctl', 'daemon-reload')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('shadow', 'promote', 'rollback', 'remove-shadow'))
    parser.add_argument('--binary', type=pathlib.Path)
    parser.add_argument('--backup', type=pathlib.Path)
    parser.add_argument('--port', type=int, default=19119)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    if args.action in ('shadow', 'promote') and not args.binary:
        parser.error('--binary is required')
    if args.action == 'shadow':
        shadow(args.binary, args.port)
    elif args.action == 'promote':
        promote(args.binary)
    elif args.action == 'rollback':
        if not args.backup:
            parser.error('--backup is required')
        validate_backup(args.backup, go_only=True)
        recovery = backup()
        try:
            restore(args.backup, go_only=True)
        except Exception:
            restore(recovery)
            raise
        print(json.dumps(dict(restored=str(args.backup), recovery_backup=str(recovery))))
    else:
        remove_shadow()
        print(json.dumps(dict(shadow='removed')))


if __name__ == '__main__':
    main()
