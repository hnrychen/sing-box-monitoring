#!/usr/bin/env python3
"""Small real TCP/UDP protocol test; no host routes or permanent client config."""
import argparse
import base64
import json
import os
import pathlib
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time


def client_outbounds(server):
    results = []
    for directory in ('/etc/sing-box', '/etc/sing-box-cn'):
        path = pathlib.Path(directory) / 'config.json'
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for inbound in data.get('inbounds', []):
            kind = inbound['type']
            out = dict(type=kind, server=server, server_port=inbound['listen_port'])
            if kind == 'shadowsocks':
                out.update(method=inbound['method'], password=inbound['password'])
            elif kind == 'vless':
                user = inbound['users'][0]
                tls = inbound['tls']
                reality = tls['reality']
                private = base64.urlsafe_b64decode(reality['private_key'] + '===')
                der = bytes.fromhex('302e020100300506032b656e04220420') + private
                public_der = subprocess.run(['openssl', 'pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'],
                                            input=der, capture_output=True, check=True).stdout
                key = base64.urlsafe_b64encode(public_der[-32:]).decode().rstrip('=')
                out.update(uuid=user['uuid'], flow=user.get('flow', ''), tls=dict(enabled=True,
                    server_name=tls['server_name'], utls=dict(enabled=True, fingerprint='chrome'),
                    reality=dict(enabled=True, public_key=key, short_id=reality['short_id'][0])))
            elif kind == 'hysteria2':
                tls = inbound['tls']
                certificate = tls.get('certificate_path')
                embedded = tls.get('certificate', '')
                if isinstance(embedded, list):
                    embedded = '\n'.join(embedded)
                provider = next((p for p in data.get('certificate_providers', []) if p.get('tag') == tls.get('certificate_provider')), None)
                if provider:
                    domains = provider['domain']
                    if isinstance(domains, str):
                        domains = [domains]
                else:
                    command = ['openssl', 'x509', '-noout', '-ext', 'subjectAltName']
                    if certificate:
                        command.extend(['-in', certificate])
                    subject = subprocess.run(command, input=embedded if not certificate else None,
                                             capture_output=True, text=True, check=True).stdout
                    domains = re.findall(r'(?:DNS:|IP Address:)([^,\s]+)', subject)
                out.update(password=inbound['users'][0]['password'], tls=dict(enabled=True, server_name=domains[0], alpn=['h3']))
                if certificate:
                    out['tls']['certificate'] = pathlib.Path(certificate).read_text().splitlines()
                elif embedded:
                    out['tls']['certificate'] = embedded.splitlines()
                if inbound.get('obfs'):
                    out['obfs'] = inbound['obfs']
            else:
                continue
            results.append(dict(protocol=kind, outbound=out))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--export', help='Produce test client settings on the target; contains credentials, use only a pipe')
    parser.add_argument('--hold', type=int, default=22)
    args = parser.parse_args()
    if args.export:
        print(json.dumps(client_outbounds(args.export)))
        return
    clients = json.load(sys.stdin)
    inbounds, outbounds, rules = [], [], []
    for i, client in enumerate(clients):
        tag = f'probe-{i}'
        outbounds.append(dict(client['outbound'], tag=tag))
        inbounds.extend([dict(type='mixed', tag=tag, listen='127.0.0.1', listen_port=17900 + i),
                         dict(type='direct', tag=tag + '-udp', listen='127.0.0.1', listen_port=18000 + i,
                              network='udp', override_address='1.1.1.1', override_port=53)])
        rules.append(dict(inbound=[tag, tag + '-udp'], action='route', outbound=tag))
    config = dict(log=dict(level='error'), inbounds=inbounds, outbounds=outbounds, route=dict(rules=rules))
    with tempfile.TemporaryDirectory(prefix='sing-box-protocol-probe-') as directory:
        path = pathlib.Path(directory) / 'client.json'
        path.write_text(json.dumps(config))
        os.chmod(path, 0o600)
        subprocess.run(['sing-box', 'check', '-c', str(path)], check=True, capture_output=True)
        with (pathlib.Path(directory) / 'client.log').open('w') as logfile:
            process = subprocess.Popen(['sing-box', 'run', '-c', str(path)], stdout=logfile, stderr=subprocess.STDOUT)
            sockets = []
            try:
                time.sleep(1)
                if process.poll() is not None:
                    raise RuntimeError('probe client failed to start')
                for i, client in enumerate(clients):
                    sock = socket.create_connection(('127.0.0.1', 17900 + i), timeout=10)
                    sock.sendall(b'\x05\x01\x00')
                    if sock.recv(2) != b'\x05\x00':
                        raise RuntimeError('SOCKS greeting failed')
                    domain = b'www.cloudflare.com'
                    sock.sendall(b'\x05\x01\x00\x03' + bytes([len(domain)]) + domain + struct.pack('!H', 443))
                    reply = sock.recv(1024)
                    if len(reply) < 2 or reply[1] != 0:
                        raise RuntimeError('SOCKS connect failed')
                    tls = ssl.create_default_context().wrap_socket(sock, server_hostname=domain.decode())
                    tls.sendall(b'GET /cdn-cgi/trace HTTP/1.1\r\nHost: www.cloudflare.com\r\nConnection: keep-alive\r\n\r\n')
                    response = tls.recv(8192)
                    if b'200 OK' not in response.split(b'\r\n')[0]:
                        raise RuntimeError('HTTPS probe failed')
                    sockets.append(tls)
                    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    udp.settimeout(8)
                    # One DNS A lookup, under 100 bytes. ID matched in response.
                    query = b'\x71\x23\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01'
                    udp.sendto(query, ('127.0.0.1', 18000 + i))
                    packet = udp.recv(4096)
                    if packet[:2] != query[:2] or not packet[2] & 0x80:
                        raise RuntimeError('UDP DNS probe failed')
                    sockets.append(udp)
                    print(json.dumps(dict(host=client['host'], protocol=client['protocol'], tcp='PASS', udp='PASS')), flush=True)
                print(f'Holding {len(sockets)} small probe sessions for {args.hold}s for collection', flush=True)
                time.sleep(args.hold)
            finally:
                for sock in sockets:
                    sock.close()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
