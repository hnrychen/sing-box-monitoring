#!/usr/bin/env python3
"""One native management entry point for sing-box monitoring helpers.

Only these four owned services are in scope; proxy cores, Grafana, Prometheus,
node exporter and unrelated monitoring are never stopped or upgraded here.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

COMPONENTS = {
    'exporter': ('sing-box-exporter.service', '/opt/sing-box-exporter', ('exporter.py', 'tcpdiag.py')),
    'ipinfo': ('sing-box-ipinfo.service', '/opt/sing-box-ipinfo', ('ipinfo_exporter.py',)),
    'connections': ('sing-box-connections.service', '/opt/sing-box-connections', ('connections_view.py',)),
    'probes': ('sing-box-probe.service', '/opt/sing-box-probe', ('proxy_probe.py',)),
}
BUNDLE = pathlib.Path('/opt/sing-box-monitoring')
BUNDLE_FILES = ('monitorctl.py', 'install.py', 'exporter.py', 'tcpdiag.py', 'install_ipinfo.py',
    'ipinfo_exporter.py', 'install_connections.py', 'connections_view.py', 'install_proxy_probe.py',
    'proxy_probe.py', 'integrate_probe_prometheus.py')
GROUP = 'sing-box-monitoring.target'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def selected(name):
    return list(COMPONENTS) if name == 'all' else [name]


def installed(component):
    unit, app, files = COMPONENTS[component]
    state = run('systemctl', 'show', unit, '-p', 'LoadState', '--value')
    if state == 'not-found':
        return False
    command = run('systemctl', 'show', unit, '-p', 'ExecStart', '--value')
    if app + '/' + files[0] not in command:
        raise RuntimeError('refusing unrelated/nonstandard unit: ' + unit)
    return True


def atomic_copy(source, target, mode=0o644):
    target = pathlib.Path(target)
    if target.is_symlink():
        raise ValueError('refusing symlink target: ' + str(target))
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = pathlib.Path(stream.name)
        stream.write(pathlib.Path(source).read_bytes())
    os.chmod(temporary, mode)
    os.replace(temporary, target)


def bootstrap(source):
    source = source.resolve()
    BUNDLE.mkdir(parents=True, exist_ok=True, mode=0o755)
    for filename in BUNDLE_FILES:
        origin = source / filename
        compile(origin.read_text(), str(origin), 'exec')
        if origin.resolve() != (BUNDLE / filename).resolve():
            atomic_copy(origin, BUNDLE / filename)
    atomic_copy(BUNDLE / 'monitorctl.py', '/usr/local/sbin/sing-box-monitor', 0o755)
    enabled = [name for name in COMPONENTS if installed(name)]
    if not enabled:
        raise ValueError('deploy at least one component before grouping services')
    for name in enabled:
        unit = COMPONENTS[name][0]
        directory = pathlib.Path('/etc/systemd/system') / (unit + '.d')
        directory.mkdir(exist_ok=True)
        (directory / 'monitoring-group.conf').write_text('[Unit]\nPartOf=' + GROUP + '\n')
    pathlib.Path('/etc/systemd/system/' + GROUP).write_text(
        '[Unit]\nDescription=sing-box monitoring helpers\nWants=' + ' '.join(COMPONENTS[name][0] for name in enabled) +
        '\nAfter=network-online.target\n[Install]\nWantedBy=multi-user.target\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', GROUP)
    print(json.dumps(dict(group=GROUP, components=enabled, command='/usr/local/sbin/sing-box-monitor')))


def upgrade(source, components, dry_run=False):
    source = source.resolve()
    plans = []
    for name in components:
        if not installed(name):
            continue
        unit, app, files = COMPONENTS[name]
        changed = []
        for filename in files:
            origin, destination = source / filename, pathlib.Path(app) / filename
            compile(origin.read_text(), str(origin), 'exec')
            if origin.read_bytes() != destination.read_bytes():
                changed.append((origin, destination))
        if changed:
            plans.append((name, unit, changed))
    if dry_run:
        print(json.dumps(dict(upgrade=[name for name, _, _ in plans], config_changes=False)))
        return
    backup = pathlib.Path('/var/backups/sing-box-monitoring') / str(time.time_ns())
    if plans:
        backup.mkdir(parents=True, mode=0o700)
        os.chmod(backup.parent, 0o700)
    for name, unit, files in plans:
        saved = backup / name
        saved.mkdir(mode=0o700)
        for _, destination in files:
            shutil.copy2(destination, saved / destination.name)
        try:
            for origin, destination in files:
                atomic_copy(origin, destination)
            run('systemctl', 'restart', unit)
            time.sleep(1)
            run('systemctl', 'is-active', '--quiet', unit)
        except Exception:
            for _, destination in files:
                atomic_copy(saved / destination.name, destination)
            run('systemctl', 'restart', unit)
            raise
    print(json.dumps(dict(upgraded=[name for name, _, _ in plans], config_changes=False,
                         backup=str(backup) if plans else None)))


def deploy(manifest):
    data = json.loads(manifest.read_text())
    specs = data['components']
    if set(specs) - set(COMPONENTS):
        raise ValueError('unknown deployment component')
    for name, spec in specs.items():
        if name == 'exporter':
            output = run('/usr/bin/python3', str(BUNDLE / 'install.py'), *spec['arguments'])
        elif name == 'ipinfo':
            command = ['/usr/bin/python3', str(BUNDLE / 'install_ipinfo.py')]
            payload = None
            if spec.get('token_file'):
                payload = json.dumps({'token': pathlib.Path(spec['token_file']).read_text().strip()})
                command.append('--token-stdin')
            if spec.get('prometheus_config'):
                command.extend(['--integrate-prometheus', spec['prometheus_config']])
            output = run(*command, input=payload)
        else:
            filename = 'install_connections.py' if name == 'connections' else 'install_proxy_probe.py'
            # Read before the installer rewrites this same private config path.
            payload = pathlib.Path(spec['config_file']).read_text()
            output = run('/usr/bin/python3', str(BUNDLE / filename), '--config-stdin', input=payload)
            if name == 'probes' and spec.get('prometheus_config'):
                output += '\n' + run('/usr/bin/python3', str(BUNDLE / 'integrate_probe_prometheus.py'),
                                     '--path', spec['prometheus_config'])
        print(name + ': ' + output)
    bootstrap(BUNDLE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('status')
    for verb in ('start', 'stop', 'restart'):
        command = subs.add_parser(verb)
        command.add_argument('component', choices=['all'] + list(COMPONENTS), default='all', nargs='?')
    command = subs.add_parser('logs')
    command.add_argument('component', choices=['all'] + list(COMPONENTS))
    command.add_argument('--lines', type=int, default=50)
    command = subs.add_parser('bootstrap')
    command.add_argument('--source', type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent)
    command = subs.add_parser('upgrade')
    command.add_argument('component', choices=['all'] + list(COMPONENTS), default='all', nargs='?')
    command.add_argument('--source', type=pathlib.Path, default=BUNDLE)
    command.add_argument('--dry-run', action='store_true')
    command = subs.add_parser('deploy')
    command.add_argument('--manifest', type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.command not in ('status', 'logs') and os.geteuid() != 0:
        parser.error('run mutations as root')
    if args.command == 'status':
        for name in COMPONENTS:
            if installed(name):
                state = run('systemctl', 'show', COMPONENTS[name][0], '-p', 'ActiveState', '-p', 'SubState',
                            '-p', 'MemoryCurrent', '-p', 'CPUQuotaPerSecUSec', '-p', 'NRestarts')
                print(name + '\n' + state)
            else:
                print(name + ': not installed')
    elif args.command == 'bootstrap':
        bootstrap(args.source)
    elif args.command == 'upgrade':
        upgrade(args.source, selected(args.component), args.dry_run)
    elif args.command == 'deploy':
        deploy(args.manifest)
    elif args.command == 'logs':
        if not 1 <= args.lines <= 10000:
            parser.error('lines must be 1..10000')
        units = [COMPONENTS[name][0] for name in selected(args.component) if installed(name)]
        command = ['journalctl', '--no-pager', '-n', str(args.lines)]
        for unit in units:
            command.extend(['-u', unit])
        if units:
            subprocess.run(command, check=True)
    else:
        if args.component == 'all':
            run('systemctl', args.command, GROUP)
        elif installed(args.component):
            run('systemctl', args.command, COMPONENTS[args.component][0])


if __name__ == '__main__':
    main()
