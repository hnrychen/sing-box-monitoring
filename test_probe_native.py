"""Protocol and deadline tests with a finite local SOCKS endpoint; no external probes."""
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch

import proxy_probe


class PassthroughTLS:
    def wrap_socket(self, sock, server_hostname):
        assert server_hostname == 'trace.example'
        return sock


class NativeProbeTests(unittest.TestCase):
    def execute(self, reply, trace=True, drip=False, context=None, connect_delay=0,
                expect_success=False):
        """Run one probe against a local SOCKS endpoint.

        Retry only an observed client-side reset, independently of the expected
        assertion. A parsed failure, timeout, TLS error or malformed response is
        returned immediately. Exhausted transport resets fail the fixture rather
        than accidentally satisfying a negative probe assertion.
        """
        for attempt in range(4):
            result, captured, reset = self._exchange(
                reply, trace, drip, context, connect_delay)
            if not reset:
                return result, captured
            if attempt == 3:
                self.fail('loopback repeatedly reset before probe result could be evaluated')
            time.sleep(0.05)

    def _exchange(self, reply, trace, drip, context, connect_delay):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(2)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        captured = []
        server_errors = []

        def server():
            try:
                with listener.accept()[0] as sock:
                    sock.settimeout(2)
                    captured.append(proxy_probe.recv_exact(sock, 3, time.monotonic() + 2))
                    sock.sendall(b'\x05\x00')
                    header = proxy_probe.recv_exact(sock, 5, time.monotonic() + 2)
                    self.assertEqual(header[:4], b'\x05\x01\x00\x03')
                    captured.append(proxy_probe.recv_exact(sock, header[4] + 2, time.monotonic() + 2))
                    time.sleep(connect_delay)
                    sock.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb')
                    request = bytearray()
                    while not request.endswith(b'\r\n\r\n'):
                        chunk = sock.recv(4096)
                        if not chunk:
                            return
                        request.extend(chunk)
                    captured.append(bytes(request))
                    if drip:
                        for chunk in reply:
                            sock.sendall(bytes([chunk]))
                            time.sleep(0.03)
                    else:
                        sock.sendall(reply)
                    sock.shutdown(socket.SHUT_WR)
                    while sock.recv(4096):
                        pass
            except AssertionError as exc:
                server_errors.append(exc)
            except (OSError, TimeoutError):
                pass

        worker = threading.Thread(target=server)
        worker.start()
        # Every exchange owns its socket and ephemeral listener. The 32 KiB
        # body limit and the independent 128 KiB wire limit are separate bounds.
        config = dict(client_port_base=port, timeout=1.0, connect_timeout=0.75)
        check = dict(url='https://trace.example/cdn-cgi/trace', status=200, trace_ip=trace)
        resets = []
        connect = socket.create_connection

        class ObservedSocket:
            def __init__(self, sock):
                self.sock = sock

            def __getattr__(self, name):
                value = getattr(self.sock, name)
                if name not in ('recv', 'recv_into', 'sendall'):
                    return value
                def observed(*args, **kwargs):
                    try:
                        return value(*args, **kwargs)
                    except (ConnectionResetError, ConnectionAbortedError) as exc:
                        resets.append(type(exc).__name__)
                        raise
                return observed

        def observed_connect(*args, **kwargs):
            try:
                return ObservedSocket(connect(*args, **kwargs))
            except (ConnectionResetError, ConnectionAbortedError) as exc:
                resets.append(type(exc).__name__)
                raise

        with patch.object(proxy_probe.socket, 'create_connection', side_effect=observed_connect):
            result = proxy_probe.run_native_check(config, 0, check, context or PassthroughTLS())
        worker.join(timeout=2)
        if server_errors:
            raise server_errors[0]
        return result, captured, bool(resets)

    def test_semantic_probe_failure_is_never_retried(self):
        failure = dict(success=0, status=200, error=1)
        with patch.object(self, '_exchange', return_value=(failure, [], False)) as exchange:
            result, _ = self.execute(b'', expect_success=True)
        self.assertEqual(result, failure)
        exchange.assert_called_once()

    def test_transport_exhaustion_cannot_pass_a_negative_assertion(self):
        failure = dict(success=0, status=0, error=1)
        with patch.object(self, '_exchange', return_value=(failure, [], True)) as exchange, \
                patch('time.sleep'):
            with self.assertRaisesRegex(AssertionError, 'loopback repeatedly reset'):
                self.execute(b'', expect_success=False)
        self.assertEqual(exchange.call_count, 4)

    def test_wire_budget_never_reads_a_byte_past_its_limit(self):
        class WireSocket:
            def settimeout(self, timeout):
                pass
            def recv_into(self, buffer):
                buffer[:] = b'x' * len(buffer)
                return len(buffer)
        reader = proxy_probe.DeadlineReader(WireSocket(), time.monotonic() + 5)
        reader.received = reader.WIRE_BUDGET - 1
        self.assertEqual(reader.readinto(bytearray(4096)), 1)
        self.assertEqual(reader.received, reader.WIRE_BUDGET)
        with self.assertRaisesRegex(ValueError, 'wire budget'):
            reader.readinto(bytearray(4096))

    def test_remote_dns_full_trace_and_fresh_request(self):
        body = b'ip=203.0.113.10\n'
        reply = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
        result, sent = self.execute(reply, expect_success=True)
        self.assertEqual(result['success'], 1)
        self.assertGreater(result['duration'], 0)
        self.assertEqual(sent[0], b'\x05\x01\x00')
        self.assertEqual(sent[1], b'trace.example\x01\xbb')
        self.assertIn(b'Connection: close', sent[2])

    def test_challenge_redirect_oversize_and_truncation_fail(self):
        for response in (
            b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nchallenge',
            b'HTTP/1.1 302 Found\r\nLocation: https://other.example/\r\nContent-Length: 0\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Length: 32769\r\n\r\n' + b'x' * 32769,
            b'HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\nip=203.0.113.10\n',
        ):
            with self.subTest(response=response[:60]):
                result, _ = self.execute(response)
                self.assertEqual(result['success'], 0)

    def test_chunked_trace(self):
        body = b'ip=203.0.113.10\n'
        result, _ = self.execute(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' +
                                 hex(len(body))[2:].encode() + b'\r\n' + body + b'\r\n0\r\n\r\n',
                                 expect_success=True)
        self.assertEqual(result['success'], 1)

    def test_slow_drip_is_bounded_by_total_deadline(self):
        result, _ = self.execute(b'HTTP/1.1 200 OK\r\n\r\nip=203.0.113.10\n', drip=True)
        self.assertEqual(result['success'], 0)
        self.assertLess(result['attempt_duration'], 1.5)

    def test_connect_budget_and_tls_verification_failure(self):
        result, _ = self.execute(b'', connect_delay=0.9)
        self.assertEqual(result['success'], 0)
        self.assertLess(result['attempt_duration'], 0.9)
        class RejectTLS:
            def wrap_socket(self, *args, **kwargs):
                raise ssl.SSLCertVerificationError('untrusted')
        result, _ = self.execute(b'', context=RejectTLS())
        self.assertEqual(result['success'], 0)


if __name__ == '__main__':
    unittest.main()
