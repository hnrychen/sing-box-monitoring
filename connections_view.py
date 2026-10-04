#!/usr/bin/env python3
"""Small central JSON view of cached exporter snapshots; no history or metric labels."""
import argparse
import concurrent.futures
import hmac
import ipaddress
import json
import logging
import pathlib
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_RESPONSE = 8 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def selected(value, choices):
    return not choices or '.*' in choices or '$__all' in choices or value in choices


def country_metadata(raw):
    """Read existing local IPinfo metrics, without querying IPinfo or Prometheus."""
    countries = {}
    for line in raw.decode().splitlines():
        if not line.startswith('source_ipinfo_info{') or not line.endswith(' 1'):
            continue
        tags = {key: json.loads('"' + value + '"') for key, value in
                re.findall(r'(\w+)="((?:\\.|[^"\\])*)"', line)}
        code, address = tags.get('country_code', ''), tags.get('source_ip', '')
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_global and not ip.is_multicast and re.fullmatch('[A-Z]{2}', code):
            countries[str(ip)] = code
    return countries


def source_label(address, countries):
    try:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        code = countries.get(str(ip), '')
    except ValueError:
        code = ''
    flag = ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code) if re.fullmatch('[A-Z]{2}', code) else ''
    return (flag + ' ' if flag else '') + address


def group_rows(snapshots, params, now=None):
    now = time.time() if now is None else now
    groups = {}
    for host, snapshot in snapshots.items():
        if not selected(host, params.get('var-host')):
            continue
        for service in snapshot['services']:
            if selected(service['service'], params.get('var-service')):
                if not service['healthy'] or now - service['timestamp'] > service.get('max_age_seconds', 15):
                    raise ValueError('Unavailable or stale collector: ' + host + '/' + service['service'])
        for row in snapshot['rows']:
            if not selected(row['service'], params.get('var-service')) or not selected(row['protocol'], params.get('var-protocol')):
                continue
            # No reverse DNS guesses: a CDN IP does not uniquely identify a website.
            address = row['destination_ip'] or row['domain'] or 'unknown'
            if ':' in address:
                address = '[' + address + ']'
            destination = address + (':' + str(row['destination_port']) if row['destination_port'] else '')
            key = (host, row['service'], row['source_ip'], row['protocol'], row['network'], row['domain'], destination, row['outbound'])
            group = groups.setdefault(key, dict(zip(('host', 'service', 'source_ip', 'protocol', 'network', 'domain', 'destination', 'outbound'), key),
                                                active=0, upload_bytes=0, download_bytes=0, age_seconds=0))
            group['active'] += 1
            group['upload_bytes'] += row['upload_bytes']
            group['download_bytes'] += row['download_bytes']
            group['age_seconds'] = max(group['age_seconds'], row['age_seconds'])
    return sorted(groups.values(), key=lambda row: (-row['download_bytes'], row['host'], row['source_ip'], row['destination']))


class View:
    def __init__(self, config):
        self.config = config
        sources = config['sources']
        if not 1 <= len(sources) <= 16 or len({s['host'] for s in sources}) != len(sources):
            raise ValueError('expected 1..16 unique exporter hosts')
        self.clients = {}
        self.ipinfo_url = config.get('ipinfo_metrics_url')
        if self.ipinfo_url:
            url = urllib.parse.urlsplit(self.ipinfo_url)
            if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                    or url.username or url.query or url.fragment or url.path != '/metrics'):
                raise ValueError('IPinfo metrics must be a local HTTP /metrics endpoint')
        self.local_client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.countries = {}
        for source in sources:
            url = urllib.parse.urlsplit(source['url'])
            if url.scheme != 'https' or url.username or url.query or url.fragment or url.path not in ('', '/'):
                raise ValueError('exporter URL must be an HTTPS origin')
            context = ssl.create_default_context(cafile=source['ca_file'])
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context))
            token = pathlib.Path(source['token_file']).read_text().strip()
            self.clients[source['host']] = (source['url'].rstrip('/') + '/connections', opener, token)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(sources) + bool(self.ipinfo_url))
        self.lock = threading.Lock()
        self.snapshots = {}
        self.failures = set()
        self.last_fetch = 0

    def fetch(self, host):
        url, opener, token = self.clients[host]
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
        with opener.open(request, timeout=4) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError('exporter response too large')
        data = json.loads(raw)
        if not isinstance(data['services'], list) or not isinstance(data['rows'], list) or len(data['rows']) > 65536:
            raise ValueError('invalid connection snapshot')
        return data

    def rows(self, params):
        with self.lock:
            if time.monotonic() - self.last_fetch >= 2:
                country_future = self.pool.submit(self.fetch_countries) if getattr(self, 'ipinfo_url', None) else None
                futures = {host: self.pool.submit(self.fetch, host) for host in self.clients}
                snapshots, failures = {}, set()
                for host, future in futures.items():
                    try:
                        snapshots[host] = future.result()
                    except Exception as exc:
                        failures.add(host)
                        logging.warning('%s connection view unavailable (%s)', host, type(exc).__name__)
                self.snapshots, self.failures = snapshots, failures
                self.countries = country_future.result() if country_future else {}
                self.last_fetch = time.monotonic()
            failures = sorted(host for host in self.failures if selected(host, params.get('var-host')))
            if failures:
                raise ValueError('Unavailable collector: ' + ', '.join(failures))
            rows = group_rows(self.snapshots, params)
            for row in rows:
                row['source_ip_display'] = source_label(row['source_ip'], self.countries)
            return rows

    def fetch_countries(self):
        try:
            with self.local_client.open(self.ipinfo_url, timeout=1) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('IPinfo metadata response too large')
            return country_metadata(raw)
        except Exception as exc:
            logging.warning('Country flags unavailable (%s)', type(exc).__name__)
            return {}  # Optional enrichment must never hide active connections.


def serve(config):
    view = View(config)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + config['token']).encode()):
                self.reply(401, {'error': 'Unauthorized'})
                return
            url = urllib.parse.urlsplit(self.path)
            if url.path not in ('/connections', '/healthz'):
                self.reply(404, {'error': 'Not found'})
                return
            try:
                rows = view.rows(urllib.parse.parse_qs(url.query))
                self.reply(200, {'rows': rows} if url.path == '/connections' else {'healthy': True})
            except ValueError as exc:
                self.reply(503, {'error': str(exc)})
            except Exception as exc:
                logging.warning('Connection view failed (%s)', type(exc).__name__)
                self.reply(503, {'error': 'Connection view unavailable'})

        def reply(self, status, value):
            body = json.dumps(value, separators=(',', ':')).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def log_message(self, *_):
            pass

    address, port = config.get('listen', '127.0.0.1:9121').rsplit(':', 1)
    server = HTTPServer((address, int(port)), Handler)
    original_get_request = server.get_request

    def get_request():
        sock, address = original_get_request()
        sock.settimeout(6)
        return sock, address

    server.get_request = get_request
    server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/sing-box-connections/config.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    serve(json.loads(pathlib.Path(args.config).read_text()))
