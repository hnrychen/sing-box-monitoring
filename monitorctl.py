#!/usr/bin/env python3
"""One native management entry point for sing-box monitoring helpers.

The sing-box and mainland-route collectors retain separate processes,
configuration and privileges. Proxy cores and central infrastructure are outside
the management scope.
"""
import argparse
import json
import os
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

COMPONENTS = {
    'exporter': ('sing-box-exporter.service', '/opt/sing-box-exporter', ('exporter.py', 'tcpdiag.py')),
    'ipinfo': ('sing-box-ipinfo.service', '/opt/sing-box-ipinfo', ('ipinfo_exporter.py',)),
    'connections': ('sing-box-connections.service', '/opt/sing-box-connections', ('connections_view.py',)),
    'probes': ('sing-box-probe.service', '/opt/sing-box-probe', ('proxy_probe.py',)),
    'sessions': ('sing-box-session-guard.timer', '/opt/sing-box-session-guard', ('session_guard.py', 'tcpdiag.py')),
    'routes': ('route-monitor.service', '/opt/route-monitor', ('collector.py', 'common.py')),
}
BUNDLE = pathlib.Path('/opt/sing-box-monitoring')
BUNDLE_FILES = ('monitorctl.py', 'install.py', 'exporter.py', 'tcpdiag.py', 'install_ipinfo.py',
    'ipinfo_exporter.py', 'install_connections.py', 'connections_view.py', 'install_proxy_probe.py',
    'proxy_probe.py', 'integrate_probe_prometheus.py', 'register_central_host.py',
    'session_guard.py', 'install_session_guard.py', 'repair_tcp_uids.py', 'native_exporter.py')
ROUTE_FILES = ('production.py', 'collector.py', 'common.py', 'route-monitor.service')
GROUP = 'sing-box-monitoring.target'
BACKUPS = pathlib.Path('/var/backups/sing-box-monitoring')
UNIT_DIR = pathlib.Path('/etc/systemd/system')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def selected(name):
    return list(COMPONENTS) if name == 'all' else [name]


def installed(component):
    unit, app, files = COMPONENTS[component]
    state = run('systemctl', 'show', unit, '-p', 'LoadState', '--value')
    if state == 'not-found':
        return False
    command = run('systemctl', 'show', unit.replace('.timer', '.service'), '-p', 'ExecStart', '--value')
    relative_route = (component == 'routes' and 'collector.py --config /etc/route-monitor/config.json' in command
                      and run('systemctl', 'show', unit, '-p', 'WorkingDirectory', '--value') == app)
    native_exporter = component == 'exporter' and app + '/sing-box-exporter-go --config /etc/sing-box-exporter/config.json' in command
    if app + '/' + files[0] not in command and not relative_route and not native_exporter:
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


def route_source(source, explicit=None):
    candidates = [explicit] if explicit is not None else [source / 'route-monitoring', source.parent / 'route-monitoring']
    return next((path.resolve() for path in candidates if (path / 'production.py').is_file()), None)


def bootstrap(source, routes=None):
    source = source.resolve()
    routes = route_source(source, routes)
    # Validate the whole bundle before changing installed management files.
    for filename in BUNDLE_FILES:
        compile((source / filename).read_text(), str(source / filename), 'exec')
    if routes:
        for filename in ROUTE_FILES:
            if filename.endswith('.py'):
                compile((routes / filename).read_text(), str(routes / filename), 'exec')
            else:
                (routes / filename).read_text()
    enabled = [name for name in COMPONENTS if installed(name)]
    paths = [BUNDLE / filename for filename in BUNDLE_FILES]
    paths += [pathlib.Path('/usr/local/sbin/sing-box-monitor'), pathlib.Path('/etc/systemd/system/' + GROUP)]
    paths += [pathlib.Path('/etc/systemd/system') / (COMPONENTS[name][0] + '.d') / 'monitoring-group.conf'
              for name in enabled]
    if routes:
        paths += [BUNDLE / 'route-monitoring' / filename for filename in ROUTE_FILES]
    backup = BACKUPS / ('management-' + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    inventory = []
    for index, path in enumerate(paths):
        if path.is_symlink():
            raise ValueError('refusing symlink management path: ' + str(path))
        if path.exists():
            saved = backup / str(index)
            shutil.copy2(path, saved)
            saved.chmod(0o600)
            inventory.append(dict(path=str(path), saved=str(index), mode=path.stat().st_mode & 0o777))
        else:
            inventory.append(dict(path=str(path), saved=None))
    (backup / 'inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
    BUNDLE.mkdir(parents=True, exist_ok=True, mode=0o755)
    for filename in BUNDLE_FILES:
        origin = source / filename
        compile(origin.read_text(), str(origin), 'exec')
        if origin.resolve() != (BUNDLE / filename).resolve():
            atomic_copy(origin, BUNDLE / filename)
    if routes:
        destination = BUNDLE / 'route-monitoring'
        destination.mkdir(exist_ok=True)
        for filename in ROUTE_FILES:
            if (routes / filename).resolve() != (destination / filename).resolve():
                atomic_copy(routes / filename, destination / filename)
    atomic_copy(BUNDLE / 'monitorctl.py', '/usr/local/sbin/sing-box-monitor', 0o755)
    for name in enabled:
        unit = COMPONENTS[name][0]
        directory = pathlib.Path('/etc/systemd/system') / (unit + '.d')
        directory.mkdir(exist_ok=True)
        (directory / 'monitoring-group.conf').write_text('[Unit]\nPartOf=' + GROUP + '\n')
    pathlib.Path('/etc/systemd/system/' + GROUP).write_text(
        '[Unit]\nDescription=Independent sing-box and route monitoring helpers\nWants=' + ' '.join(COMPONENTS[name][0] for name in enabled) +
        '\nAfter=network-online.target\n[Install]\nWantedBy=multi-user.target\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', GROUP)
    print(json.dumps(dict(group=GROUP, components=enabled, command='/usr/local/sbin/sing-box-monitor', backup=str(backup))))


def health(component):
    if component not in ('exporter', 'routes'):
        return
    directory = pathlib.Path('/etc/sing-box-exporter' if component == 'exporter' else '/etc/route-monitor')
    config = json.loads((directory / 'config.json').read_text())
    if component == 'exporter':
        port = int(config['listen'].rsplit(':', 1)[1])
        token = config['scrape_token']
    else:
        port = int(config.get('port', 9123))
        values = dict(line.split('=', 1) for line in (directory / 'auth.env').read_text().splitlines()
                      if line and not line.startswith('#') and '=' in line)
        token = values['ROUTE_MONITOR_TOKEN']
    context = ssl.create_default_context(cafile=config['tls_cert'])
    # Loopback is the explicit local transport; the pinned CA still verifies the
    # server certificate, whose SAN belongs to the public scrape address.
    context.check_hostname = False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(f'https://127.0.0.1:{port}/healthz',
                                    headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'sing-box-monitor/1.0'})
    with opener.open(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(component + ': unhealthy collector')


def wait_healthy(component):
    for attempt in range(10):
        try:
            run('systemctl', 'is-active', '--quiet', COMPONENTS[component][0])
            health(component)
            return
        except Exception:
            if attempt == 9:
                raise
            time.sleep(1)


def upgrade(source, components, dry_run=False, routes=None):
    source = source.resolve()
    plans = []
    for name in components:
        if not installed(name):
            continue
        unit, app, files = COMPONENTS[name]
        if name == 'exporter' and '/sing-box-exporter-go ' in run('systemctl', 'show', unit, '-p', 'ExecStart', '--value'):
            if components == ['exporter']:
                raise ValueError('Go exporter: use native promote --binary PATH to upgrade the native binary')
            continue  # An all-Python bundle upgrade must not claim to upgrade Go.
        component_source = route_source(source, routes) if name == 'routes' else source
        if component_source is None:
            raise ValueError('provide --route-source for the installed route collector')
        changed = []
        for filename in files:
            origin, destination = component_source / filename, pathlib.Path(app) / filename
            compile(origin.read_text(), str(origin), 'exec')
            if origin.read_bytes() != destination.read_bytes():
                changed.append((origin, destination))
        if changed:
            plans.append((name, unit, changed))
    if dry_run:
        print(json.dumps(dict(upgrade=[name for name, _, _ in plans], config_changes=False)))
        return
    backup = BACKUPS / str(time.time_ns())
    if plans:
        backup.mkdir(parents=True, mode=0o700)
        os.chmod(backup.parent, 0o700)
    for name, unit, files in plans:
        saved = backup / name
        saved.mkdir(mode=0o700)
        if name == 'routes':
            unit_path = UNIT_DIR / unit
            if unit_path.is_symlink():
                raise ValueError('refusing symlink route unit')
            if unit_path.is_file():
                shutil.copy2(unit_path, saved / unit)
                (saved / unit).chmod(0o600)
        for _, destination in files:
            shutil.copy2(destination, saved / destination.name)
        try:
            for origin, destination in files:
                atomic_copy(origin, destination)
            run('systemctl', 'restart', unit)
            wait_healthy(name)
        except Exception:
            for _, destination in files:
                atomic_copy(saved / destination.name, destination)
            run('systemctl', 'restart', unit)
            raise
    print(json.dumps(dict(upgraded=[name for name, _, _ in plans], config_changes=False,
                         backup=str(backup) if plans else None)))


def rollback(component, backup):
    root = BACKUPS.resolve()
    backup = backup.resolve()
    if root not in backup.parents or not installed(component):
        raise ValueError('backup must be inside the managed backup directory and component must be installed')
    unit, app, files = COMPONENTS[component]
    saved = backup / component
    if saved.is_symlink():
        raise ValueError('refusing symlink backup directory')
    plan = [(saved / filename, pathlib.Path(app) / filename) for filename in files if (saved / filename).is_file()]
    unit_restored = component == 'routes' and (saved / unit).is_file()
    if unit_restored:
        plan.append((saved / unit, UNIT_DIR / unit))
    if component == 'exporter' and (saved / 'config.json').is_file():
        json.loads((saved / 'config.json').read_text())
        plan.append((saved / 'config.json', pathlib.Path('/etc/sing-box-exporter/config.json')))
    if not plan:
        raise ValueError('no backup files for this component')
    for origin, target in plan:
        if origin.is_symlink() or target.is_symlink():
            raise ValueError('refusing symlink recovery path')
        if origin.suffix == '.py':
            compile(origin.read_text(), str(origin), 'exec')
    if unit_restored:
        run('systemd-analyze', 'verify', str(saved / unit))
    recovery = root / ('pre-rollback-' + str(time.time_ns())) / component
    recovery.mkdir(parents=True, mode=0o700)
    recovery.parent.chmod(0o700)
    for _, target in plan:
        shutil.copy2(target, recovery / target.name)
        (recovery / target.name).chmod(0o600)
    try:
        for origin, target in plan:
            stat = target.stat()
            atomic_copy(origin, target, stat.st_mode & 0o777)
            os.chown(target, stat.st_uid, stat.st_gid)
        if unit_restored:
            run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', unit)
        wait_healthy(component)
    except Exception:
        for _, target in plan:
            stat = target.stat()
            atomic_copy(recovery / target.name, target, stat.st_mode & 0o777)
            os.chown(target, stat.st_uid, stat.st_gid)
        if unit_restored:
            run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', unit)
        raise
    warnings = (['Legacy route backup has no unit; only saved code was restored.']
                if component == 'routes' and not unit_restored else [])
    print(json.dumps(dict(restored=component, backup=str(backup), recovery_backup=str(recovery.parent),
                         unit_restored=unit_restored, warnings=warnings)))


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
        elif name == 'sessions':
            output = run('/usr/bin/python3', str(BUNDLE / 'install_session_guard.py'),
                         *(['--enable-cleanup'] if spec.get('enable_cleanup', False) else []))
        elif name == 'routes':
            output = run('/usr/bin/python3', str(BUNDLE / 'route-monitoring' / 'production.py'),
                         'install-collector', *spec['arguments'])
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
    command.add_argument('--route-source', type=pathlib.Path)
    command = subs.add_parser('upgrade')
    command.add_argument('component', choices=['all'] + list(COMPONENTS), default='all', nargs='?')
    command.add_argument('--source', type=pathlib.Path, default=BUNDLE)
    command.add_argument('--dry-run', action='store_true')
    command.add_argument('--route-source', type=pathlib.Path)
    command = subs.add_parser('rollback')
    command.add_argument('component', choices=list(COMPONENTS))
    command.add_argument('--backup', type=pathlib.Path, required=True)
    command = subs.add_parser('rtt')
    command.add_argument('--apply', action='store_true', help='Repair only mismatched socket UIDs')
    command = subs.add_parser('native', help='Go exporter shadow rollout, promotion and rollback')
    command.add_argument('action', choices=['shadow', 'promote', 'rollback', 'remove-shadow'])
    command.add_argument('--binary', type=pathlib.Path)
    command.add_argument('--backup', type=pathlib.Path)
    command.add_argument('--port', type=int, default=19119)
    command = subs.add_parser('check')
    command.add_argument('component', choices=['all', 'exporter', 'routes'], default='all', nargs='?')
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
        bootstrap(args.source, args.route_source)
    elif args.command == 'upgrade':
        upgrade(args.source, selected(args.component), args.dry_run, args.route_source)
    elif args.command == 'rollback':
        rollback(args.component, args.backup)
    elif args.command == 'rtt':
        print(run('/usr/bin/python3', str(BUNDLE / 'repair_tcp_uids.py'), *(['--apply'] if args.apply else [])))
    elif args.command == 'native':
        arguments = [args.action, '--port', str(args.port)]
        if args.binary:
            arguments.extend(['--binary', str(args.binary)])
        if args.backup:
            arguments.extend(['--backup', str(args.backup)])
        print(run('/usr/bin/python3', str(BUNDLE / 'native_exporter.py'), *arguments))
    elif args.command == 'check':
        for name in ('exporter', 'routes') if args.component == 'all' else [args.component]:
            if installed(name):
                wait_healthy(name)
                print(name + ': healthy (authenticated local TLS)')
            else:
                print(name + ': not installed')
    elif args.command == 'deploy':
        deploy(args.manifest)
    elif args.command == 'logs':
        if not 1 <= args.lines <= 10000:
            parser.error('lines must be 1..10000')
        units = [COMPONENTS[name][0].replace('.timer', '.service') for name in selected(args.component) if installed(name)]
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
