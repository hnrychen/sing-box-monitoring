import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import repair_tcp_uids as repair


class SocketIdentityTests(unittest.TestCase):
    def test_uses_filesystem_uid_even_when_proc_owner_is_root(self):
        with patch.object(repair.subprocess, 'check_output', return_value='42\n'), \
             patch.object(pathlib.Path, 'read_text', return_value='Uid:\t0\t988\t988\t994\n'):
            self.assertEqual(repair.service_socket_uid('sing-box-cn'), 994)

    def test_retries_changed_process_and_rejects_stopped_service(self):
        with patch.object(repair.subprocess, 'check_output', side_effect=['42', '43', '43', '43']), \
             patch.object(pathlib.Path, 'read_text', side_effect=['Uid:\t988\t988\t988\t988', 'Uid:\t994\t994\t994\t994']):
            self.assertEqual(repair.service_socket_uid('sing-box-cn'), 994)
        with patch.object(repair.subprocess, 'check_output', return_value='0'):
            with self.assertRaises(RuntimeError):
                repair.service_socket_uid('sing-box-cn')

    def test_missing_uid_is_rejected(self):
        with patch.object(repair.subprocess, 'check_output', return_value='42'), \
             patch.object(pathlib.Path, 'read_text', return_value='Name: sing-box'):
            with self.assertRaises(RuntimeError):
                repair.service_socket_uid('sing-box-cn')

    def test_dry_run_preserves_all_config_bytes_and_does_not_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'config.json'
            original = b'{"tcp_rtt":true,"scrape_token":"preserve","services":[{"name":"sing-box-cn","tcp_uid":0,"inbounds":{}}]}'
            path.write_bytes(original)
            with patch.object(repair, 'service_socket_uid', return_value=988), \
                 patch.object(repair.subprocess, 'run') as restart:
                result = repair.repair(path)
            self.assertEqual(result['changes'], [dict(service='sing-box-cn', before=0, after=988)])
            self.assertEqual(path.read_bytes(), original)
            restart.assert_not_called()

    def test_already_correct_config_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'config.json'
            path.write_text(json.dumps(dict(tcp_rtt=True, services=[dict(name='sing-box', tcp_uid=988)])))
            with patch.object(repair, 'service_socket_uid', return_value=988), \
                 patch.object(repair.subprocess, 'run') as restart:
                self.assertEqual(repair.repair(path, True)['changes'], [])
            restart.assert_not_called()

    @unittest.skipUnless(hasattr(os, 'chown'), 'POSIX ownership required')
    def test_apply_changes_only_uid_and_restores_bytes_on_restart_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / 'config.json'
            original = dict(tcp_rtt=True, scrape_token='preserve', poll_interval=2,
                            services=[dict(name='sing-box-cn', tcp_uid=0, secret='preserve-api')])
            path.write_text(json.dumps(original))
            path.chmod(0o640)
            with patch.object(repair, 'BACKUP_ROOT', root / 'backups'), \
                 patch.object(repair, 'service_socket_uid', return_value=988), \
                 patch.object(repair.subprocess, 'run') as restart:
                result = repair.repair(path, True)
            expected = dict(original, services=[dict(original['services'][0], tcp_uid=988)])
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(restart.call_args_list[0].args[0], ['systemctl', 'restart', 'sing-box-exporter.service'])
            saved = pathlib.Path(result['backup']) / 'exporter' / 'config.json'
            self.assertEqual(json.loads(saved.read_text()), original)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            path.write_text(json.dumps(original))
            before = path.read_bytes()
            with patch.object(repair, 'BACKUP_ROOT', root / 'backups'), \
                 patch.object(repair, 'service_socket_uid', return_value=988), \
                 patch.object(repair.subprocess, 'run', side_effect=[RuntimeError('failed restart'), None]):
                with self.assertRaises(RuntimeError):
                    repair.repair(path, True)
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
