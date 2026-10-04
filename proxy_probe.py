#!/usr/bin/env python3
"""Central, bounded HTTPS probes through real sing-box client outbounds.

No HTTP request starts a probe. One short-lived client per scheduled cycle;
fresh transports/authentication, no packet capture, no remote SSH at runtime.
"""
import argparse
import collections
import concurrent.futures
import http.server
import ipaddress
import json
import logging
import math
import os
import pathlib
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse


def validate(config):
    for name in ('listen', 'state_file', 'runtime_dir', 'targets', 'checks'):
        if name not in config:
            raise ValueError('missing probe configuration field: ' + name)
    host, port = config['listen'].rsplit(':', 1)
    if not ipaddress.ip_address(host).is_loopback or not 1 <= int(port) <= 65535:
        raise ValueError('metrics must bind to loopback')
    if not 30 <= config.get('interval', 60) <= 3600 or not 1 <= config.get('timeout', 8) <= 15:
        raise ValueError('invalid interval/timeout')
    if not 1 <= len(config['targets']) <= 32 or not 1 <= len(config['checks']) <= 4:
        raise ValueError('invalid target/check count')
    if not 1 <= config.get('workers', 2) <= 4:
        raise ValueError('invalid concurrency')
    if not 1024 <= config.get('client_port_base', 19360) <= 65535 - len(config['targets']):
        raise ValueError('invalid client port range')
    def label(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value))
    if not label(config.get('vantage', 'central')):
        raise ValueError('invalid vantage label')
    identities = set()
    for target in config['targets']:
        if not all(label(target.get(k)) for k in ('instance', 'service', 'protocol', 'id')):
            raise ValueError('invalid target labels')
        if target['id'] in identities:
            raise ValueError('duplicate target ID')
        identities.add(target['id'])
        if target.get('outbound', {}).get('type') != target['protocol']:
            raise ValueError('outbound protocol does not match target')
    names = set()
    for check in config['checks']:
        url = urllib.parse.urlsplit(check['url'])
        if not label(check.get('name')) or check['name'] in names:
            raise ValueError('invalid/duplicate check name')
        names.add(check['name'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError('checks require HTTPS URLs without credentials/fragments')
        if not isinstance(check.get('status', 200), int) or not 100 <= check.get('status', 200) <= 599:
            raise ValueError('invalid expected HTTP status')


def client_config(config):
    inbounds, outbounds, rules = [], [], []
    for index, target in enumerate(config['targets']):
        tag = 'probe-' + target['id']
        inbounds.append(dict(type='mixed', tag=tag, listen='127.0.0.1',
                             listen_port=config.get('client_port_base', 19360) + index))
        outbounds.append(dict(target['outbound'], tag=tag))
        rules.append(dict(inbound=[tag], action='route', outbound=tag))
    return dict(log=dict(level='error'), inbounds=inbounds, outbounds=outbounds, route=dict(rules=rules))


def run_check(config, index, check, directory):
    started = time.monotonic()
    body = pathlib.Path(directory) / (str(index) + '-' + check['name'] + '.body')
    port = config.get('client_port_base', 19360) + index
    command = [config.get('curl_binary', '/usr/bin/curl'), '-q', '--silent', '--show-error',
               '--proxy', f'socks5h://127.0.0.1:{port}', '--noproxy', '',
               '--connect-timeout', str(min(4, config.get('timeout', 8))),
               '--max-time', str(config.get('timeout', 8)), '--max-filesize', '32768',
               '--max-redirs', '0', '--output', str(body), '--write-out', '%{json}', check['url']]
    env = {k: v for k, v in os.environ.items() if k.lower() not in
           ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    result = dict(success=0, status=0, duration=0, error=1)
    try:
        process = subprocess.run(command, capture_output=True, text=True, env=env,
                                 timeout=config.get('timeout', 8) + 2)
        data = json.loads(process.stdout)
        result['status'] = int(data.get('http_code', 0))
        result['duration'] = max(0, float(data.get('time_total', 0)))
        valid = process.returncode == 0 and result['status'] == check.get('status', 200)
        if check.get('trace_ip', False) and valid:
            # A real trace response must contain a valid exit IP; never store it
            # in metrics or logs. This rejects challenge/error bodies with 200.
            text = body.read_text()[:32768]
            ip = next((line[3:] for line in text.splitlines() if line.startswith('ip=')), '')
            ipaddress.ip_address(ip)
        result['success'] = int(valid)
        result['error'] = int(not valid)
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        pass  # Never log subprocess stderr or credential-bearing client settings.
    result['attempt_duration'] = time.monotonic() - started
    if not math.isfinite(result['duration']):
        result.update(success=0, duration=0, error=1)
    return result


class Prober:
    def __init__(self, config):
        validate(config)
        self.config = config
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.results = {}
        self.counts = collections.defaultdict(int)
        self.runner_up = 0
        self.last_cycle = 0
        self.cycle_duration = 0
        self.cycles = 0
        try:
            data = json.loads(pathlib.Path(config['state_file']).read_text())
            for key, value in data.get('counts', []):
                if len(key) == 3 and isinstance(value, int) and value >= 0:
                    self.counts[tuple(key)] = value
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        path = pathlib.Path(self.config['state_file'])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix('.new')
        with temporary.open('w') as stream:
            os.chmod(temporary, 0o600)
            json.dump(dict(counts=list(self.counts.items())), stream)
        os.replace(temporary, path)

    def record(self, target, check, result):
        with self.lock:
            self.results[(target['id'], check['name'])] = dict(result, timestamp=time.time())
            self.counts[(target['id'], check['name'], 'success' if result['success'] else 'failure')] += 1

    def cycle(self):
        started = time.monotonic()
        process = None
        jobs = [(index, target, check) for index, target in enumerate(self.config['targets'])
                for check in self.config['checks']]
        recorded = set()
        try:
            with tempfile.TemporaryDirectory(prefix='cycle-', dir=self.config['runtime_dir']) as directory:
                path = pathlib.Path(directory) / 'client.json'
                path.write_text(json.dumps(client_config(self.config)))
                os.chmod(path, 0o600)
                process = subprocess.Popen([self.config.get('sing_box_binary', '/usr/bin/sing-box'), 'run', '-c', str(path)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 5
                pending = set(range(len(self.config['targets'])))
                while pending:
                    if process.poll() is not None or time.monotonic() >= deadline or self.stop.is_set():
                        raise RuntimeError('probe client unavailable')
                    for index in list(pending):
                        try:
                            with socket.create_connection(('127.0.0.1', self.config.get('client_port_base', 19360) + index), timeout=0.1):
                                pending.remove(index)
                        except OSError:
                            pass
                    self.stop.wait(0.05)
                with self.lock:
                    self.runner_up = 1
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.get('workers', 2)) as pool:
                    futures = {pool.submit(run_check, self.config, index, check, directory): (target, check)
                               for index, target, check in jobs}
                    for future in concurrent.futures.as_completed(futures):
                        target, check = futures[future]
                        self.record(target, check, future.result())
                        recorded.add((target['id'], check['name']))
                if process.poll() is not None:
                    with self.lock:
                        self.runner_up = 0
        except Exception as exc:
            with self.lock:
                self.runner_up = 0
            for _, target, check in jobs:
                if (target['id'], check['name']) in recorded:
                    continue
                self.record(target, check, dict(success=0, status=0, duration=0,
                    attempt_duration=time.monotonic() - started, error=1))
            logging.warning('Probe cycle unavailable (%s)', type(exc).__name__)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            with self.lock:
                self.cycle_duration = time.monotonic() - started
                self.last_cycle = time.time()
                self.cycles += 1
                self.save()

    def worker(self):
        # Keep cycles >= interval apart even after a long failure cycle; never
        # overlap clients or spin through a credential/configuration failure.
        while not self.stop.is_set():
            started = time.monotonic()
            self.cycle()
            self.stop.wait(max(1, self.config.get('interval', 60) - (time.monotonic() - started)))

    def render(self):
        lines = []
        names = set()
        def emit(name, value, labels=None, kind='gauge'):
            name = 'singbox_probe_' + name
            if name not in names:
                lines.extend([f'# HELP {name} Central scheduled HTTPS proxy check {name}.', f'# TYPE {name} {kind}'])
                names.add(name)
            tags = '{' + ','.join(k + '=' + json.dumps(str(v)) for k, v in (labels or {}).items()) + '}'
            lines.append(f'{name}{tags} {value}')
        now = time.time()
        with self.lock:
            emit('runner_up', int(self.runner_up and now - self.last_cycle <= self.config.get('interval', 60) * 2 + 20))
            emit('last_cycle_timestamp_seconds', self.last_cycle)
            emit('cycle_duration_seconds', self.cycle_duration)
            emit('cycles_total', self.cycles, kind='counter')
            for target in self.config['targets']:
                for check in self.config['checks']:
                    tags = {k: target[k] for k in ('instance', 'service', 'protocol')}
                    tags.update(target=target['id'], probe=check['name'], vantage=self.config.get('vantage', 'central'))
                    result = self.results.get((target['id'], check['name']))
                    fresh = result is not None and now - result['timestamp'] <= self.config.get('interval', 60) * 2 + 20
                    emit('result_fresh', int(fresh), tags)
                    if result:
                        emit('last_run_timestamp_seconds', result['timestamp'], tags)
                    if fresh:
                        emit('success', result['success'], tags)
                        emit('http_status_code', result['status'], tags)
                        emit('attempt_duration_seconds', result['attempt_duration'], tags)
                        if result['success']:
                            emit('duration_seconds', result['duration'], tags)
                    for outcome in ('success', 'failure'):
                        emit('checks_total', self.counts[(target['id'], check['name'], outcome)], dict(tags, outcome=outcome), 'counter')
        return ('\n'.join(lines) + '\n').encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=pathlib.Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    prober = Prober(config)
    pathlib.Path(config['runtime_dir']).mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.once:
        prober.cycle()
        print(prober.render().decode(), end='')
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: prober.stop.set())
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/metrics':
                self.send_error(404)
                return
            body = prober.render()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    host, port = config['listen'].rsplit(':', 1)
    server = http.server.HTTPServer((host, int(port)), Handler)
    server.timeout = 0.5
    worker = threading.Thread(target=prober.worker, daemon=True)
    worker.start()
    try:
        while not prober.stop.is_set():
            server.handle_request()
    finally:
        prober.stop.set()
        server.server_close()
        worker.join(timeout=120)


if __name__ == '__main__':
    main()
