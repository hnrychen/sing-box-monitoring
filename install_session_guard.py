#!/usr/bin/env python3
"""Install an opt-in cleanup timer; proxy services and exporter permissions stay unchanged."""
import argparse
import os
import pathlib
import subprocess
from monitorctl import atomic_copy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-cleanup', action='store_true', help='explicitly allow selective FIN-WAIT-2 closure')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    app = pathlib.Path('/opt/sing-box-session-guard')
    app.mkdir(mode=0o755, exist_ok=True)
    source = pathlib.Path(__file__).resolve().parent
    for name in ('session_guard.py', 'tcpdiag.py'):
        compile((source / name).read_text(), name, 'exec')
        atomic_copy(source / name, app / name)
    unit = f'''[Unit]
Description=Conservative sing-box half-closed session guard
PartOf=sing-box-monitoring.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {app}/session_guard.py{' --apply' if args.enable_cleanup else ''}
User=root
StateDirectory=sing-box-session-guard
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_NETLINK AF_UNIX
CapabilityBoundingSet=CAP_NET_ADMIN CAP_SYS_PTRACE CAP_DAC_READ_SEARCH
MemoryMax=64M
CPUQuota=2%
Nice=19
TimeoutStartSec=120
'''
    timer = '''[Unit]
Description=Check sing-box half-closed sessions every five minutes
PartOf=sing-box-monitoring.target
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=15s
Unit=sing-box-session-guard.service
[Install]
WantedBy=sing-box-monitoring.target
'''
    # Preserve original units for a narrow rollback before reconfiguration.
    import time
    backup = pathlib.Path('/var/backups/sing-box-session-guard') / str(time.time_ns())
    for name, content in (('sing-box-session-guard.service', unit), ('sing-box-session-guard.timer', timer)):
        target = pathlib.Path('/etc/systemd/system') / name
        if target.exists():
            backup.mkdir(parents=True, mode=0o700, exist_ok=True)
            atomic_copy(target, backup / name, 0o600)
        with __import__('tempfile').NamedTemporaryFile(mode='w', dir=app, delete=False) as stream:
            stream.write(content)
            temporary = pathlib.Path(stream.name)
        atomic_copy(temporary, target)
        temporary.unlink()
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'sing-box-session-guard.timer'], check=True)
    subprocess.run(['systemctl', 'start', 'sing-box-session-guard.service'], check=True)
    print('Session guard installed: ' + ('cleanup enabled' if args.enable_cleanup else 'audit only'))


if __name__ == '__main__':
    main()
