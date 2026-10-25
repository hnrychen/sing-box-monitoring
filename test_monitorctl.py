import unittest
import os
import pathlib
import tempfile
import json
import io
from contextlib import redirect_stdout
from unittest.mock import patch
import monitorctl


class ManagementTests(unittest.TestCase):
    def test_scope_never_includes_proxy_or_other_infrastructure(self):
        self.assertEqual(set(monitorctl.selected('all')), {'exporter', 'ipinfo', 'connections', 'probes', 'sessions', 'routes'})
        units = {spec[0] for spec in monitorctl.COMPONENTS.values()}
        self.assertNotIn('sing-box.service', units)
        self.assertNotIn('prometheus.service', units)
        self.assertNotIn('grafana.service', units)

    def test_unmanaged_service_and_absent_component_guards(self):
        with patch.object(monitorctl, 'run', return_value='not-found'):
            self.assertFalse(monitorctl.installed('probes'))
        with patch.object(monitorctl, 'run', side_effect=['loaded', '/unrelated/application']):
            with self.assertRaises(RuntimeError):
                monitorctl.installed('probes')
        with patch.object(monitorctl, 'run', side_effect=['loaded', '/opt/sing-box-probe/proxy_probe.py']):
            self.assertTrue(monitorctl.installed('probes'))

    def test_relative_route_entry_requires_owned_working_directory(self):
        command = '/usr/bin/python3 collector.py --config /etc/route-monitor/config.json'
        with patch.object(monitorctl, 'run', side_effect=['loaded', command, '/opt/route-monitor']):
            self.assertTrue(monitorctl.installed('routes'))
        with patch.object(monitorctl, 'run', side_effect=['loaded', command, '/unrelated']):
            with self.assertRaises(RuntimeError):
                monitorctl.installed('routes')

    def test_owned_go_exporter_supported_and_python_upgrade_guarded(self):
        command = '/opt/sing-box-exporter/sing-box-exporter-go --config /etc/sing-box-exporter/config.json'
        with patch.object(monitorctl, 'run', side_effect=['loaded', command]):
            self.assertTrue(monitorctl.installed('exporter'))
        with patch.object(monitorctl, 'installed', return_value=True), patch.object(monitorctl, 'run', return_value=command):
            with self.assertRaisesRegex(ValueError, 'native promote'):
                monitorctl.upgrade(pathlib.Path('.'), ['exporter'], dry_run=True)

    def test_route_upgrade_dry_run_uses_separate_source_and_never_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, routes, app = root / 'sing-box', root / 'route-monitoring', root / 'app'
            for path in (source, routes, app):
                path.mkdir()
            (routes / 'production.py').write_text('')
            for filename in ('collector.py', 'common.py'):
                (routes / filename).write_text('new = True\n')
                (app / filename).write_text('old = True\n')
            with patch.dict(monitorctl.COMPONENTS, routes=('route-monitor.service', str(app), ('collector.py', 'common.py'))), \
                 patch.object(monitorctl, 'installed', return_value=True), \
                 patch.object(monitorctl, 'run') as run:
                monitorctl.upgrade(source, ['routes'], dry_run=True)
            run.assert_not_called()
            self.assertEqual((app / 'collector.py').read_text(), 'old = True\n')

    def test_rollback_rejects_unmanaged_backup_path(self):
        with self.assertRaises(ValueError):
            monitorctl.rollback('exporter', pathlib.Path('/tmp/not-a-backup'))

    def test_route_manifest_forwards_to_its_own_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = root / 'deployment.json'
            manifest.write_text(json.dumps({'components': {'routes': {'arguments': ['--host', 'test']}}}))
            with patch.object(monitorctl, 'BUNDLE', root), patch.object(monitorctl, 'bootstrap'), \
                 patch.object(monitorctl, 'run', return_value='ok') as run:
                monitorctl.deploy(manifest)
            run.assert_called_once_with('/usr/bin/python3', str(root / 'route-monitoring' / 'production.py'),
                                        'install-collector', '--host', 'test')

    @unittest.skipUnless(hasattr(os, 'chown'), 'POSIX ownership required')
    def test_failed_route_rollback_recovers_current_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            backup = root / 'backups' / '1'
            saved = backup / 'routes'
            app = root / 'app'
            saved.mkdir(parents=True)
            app.mkdir()
            (saved / 'collector.py').write_text('old = True\n')
            (app / 'collector.py').write_text('current = True\n')
            with patch.object(monitorctl, 'BACKUPS', root / 'backups'), \
                 patch.dict(monitorctl.COMPONENTS, routes=('route-monitor.service', str(app), ('collector.py',))), \
                 patch.object(monitorctl, 'installed', return_value=True), patch.object(monitorctl, 'run') as run, \
                 patch.object(monitorctl, 'wait_healthy', side_effect=RuntimeError('health failed')):
                with self.assertRaises(RuntimeError):
                    monitorctl.rollback('routes', backup)
            self.assertEqual((app / 'collector.py').read_text(), 'current = True\n')
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(call.args == ('systemctl', 'restart', 'route-monitor.service') for call in run.call_args_list))

    @unittest.skipUnless(hasattr(os, 'chown'), 'POSIX ownership required')
    def test_route_unit_is_restored_and_recovered_together_with_code(self):
        for fail_health in (False, True):
            with self.subTest(fail_health=fail_health), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                backup, app, units = root / 'backups/1/routes', root / 'app', root / 'units'
                for path in (backup, app, units):
                    path.mkdir(parents=True)
                (backup / 'collector.py').write_text('old = True\n')
                (app / 'collector.py').write_text('current = True\n')
                unit = 'route-monitor.service'
                (backup / unit).write_text('[Service]\nExecStart=/bin/true\n')
                (units / unit).write_text('[Service]\nExecStart=/bin/false\n')
                with patch.object(monitorctl, 'BACKUPS', root / 'backups'), \
                        patch.object(monitorctl, 'UNIT_DIR', units), \
                        patch.dict(monitorctl.COMPONENTS, routes=(unit, str(app), ('collector.py',))), \
                        patch.object(monitorctl, 'installed', return_value=True), \
                        patch.object(monitorctl, 'run') as run, \
                        patch.object(monitorctl, 'wait_healthy', side_effect=RuntimeError('health failed') if fail_health else None), \
                        redirect_stdout(io.StringIO()) as output:
                    if fail_health:
                        with self.assertRaisesRegex(RuntimeError, 'health failed'):
                            monitorctl.rollback('routes', backup.parent)
                    else:
                        monitorctl.rollback('routes', backup.parent)
                        self.assertTrue(json.loads(output.getvalue())['unit_restored'])
                self.assertIn('current' if fail_health else 'old', (app / 'collector.py').read_text())
                self.assertIn('/bin/false' if fail_health else '/bin/true', (units / unit).read_text())
                reloads = [c for c in run.call_args_list if c.args == ('systemctl', 'daemon-reload')]
                self.assertEqual(len(reloads), 2 if fail_health else 1)
