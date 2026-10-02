#!/usr/bin/env python3
"""Authenticated proxy RTT check using a temporary sing-box client and echo peer.

Supply credential-bearing protocol_smoke.py --export output ONLY through stdin.
The target echo server must already exist on the proxy's loopback interface.
No host routes, persistent configuration, or throughput benchmark is involved.
"""
import argparse
import json
import os
import pathlib
import selectors
import socket
import struct
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', default='sing-box')
    parser.add_argument('--echo-port', type=int, default=19543)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--serve-echo', action='store_true', help='Temporary loopback-only echo peer; no sing-box required')
    args = parser.parse_args()
    if args.serve_echo:
        with socket.socket() as listener, selectors.DefaultSelector() as selector:
            listener.bind(('127.0.0.1', args.echo_port))
            listener.listen()
            listener.setblocking(False)
            selector.register(listener, selectors.EVENT_READ)
            print('loopback echo ready', flush=True)
            deadline = time.monotonic() + args.seconds
            try:
                while time.monotonic() < deadline:
                    for key, _ in selector.select(timeout=1):
                        if key.fileobj is listener:
                            peer, _ = listener.accept()
                            peer.settimeout(1)
                            peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                            selector.register(peer, selectors.EVENT_READ)
                        else:
                            payload = key.fileobj.recv(64)
                            if payload:
                                key.fileobj.sendall(payload)
                            else:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
            finally:
                for key in list(selector.get_map().values()):
                    if key.fileobj is not listener:
                        key.fileobj.close()
        return
    settings = [c for c in json.load(sys.stdin) if c['protocol'] in ('shadowsocks', 'vless')]
    config = dict(log=dict(level='error'), inbounds=[], outbounds=[], route=dict(rules=[]))
    for i, client in enumerate(settings):
        tag = f'probe-{i}'
        config['inbounds'].append(dict(type='mixed', tag=tag, listen='127.0.0.1', listen_port=17950 + i))
        config['outbounds'].append(dict(client['outbound'], tag=tag))
        config['route']['rules'].append(dict(inbound=[tag], action='route', outbound=tag))
    with tempfile.TemporaryDirectory(prefix='proxy-rtt-') as directory:
        os.chmod(directory, 0o700)
        path = pathlib.Path(directory) / 'client.json'
        path.write_text(json.dumps(config))
        os.chmod(path, 0o600)
        process = subprocess.Popen([args.binary, 'run', '-c', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sockets = []
        try:
            time.sleep(1)
            if process.poll() is not None:
                raise RuntimeError('temporary proxy client failed')
            for i, client in enumerate(settings):
                sock = socket.create_connection(('127.0.0.1', 17950 + i), timeout=5)
                sock.sendall(b'\x05\x01\x00')
                if sock.recv(2) != b'\x05\x00':
                    raise RuntimeError('SOCKS negotiation failed')
                sock.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + struct.pack('!H', args.echo_port))
                reply = sock.recv(1024)
                if len(reply) < 2 or reply[1] != 0:
                    raise RuntimeError('SOCKS connect failed')
                sockets.append((client['protocol'], sock))
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                for protocol, sock in sockets:
                    started = time.perf_counter()
                    sock.sendall(b'RTT!')
                    reply = b''
                    while len(reply) < 4:
                        block = sock.recv(4 - len(reply))
                        if not block:
                            raise RuntimeError('echo connection closed')
                        reply += block
                    if reply != b'RTT!':
                        raise RuntimeError('echo mismatch')
                    print(json.dumps(dict(protocol=protocol, echo_ms=(time.perf_counter() - started) * 1000)), flush=True)
                time.sleep(2)
        finally:
            for _, sock in sockets:
                sock.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
