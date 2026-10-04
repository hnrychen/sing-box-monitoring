#!/usr/bin/env python3
"""Central, opt-in IPinfo Lite enrichment. No API calls in scrape handlers."""
import argparse
import ipaddress
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

SOURCE_QUERY = 'count by(source_ip)({__name__=~"singbox_source_protocol_connections|singbox_tcp_source_rtt_count"})'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return str(ip) if ip.is_global and not ip.is_multicast else None
    except ValueError:
        return None


def metadata(address, data):
    if not isinstance(data, dict) or data.get('ip') != address:
        raise ValueError('IPinfo response identity mismatch')
    country, asn = data.get('country_code', ''), data.get('asn', '')
    if not isinstance(country, str) or not re.fullmatch(r'[A-Z]{2}|', country):
        raise ValueError('invalid country code')
    if not isinstance(asn, str) or not re.fullmatch(r'AS[0-9]{1,12}|', asn):
        raise ValueError('invalid ASN')
    name = data.get('as_name', '')
    if not isinstance(name, str):
        raise ValueError('invalid organization')
    return dict(country_code=country, asn=asn, as_name=''.join(c for c in name[:120] if c.isprintable()))


def source_display(address, country):
    flag = ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in country) if re.fullmatch('[A-Z]{2}', country) else ''
    return (flag + ' ' if flag else '') + address


class Enrichment:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.cache = {}
        self.current = []
        self.up = 0
        self.last_discovery = 0
        self.requests = self.errors = self.discovery_errors = 0
        self.cooldown = 0
        self.discovery_interval = float(config.get('discovery_interval', 2))
        self.lookup_spacing = float(config.get('lookup_spacing', 0.1))
        self.lookup_batch = int(config.get('lookup_batch', 32))
        if not 1 <= self.discovery_interval <= 3600 or not 0.1 <= self.lookup_spacing <= 60 or not 1 <= self.lookup_batch <= 256:
            raise ValueError('invalid discovery/lookup pacing')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with open(config['token_file'], encoding='utf-8') as stream:
            self.token = stream.read().strip()
        if not self.token or any(c.isspace() for c in self.token):
            raise ValueError('invalid IPinfo token')
        url = urllib.parse.urlsplit(config.get('prometheus_url', 'http://127.0.0.1:9090'))
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.query or url.fragment:
            raise ValueError('Prometheus must be a local HTTP endpoint')
        self.prometheus = urllib.parse.urlunsplit(url).rstrip('/')
        try:
            with open(config['cache_file'], encoding='utf-8') as stream:
                saved = json.load(stream)
            if not isinstance(saved, dict):
                raise ValueError('invalid cache')
            for address, entry in saved.items():
                if public_ip(address) != address or not isinstance(entry, dict):
                    raise ValueError('invalid cache identity')
                checked = float(entry.get('checked', 0))
                if entry.get('data') is not None:
                    entry['data'] = metadata(address, dict(entry['data'], ip=address))
                self.cache[address] = dict(data=entry.get('data'), checked=checked,
                                           retry=float(entry.get('retry', 0)), failures=int(entry.get('failures', 0)))
        except FileNotFoundError:
            pass

    def get_json(self, request, budget):
        with self.opener.open(request, timeout=4) as response:
            raw = response.read(budget + 1)
        if len(raw) > budget:
            raise ValueError('response budget exceeded')
        return json.loads(raw)

    def discover(self):
        data = self.get_json(self.prometheus + '/api/v1/query?' + urllib.parse.urlencode({'query': SOURCE_QUERY}), 1024 * 1024)
        if data.get('status') != 'success' or data['data'].get('resultType') != 'vector':
            raise ValueError('invalid Prometheus result')
        # Respect source privacy; there is no IP-count cutoff.
        ips = sorted({ip for row in data['data']['result'] if (ip := public_ip(row['metric'].get('source_ip', '')))})
        return ips

    def lookup(self, address, now):
        self.requests += 1
        request = urllib.request.Request('https://api.ipinfo.io/lite/' + urllib.parse.quote(address, safe=':'),
                                        headers={'Authorization': 'Bearer ' + self.token, 'User-Agent': 'sing-box-monitoring-ipinfo/1'})
        try:
            record = metadata(address, self.get_json(request, 16384))
            entry = dict(data=record, checked=now, retry=0, failures=0)
        except Exception as exc:
            self.errors += 1
            old = self.cache.get(address, {})
            failures = min(8, old.get('failures', 0) + 1)
            delay = min(21600, 300 * 2 ** (failures - 1))
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 429):
                self.cooldown = now + 3600  # One invalid/rate-limited token must not trigger a request storm.
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            entry = dict(data=old.get('data'), checked=old.get('checked', 0), retry=now + delay, failures=failures)
            logging.warning('IPinfo lookup failed (%s)', type(exc).__name__)
        with self.lock:
            self.cache[address] = entry

    def save(self):
        with self.lock:
            contents = json.dumps(self.cache, ensure_ascii=False)
        path = self.config['cache_file']
        temporary = path + '.tmp'
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as stream:
            stream.write(contents)
        os.replace(temporary, path)

    def cycle(self, now=None):
        now = time.time() if now is None else now
        try:
            current = self.discover()
        except Exception as exc:
            with self.lock:
                self.up = 0
                self.discovery_errors += 1
            logging.warning('Source discovery failed (%s)', type(exc).__name__)
            return
        with self.lock:
            self.current, self.up, self.last_discovery = current, 1, now
            # Expire old inactive metadata by age, never by IP count.
            active = set(current)
            retention = max(3600, self.config.get('cache_ttl', 604800))
            expired = [ip for ip, entry in self.cache.items() if ip not in active and
                       now - max(entry['checked'], entry.get('retry', 0)) > retention]
            for ip in expired:
                del self.cache[ip]
        count = 0
        ttl = max(3600, self.config.get('cache_ttl', 604800))
        # Cold entries precede TTL refreshes, so new clients never wait behind
        # an expired cache backlog. One worker keeps rate/backoff deterministic.
        for address in sorted(current, key=lambda ip: self.cache.get(ip, {}).get('data') is not None):
            if now < self.cooldown or count >= self.lookup_batch:
                break
            old = self.cache.get(address, {})
            if now < old.get('retry', 0) or (old.get('checked', 0) and now - old['checked'] < ttl):
                continue
            self.lookup(address, now)
            count += 1
            time.sleep(self.lookup_spacing)  # Bounded pacing; cached IPs make no external requests.
        if count or expired:
            self.save()

    def render(self, now=None):
        now = time.time() if now is None else now
        lines = []
        def metric(name, value, tags=None):
            tag_text = ','.join(k + '=' + json.dumps(v, ensure_ascii=False) for k, v in (tags or {}).items())
            lines.append(f'source_ipinfo_{name}{{{tag_text}}} {value}')
        with self.lock:
            metric('discovery_up', self.up if now - self.last_discovery < 240 else 0)
            metric('last_discovery_timestamp_seconds', self.last_discovery)
            metric('requests_total', self.requests)
            metric('lookup_errors_total', self.errors)
            metric('discovery_errors_total', self.discovery_errors)
            metric('cache_entries', len(self.cache))
            for address in self.current:
                entry = self.cache.get(address, {})
                if entry.get('data') is None or now - entry.get('checked', 0) > 2592000:
                    continue  # Never retain attribution indefinitely after API failure.
                metric('info', 1, dict(source_ip=address, source_ip_display=source_display(address, entry['data']['country_code']), **entry['data']))
                metric('lookup_timestamp_seconds', entry['checked'], dict(source_ip=address))
        return ('\n'.join(lines) + '\n').encode()


def serve(config):
    collector = Enrichment(config)
    def worker():
        while True:
            try:
                collector.cycle()
            except Exception as exc:
                logging.warning('Enrichment cycle failed (%s)', type(exc).__name__)
            time.sleep(collector.discovery_interval)
    threading.Thread(target=worker, daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/metrics':
                self.send_error(404)
                return
            body = collector.render()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    host, port = config.get('listen', '127.0.0.1:9120').rsplit(':', 1)
    if host != '127.0.0.1':
        raise ValueError('Enrichment metrics must bind to IPv4 loopback')
    HTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/sing-box-ipinfo/config.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with open(args.config, encoding='utf-8') as stream:
        serve(json.load(stream))
