import pathlib
import os
import json
import tempfile
import unittest
from unittest.mock import patch

import native_exporter as native


class NativeDeploymentTests(unittest.TestCase):
    def test_binary_architecture_matches_host_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = pathlib.Path(directory) / 'candidate'
            for machine, host in ((62, 'x86_64'), (183, 'aarch64')):
                header = bytearray(20)
                header[:6] = b'\x7fELF\x02\x01'
                header[18:20] = machine.to_bytes(2, 'little')
                candidate.write_bytes(header)
                with self.subTest(machine=machine), patch.object(native, 'run', side_effect=[host, '1.5.0-go']):
                    self.assertEqual(native.validate_binary(candidate), (candidate.resolve(), '1.5.0-go'))
                other = 'aarch64' if host == 'x86_64' else 'x86_64'
                with self.subTest(mismatch=machine), patch.object(native, 'run', return_value=other) as run:
                    with self.assertRaisesRegex(ValueError, 'architecture differs'):
                        native.validate_binary(candidate)
                    run.assert_called_once_with('uname', '-m')

    def test_invalid_binary_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = pathlib.Path(directory) / 'candidate'
            header = bytearray(20)
            header[:6] = b'\x7fELF\x02\x01'
            header[18:20] = (243).to_bytes(2, 'little')
            big_endian = bytearray(header)
            big_endian[5] = 2
            for invalid in (b'', b'\x7fELF\x02\x01', b'not an ELF executable', header, big_endian):
                candidate.write_bytes(invalid)
                with self.subTest(header=invalid), patch.object(native, 'run') as run:
                    with self.assertRaises(ValueError):
                        native.validate_binary(candidate)
                    run.assert_not_called()

    @unittest.skipUnless(hasattr(os, 'chown'), 'POSIX ownership required')
    def test_failed_promotion_restores_previous_binary_and_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            binary, dropin, candidate = root / 'app' / 'binary', root / 'unit' / 'implementation.conf', root / 'candidate'
            binary.parent.mkdir();dropin.parent.mkdir()
            binary.write_bytes(b'old binary');binary.chmod(0o755)
            candidate.write_bytes(b'new binary')
            with patch.object(native, 'BINARY', binary), patch.object(native, 'DROPIN', dropin), \
                 patch.object(native, 'BACKUPS', root / 'backups'), patch.object(native, 'production'), \
                 patch.object(native, 'validate_binary', return_value=(candidate, '1.5.0-go')), \
                 patch.object(native, 'run') as run, patch.object(native, 'health', side_effect=[RuntimeError('failed'), None]):
                with self.assertRaises(RuntimeError):
                    native.promote(candidate)
            self.assertEqual(binary.read_bytes(), b'old binary')
            self.assertFalse(dropin.exists())
            self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
            self.assertEqual(sum(c.args == ('systemctl', 'restart', native.UNIT) for c in run.call_args_list), 2)

    def test_restore_rejects_arbitrary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                native.restore(pathlib.Path(directory))

    def test_go_only_rollback_rejects_python_backup_without_touching_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            saved = root / 'backups' / 'native-exporter-1'
            saved.mkdir(parents=True)
            binary = root / 'sing-box-exporter-go'
            dropin = root / 'implementation.conf'
            binary.write_bytes(b'current Go binary')
            entries = [dict(target=str(binary), exists=False),
                       dict(target=str(dropin), exists=False)]
            (saved / 'manifest.json').write_text(json.dumps(entries))
            with patch.object(native, 'BACKUPS', root / 'backups'), \
                 patch.object(native, 'BINARY', binary), patch.object(native, 'DROPIN', dropin), \
                 patch.object(native, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'previous Go binary'):
                    native.restore(saved, go_only=True)
                run.assert_not_called()
            self.assertEqual(binary.read_bytes(), b'current Go binary')

    @unittest.skipUnless(os.name == 'posix', 'POSIX deployment symlink check')
    def test_atomic_copy_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / 'target';target.write_text('preserve')
            link = root / 'link';link.symlink_to(target)
            with self.assertRaises(ValueError):
                native.atomic_bytes(link, b'replacement')
            self.assertEqual(target.read_text(), 'preserve')
