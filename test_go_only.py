"""Production entry points cannot silently deploy the retired Python collector."""
import pathlib
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
import monitorctl
import install


class GoOnlyTests(unittest.TestCase):
    def test_python_runtime_entry_is_retired(self):
        p=subprocess.run([sys.executable,str(pathlib.Path(__file__).with_name('exporter.py'))],capture_output=True,text=True)
        self.assertNotEqual(p.returncode,0)
        self.assertIn('Python exporter retired',p.stderr)

    def test_helper_bundle_does_not_ship_or_upgrade_python_exporter(self):
        self.assertNotIn('exporter.py',monitorctl.BUNDLE_FILES)
        self.assertEqual(monitorctl.COMPONENTS['exporter'][2],('sing-box-exporter-go',))
        with patch.object(monitorctl,'installed',return_value=True),patch.object(monitorctl,'run') as run:
            with self.assertRaisesRegex(ValueError,'native promote'):
                monitorctl.upgrade(pathlib.Path('.'),['exporter'])
            run.assert_not_called()

    def test_non_native_rollback_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'native rollback'):
            monitorctl.rollback('exporter',pathlib.Path('/tmp/old-python-files'))

    @unittest.skipUnless(os.name == 'posix', 'Linux installer preflight')
    def test_bad_binary_stops_install_before_account_or_config_changes(self):
        args = ['install.py', '--binary', '/tmp/wrong-architecture',
                '--monitor-ip', '192.0.2.20', '--certificate-ip', '192.0.2.10']
        with patch.object(sys, 'argv', args), patch.object(os, 'geteuid', return_value=0), \
             patch.object(install, 'validate_binary', side_effect=ValueError('bad candidate')), \
             patch.object(install, 'run') as run, patch.object(install, 'write') as write:
            with self.assertRaisesRegex(ValueError, 'bad candidate'):
                install.main()
            run.assert_not_called()
            write.assert_not_called()


if __name__=='__main__':unittest.main()
