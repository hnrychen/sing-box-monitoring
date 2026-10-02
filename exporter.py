#!/usr/bin/env python3
"""Dependency-free, bounded, read-only sing-box Clash API Prometheus exporter."""
import argparse
import collections
import hmac
import ipaddress
import json
import logging
import math
import os
import resource
import ssl
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
import tcpdiag

VERSION = '1.1.0'
MAX_RESPONSE = 8 * 1024 * 1024
API_CLIENT = threading.local()
RTT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1, 2, 5, math.inf)


def labels(values):
    def esc(s):
        return str(s).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')
    return '{' + ','.join(f'{k}="{esc(v)}"' for k, v in values.items()) + '}'


class Metrics:
    def __init__(self):
        self.lines = []
        self.names = set()

    def add(self, name, value, tags, help_text, kind='gauge'):
        name = 'singbox_' + name
        if name not in self.names:
            self.lines.extend([f'# HELP {name} {help_text}', f'# TYPE {name} {kind}'])
            self.names.add(name)
        if not math.isfinite(float(value)):
            return
        self.lines.append(f'{name}{labels(tags)} {value}')

    def render(self):
        return ('\n'.join(self.lines) + '\n').encode()


def fetch(service):
    request = urllib.request.Request(service['url'] + '/connections',
                                    headers={'Authorization': 'Bearer ' + service['secret']})
    # Never consult HTTP_PROXY for loopback control APIs.
    if not hasattr(API_CLIENT, 'opener'):
        # Python 3.13's default opener initializes an HTTPS trust store even for
        # HTTP. Rebuilding it each poll wastes CPU/RAM. APIs are HTTP loopback:
        # a reusable HTTP-only opener also refuses redirects/proxy traversal.
        API_CLIENT.opener = urllib.request.OpenerDirector()
        for handler in (urllib.request.HTTPHandler(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
            API_CLIENT.opener.add_handler(handler)
    with API_CLIENT.opener.open(request, timeout=3) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError('snapshot response exceeds byte budget')
    data = json.loads(raw)
    if not isinstance(data.get('connections'), (list, type(None))):
        raise ValueError('invalid connections schema')
    for key in ('uploadTotal', 'downloadTotal', 'memory'):
        if not isinstance(data.get(key), (int, float)) or data[key] < 0:
            raise ValueError('invalid total schema')
    return data


class Collector:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.state = {}
        self.sources = []
        path = config.get('source_state')
        if path and os.path.exists(path):
            with open(path) as stream:
                self.sources = json.load(stream)[:config.get('max_sources', 32)]
        self.last_save = 0
        self.started = time.time()
        self.renders = 0
        self.processes = {}
        self.tcp = {}
        self.certificate_expiry = None
        if config.get('tls_cert'):
            self.certificate_expiry = ssl.cert_time_to_seconds(ssl._ssl._test_decode_cert(config['tls_cert'])['notAfter'])

    def process(self, service):
        name = service['name']
        cached = self.processes.get(name, {})
        if time.time() - cached.get('collected', 0) < self.config.get('process_interval', 30):
            return
        values = {'collected': time.time()}
        try:
            result = subprocess.run(['systemctl', 'show', name, '-p', 'MainPID', '-p', 'ActiveState'],
                                    check=True, capture_output=True, text=True, timeout=2)
            unit = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            values['unit_active'] = int(unit.get('ActiveState') == 'active')
            pid = int(unit.get('MainPID', '0'))
            if pid:
                with open(f'/proc/{pid}/stat') as stream:
                    fields = stream.read().rsplit(')', 1)[1].split()
                with open('/proc/uptime') as stream:
                    uptime = float(stream.read().split()[0])
                ticks = os.sysconf('SC_CLK_TCK')
                values.update(cpu_seconds_total=(int(fields[11]) + int(fields[12])) / ticks,
                              resident_memory_bytes=int(fields[21]) * os.sysconf('SC_PAGE_SIZE'),
                              threads=int(fields[17]),
                              uptime_seconds=max(0, uptime - int(fields[19]) / ticks))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # Docker deployments need not have access to host systemd.
        with self.lock:
            self.processes[name] = values

    def source(self, address):
        mode = self.config.get('source_mode', 'off')
        if mode == 'off':
            return 'hidden'
        try:
            ip = ipaddress.ip_address(address)
            address = str(ip) if mode == 'raw' else str(ipaddress.ip_network(f'{ip}/{24 if ip.version == 4 else 64}', strict=False))
        except ValueError:
            return 'unknown'
        if address in self.sources:
            return address
        if len(self.sources) >= self.config.get('max_sources', 32):
            return 'other'
        self.sources.append(address)
        return address

    def tcp_update(self, records, now=None):
        groups, sources = {}, {}
        candidates = []
        for service in self.config['services']:
            for inbound, info in service.get('inbounds', {}).items():
                port = int(info.get('port', 0))
                if not port or info.get('type') in ('hysteria', 'hysteria2', 'tuic') or info.get('network') == 'udp':
                    continue
                key = (service['name'], info['type'], inbound)
                candidates.append((key, info, service.get('tcp_uid')))
                groups[key] = dict(sockets=0, count=0, sum=0, maximum=0, variation=0,
                                   ack_age=0, buckets=[0] * len(RTT_BUCKETS))
        unmatched = 0
        for record in records:
            matching = []
            for key, info, uid in candidates:
                bind = info.get('listen', '::')
                bind = bind.removeprefix('::ffff:')
                if (record['local_port'] == int(info['port']) and
                    (uid is None or record['uid'] == uid) and
                    (bind in ('::', '0.0.0.0', '') or bind == record['local_ip'])):
                    matching.append(key)
            if len(matching) != 1:
                unmatched += 1
                continue  # Shared/ambiguous listeners are not guessed.
            key = matching[0]
            group = groups[key]
            group['sockets'] += 1
            rtt = record.get('rtt_seconds', 0)
            if rtt <= 0 or record.get('ack_age_seconds', math.inf) > self.config.get('tcp_max_idle', 120):
                continue
            group['count'] += 1
            group['sum'] += rtt
            group['maximum'] = max(group['maximum'], rtt)
            group['variation'] += record['variation_seconds']
            group['ack_age'] = max(group['ack_age'], record['ack_age_seconds'])
            for index, bound in enumerate(RTT_BUCKETS):
                group['buckets'][index] += int(rtt <= bound)
            source = self.source(record['peer_ip'])
            if key + (source,) not in sources and len(sources) >= 128:
                source = 'other'
            values = sources.setdefault(key + (source,), [0, 0, 0, 0])
            values[0] += 1
            values[1] += rtt
            values[2] = max(values[2], rtt)
            values[3] = max(values[3], record['ack_age_seconds'])
        self.tcp = dict(up=1, timestamp=time.time() if now is None else now, groups=groups, sources=sources,
                        unmatched=unmatched, errors=self.tcp.get('errors', 0))

    def tcp_poll(self):
        ports = {int(info.get('port', 0)) for s in self.config['services'] for info in s.get('inbounds', {}).values()
                 if int(info.get('port', 0)) and info.get('type') not in ('hysteria', 'hysteria2', 'tuic') and info.get('network') != 'udp'}
        started = time.monotonic()
        try:
            records = tcpdiag.snapshot(ports, max_sockets=self.config.get('max_connections', 4096))
            with self.lock:
                self.tcp_update(records)
        except Exception as exc:
            with self.lock:
                self.tcp.update(up=0, errors=self.tcp.get('errors', 0) + 1)
            logging.warning('TCP RTT collection failed (%s)', type(exc).__name__)
        with self.lock:
            self.tcp['duration'] = time.monotonic() - started

    def tcp_worker(self):
        interval = max(2, self.config.get('tcp_poll_interval', 30))
        while True:
            started = time.monotonic()
            self.tcp_poll()
            time.sleep(max(0.2, interval - (time.monotonic() - started)))

    def update(self, service, data, now=None):
        now = time.time() if now is None else now
        name = service['name']
        old = self.state.get(name, {})
        previous = old.get('previous', {})
        totals = (data['uploadTotal'], data['downloadTotal'])
        reset = any(a < b for a, b in zip(totals, old.get('totals', totals)))
        if reset:
            previous = {}
        inventory = service.get('inbounds', {})
        valid_outbounds = set(service.get('outbounds', [])) | {'direct', 'DIRECT'}
        groups = collections.defaultdict(lambda: [0, 0, 0, 0])
        sources = collections.defaultdict(lambda: [0, 0, 0])
        source_protocols = collections.defaultdict(int)
        observed = collections.defaultdict(lambda: [0, 0, 0], {k: v.copy() for k, v in old.get('observed', {}).items()})
        source_observed = collections.defaultdict(lambda: [0, 0], {k: v.copy() for k, v in old.get('source_observed', {}).items()})
        following = {}
        dropped = 0
        connections = data.get('connections') or []
        # Reject rather than publish silently truncated "exact" active counts.
        if len(connections) > self.config.get('max_connections', 10000):
            raise ValueError('active connection budget exceeded')
        for conn in connections:
            md = conn.get('metadata', {})
            protocol, _, inbound = str(md.get('type', 'unknown')).partition('/')
            if not inbound:
                inbound = protocol
            protocol = inventory.get(inbound, {}).get('type', protocol)
            if inventory and inbound not in inventory:
                inbound, protocol = 'unknown', 'unknown'
            network = md.get('network', 'unknown')
            if network not in ('tcp', 'udp', 'icmp'):
                network = 'unknown'
            chain = conn.get('chains') or ['direct']
            # Sing-box reverses its chain: first element is final egress.
            outbound = str(chain[0])
            if outbound not in valid_outbounds:
                outbound = 'other'
            group = (protocol, inbound, network, outbound)
            if (group not in observed and len(observed) >= 128) or (group not in groups and len(groups) >= 128):
                group = ('other', 'other', 'other', 'other')
                dropped += 1
            source = self.source(str(md.get('sourceIP', '')))
            source_protocol = (protocol, source)
            if source_protocol not in source_protocols and len(source_protocols) >= 128:
                source_protocol = ('other', 'other')
            source_protocols[source_protocol] += 1
            upload, download = max(0, int(conn.get('upload', 0))), max(0, int(conn.get('download', 0)))
            cid = str(conn['id'])
            before = previous.get(cid)
            # The first-ever snapshot is a baseline, not newly observed traffic.
            delta = (max(0, upload - before[0]), max(0, download - before[1])) if before else ((upload, download) if old else (0, 0))
            if old:
                observed[group][0] += delta[0]
                observed[group][1] += delta[1]
                observed[group][2] += int(before is None)
                source_observed[source][0] += delta[0]
                source_observed[source][1] += delta[1]
            following[cid] = (upload, download)
            age = 0
            try:
                from datetime import datetime
                age = max(0, now - datetime.fromisoformat(conn['start'].replace('Z', '+00:00')).timestamp())
            except (KeyError, ValueError):
                pass
            g = groups[group]
            g[0] += 1
            g[1] += upload
            g[2] += download
            g[3] = max(g[3], age)
            s = sources[source]
            s[0] += 1
            s[1] += upload
            s[2] += download
        # Zero series for known idle inbounds, retaining protocol visibility.
        for inbound, info in inventory.items():
            for network in ('tcp', 'udp'):
                groups[(info['type'], inbound, network, 'direct')]
                observed[(info['type'], inbound, network, 'direct')]
        self.state[name] = dict(up=1, timestamp=now, totals=totals, memory=data['memory'],
                                groups=groups, sources=sources, source_protocols=source_protocols, previous=following,
                                observed=observed, source_observed=source_observed,
                                errors=old.get('errors', 0), resets=old.get('resets', 0) + int(reset),
                                dropped=dropped, connections=len(connections), duration=0)

    def poll(self, service):
        started = time.monotonic()
        self.process(service)
        try:
            data = fetch(service)
            with self.lock:
                self.update(service, data)
        except Exception as exc:
            with self.lock:
                state = self.state.setdefault(service['name'], {})
                state.update(up=0, errors=state.get('errors', 0) + 1)
            logging.warning('%s collection failed (%s)', service['name'], type(exc).__name__)
        with self.lock:
            self.state[service['name']]['duration'] = time.monotonic() - started

    def worker(self, service):
        interval = max(2, self.config.get('poll_interval', 5))
        while True:
            started = time.monotonic()
            self.poll(service)
            with self.lock:
                path = self.config.get('source_state')
                if path and time.time() - self.last_save >= 60:
                    try:
                        temp = path + '.tmp'
                        with open(temp, 'w') as stream:
                            json.dump(self.sources, stream)
                        os.replace(temp, path)
                        self.last_save = time.time()
                    except OSError:
                        logging.error('Cannot persist source slots')
            time.sleep(max(0.2, interval - (time.monotonic() - started)))

    def render(self):
        m = Metrics()
        with self.lock:
            for service in self.config['services']:
                tags = {'service': service['name']}
                s = self.state.get(service['name'], {})
                fresh = time.time() - s.get('timestamp', 0) < max(2, self.config.get('poll_interval', 5)) * 3 + 3
                up = s.get('up', 0) if fresh else 0
                m.add('up', up, tags, 'Clash API snapshot collection success and freshness.')
                m.add('last_success_timestamp_seconds', s.get('timestamp', 0), tags, 'Time of last successful snapshot.')
                m.add('collection_duration_seconds', s.get('duration', 0), tags, 'Local snapshot collection duration.')
                m.add('collection_errors_total', s.get('errors', 0), tags, 'Failed snapshots.', 'counter')
                m.add('api_resets_total', s.get('resets', 0), tags, 'Observed upstream total counter resets.', 'counter')
                for key, value in self.processes.get(service['name'], {}).items():
                    if key != 'collected':
                        m.add('process_' + key, value, tags, 'Native systemd process metric; configurable refresh interval.', 'counter' if key.endswith('_total') else 'gauge')
                if not up:
                    continue  # Never pass stale values off as healthy or zero traffic.
                for direction, value in zip(('upload', 'download'), s['totals']):
                    m.add('traffic_bytes_total', value, dict(tags, direction=direction), 'Exact API payload bytes since sing-box start; excludes transport overhead.', 'counter')
                m.add('memory_bytes', s['memory'], tags, 'Sing-box API in-use Go memory, not RSS.')
                m.add('connections', s['connections'], tags, 'Active routed connections/UDP sessions; DNS outbound excluded.')
                m.add('label_overflow_connections', s['dropped'], tags, 'Connections assigned to overflow route labels.')
                for key, values in s['groups'].items():
                    gtags = dict(tags, **dict(zip(('protocol', 'inbound', 'network', 'outbound'), key)))
                    m.add('route_connections', values[0], gtags, 'Active routed connections by inbound protocol, network and egress.')
                    m.add('route_oldest_connection_seconds', values[3], gtags, 'Age of oldest active connection.')
                    for direction, value in zip(('upload', 'download'), values[1:3]):
                        m.add('route_active_bytes', value, dict(gtags, direction=direction), 'Lifetime bytes of currently active connections; gauge, NOT a counter.')
                for key, values in s['observed'].items():
                    gtags = dict(tags, **dict(zip(('protocol', 'inbound', 'network', 'outbound'), key)))
                    for direction, value in zip(('upload', 'download'), values[:2]):
                        m.add('route_observed_bytes_total', value, dict(gtags, direction=direction), 'Sampled per-route bytes; misses short-lived connections and bytes after final sample.', 'counter')
                    m.add('route_observed_connections_total', values[2], gtags, 'New connections seen after first snapshot; sampling lower bound.', 'counter')
                for source, values in s['sources'].items():
                    stags = dict(tags, source_ip=source)
                    m.add('source_connections', values[0], stags, 'Active connections by bounded source IP slot.')
                    for direction, value in zip(('upload', 'download'), values[1:]):
                        m.add('source_active_bytes', value, dict(stags, direction=direction), 'Lifetime bytes of active connections for this source.')
                for (protocol, source), count in s['source_protocols'].items():
                    m.add('source_protocol_connections', count, dict(tags, protocol=protocol, source_ip=source), 'Active routed sessions by protocol and bounded source IP; at most 128 groups plus overflow.')
                for source, values in s['source_observed'].items():
                    for direction, value in zip(('upload', 'download'), values):
                        m.add('source_observed_bytes_total', value, dict(tags, source_ip=source, direction=direction), 'Sampled source bytes; NOT billing grade.', 'counter')
                for inbound, info in service.get('inbounds', {}).items():
                    m.add('inbound_info', 1, dict(tags, inbound=inbound, protocol=info['type'], port=info.get('port', 0)), 'Configured inbound inventory, including idle protocols.')
            if self.config.get('tcp_rtt', False):
                fresh = time.time() - self.tcp.get('timestamp', 0) < max(2, self.config.get('tcp_poll_interval', 30)) * 3 + 3
                up = self.tcp.get('up', 0) if fresh else 0
                m.add('tcp_collector_up', up, {}, 'Successful, fresh Linux TCP diagnostic snapshot.')
                m.add('tcp_collector_last_success_timestamp_seconds', self.tcp.get('timestamp', 0), {}, 'Last successful TCP socket snapshot.')
                m.add('tcp_collector_duration_seconds', self.tcp.get('duration', 0), {}, 'Time spent reading kernel socket diagnostics.')
                m.add('tcp_collector_errors_total', self.tcp.get('errors', 0), {}, 'Rejected/failed TCP socket snapshots.', 'counter')
                if up:
                    m.add('tcp_unmatched_sockets', self.tcp['unmatched'], {}, 'Sockets omitted because listener identity was ambiguous or did not match.')
                    for key, values in self.tcp['groups'].items():
                        tags = dict(zip(('service', 'protocol', 'inbound'), key))
                        m.add('tcp_established_sockets', values['sockets'], tags, 'Physical TCP sockets on configured listeners; includes pre-authentication sockets, not logical routed sessions.')
                        m.add('tcp_rtt_snapshot_count', values['count'], tags, 'Current TCP sockets with a positive SRTT and recent ACK; gauge, not a counter.')
                        if not values['count']:
                            continue  # Unsupported/idle is not zero milliseconds.
                        m.add('tcp_rtt_snapshot_sum', values['sum'], tags, 'Sum of current socket smoothed RTT in seconds; gauge, do not rate().')
                        m.add('tcp_rtt_max_seconds', values['maximum'], tags, 'Maximum current smoothed TCP ACK RTT.')
                        m.add('tcp_rtt_variation_sum_seconds', values['variation'], tags, 'Sum of kernel RTT mean deviations; not application jitter.')
                        m.add('tcp_ack_age_max_seconds', values['ack_age'], tags, 'Oldest last-ACK age among included TCP RTT samples.')
                        for bound, count in zip(RTT_BUCKETS, values['buckets']):
                            m.add('tcp_rtt_snapshot_bucket', count, dict(tags, le='+Inf' if math.isinf(bound) else str(bound)), 'Cumulative CURRENT SOCKET RTT bucket; gauge, use histogram_quantile WITHOUT rate().')
                    for key, values in self.tcp['sources'].items():
                        tags = dict(zip(('service', 'protocol', 'inbound', 'source_ip'), key))
                        for name, value in zip(('count', 'sum', 'max_seconds', 'ack_age_max_seconds'), values):
                            m.add('tcp_source_rtt_' + name, value, tags, 'Current TCP peer RTT summary; bounded source labels, recent ACK only. All gauges.')
            m.add('exporter_source_slots', len(self.sources), {}, 'Persistent distinct source labels assigned; full slots aggregate other.')
            m.add('exporter_source_slots_limit', self.config.get('max_sources', 32), {}, 'Maximum distinct source IP labels per host.')
            if self.certificate_expiry:
                m.add('exporter_certificate_expiry_timestamp_seconds', self.certificate_expiry, {}, 'HTTPS certificate expiry; renew and restart exporter before this time.')
        usage = resource.getrusage(resource.RUSAGE_SELF)
        m.add('exporter_cpu_seconds_total', usage.ru_utime + usage.ru_stime, {}, 'Exporter CPU time.', 'counter')
        m.add('exporter_max_rss_bytes', usage.ru_maxrss * 1024, {}, 'Linux peak resident memory.')
        m.add('exporter_start_time_seconds', self.started, {}, 'Exporter process start time.')
        m.add('exporter_build_info', 1, {'version': VERSION}, 'Exporter build version.')
        return m.render()


def serve(config):
    collector = Collector(config)
    if config.get('tcp_rtt', False):
        threading.Thread(target=collector.tcp_worker, daemon=True).start()
    for service in config['services']:
        threading.Thread(target=collector.worker, args=(service,), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'),
                                       ('Bearer ' + config['scrape_token']).encode('utf-8')):
                self.reply(401, b'Unauthorized\n')
            elif self.path == '/metrics':
                self.reply(200, collector.render(), 'text/plain; version=0.0.4; charset=utf-8')
            elif self.path == '/healthz':
                with collector.lock:
                    healthy = all(collector.state.get(s['name'], {}).get('up') == 1 and
                                  time.time() - collector.state[s['name']].get('timestamp', 0) < max(2, config.get('poll_interval', 5)) * 3 + 3
                                  for s in config['services'])
                    if config.get('tcp_rtt', False):
                        healthy = healthy and collector.tcp.get('up') == 1 and time.time() - collector.tcp.get('timestamp', 0) < max(2, config.get('tcp_poll_interval', 30)) * 3 + 3
                self.reply(200 if healthy else 503, b'ok\n' if healthy else b'collection failed\n')
            else:
                self.reply(404, b'Not found\n')

        def reply(self, status, body, content_type='text/plain'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def log_message(self, *_):
            pass

    host, port = config.get('listen', '127.0.0.1:9119').rsplit(':', 1)
    server = HTTPServer((host, int(port)), Handler)
    server.timeout = 2
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config['tls_cert'], config['tls_key'])
    # Set timeouts before TLS handshake, not on an already-wrapped listener.
    original_get_request = server.get_request

    def get_request():
        sock, address = original_get_request()
        sock.settimeout(3)
        try:
            return context.wrap_socket(sock, server_side=True), address
        except Exception:
            sock.close()
            raise

    server.get_request = get_request
    logging.info('sing-box exporter %s listening at %s', VERSION, config.get('listen'))
    server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/sing-box-exporter/config.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with open(args.config) as stream:
        settings = json.load(stream)
    serve(settings)
