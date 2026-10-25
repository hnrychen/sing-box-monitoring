import copy
import unittest
from session_guard import eligible, identity, reconcile
import tcpdiag


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(family=2, state=5, inode=42, socket_cookie=(12, 34),
                        payload_idle_seconds=90000, send_queue_bytes=0, receive_queue_bytes=0)

    def test_only_long_observed_empty_idle_owned_half_closed_sockets(self):
        self.assertTrue(eligible(self.row, {42}, 0, 3601))
        for field, value in [('state', 1), ('inode', 43), ('payload_idle_seconds', 12),
                             ('send_queue_bytes', 1), ('receive_queue_bytes', 1),
                             ('socket_cookie', (0xffffffff, 0xffffffff))]:
            row = dict(self.row, **{field: value})
            self.assertFalse(eligible(row, {42}, 0, 3601))
        for field in ('payload_idle_seconds', 'send_queue_bytes', 'receive_queue_bytes'):
            row = copy.copy(self.row)
            del row[field]
            self.assertFalse(eligible(row, {42}, 0, 3601))
        self.assertFalse(eligible(self.row, {42}, 3599, 3601))

    def test_bounded_state_no_history_and_pid_or_cookie_reuse_resets_clock(self):
        owners = {'sing-box': (123, {42})}
        first = reconcile([self.row], owners, {}, 100)
        self.assertEqual(reconcile([self.row], owners, first, 200), first)
        self.assertEqual(reconcile([], owners, first, 200), {})
        self.assertEqual(reconcile([self.row], {'sing-box': (456, {42})}, first, 200)[identity(self.row)]['first_seen'], 200)
        changed = dict(self.row, socket_cookie=(12, 35))
        self.assertEqual(reconcile([changed], owners, first, 200)[identity(changed)]['first_seen'], 200)

    def test_destroy_rejects_established_or_missing_identity_before_kernel_call(self):
        for row in ({}, dict(self.row, state=1), self.row):
            with self.assertRaises(ValueError):
                tcpdiag.destroy(row)


if __name__ == '__main__':
    unittest.main()
