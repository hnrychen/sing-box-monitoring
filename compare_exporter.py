#!/usr/bin/env python3
"""Offline Python/Go semantic parity using identical API and TCP fixtures."""
import argparse
import copy
import json
import math
import pathlib
import re
import subprocess
import tempfile
from unittest.mock import patch

import exporter


def samples(text):
    result = {}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        name, labels, value = re.fullmatch(r'(\w+)\{(.*)\} (\S+)', line).groups()
        if name in ('singbox_exporter_cpu_seconds_total', 'singbox_exporter_max_rss_bytes',
                    'singbox_exporter_start_time_seconds', 'singbox_exporter_build_info'):
            continue
        tags = tuple(sorted((key, json.loads('"' + value + '"')) for key, value in
                            re.findall(r'(\w+)="((?:\\.|[^"\\])*)"', labels)))
        key = name, tags
        if key in result:
            raise AssertionError('duplicate series: ' + str(key))
        result[key] = float(value)
    return result


def fixtures():
    service = dict(name='core', tcp_uid=988, url='http://127.0.0.1:19090', secret='fixture-only',
                   inbounds={'ss': dict(type='shadowsocks', port=443, listen='::')}, outbounds=['direct'])
    config = dict(services=[service], source_mode='raw', connection_details=True, tcp_rtt=True,
                  max_connections=4096, poll_interval=2, tcp_poll_interval=2)
    conn = dict(id='one', upload=10, download=20, start='2026-10-12T00:00:00Z', chains=['direct'],
                metadata=dict(type='shadowsocks/ss', network='tcp', sourceIP='203.0.113.10', sourcePort='12345',
                              destinationIP='198.51.100.1', destinationPort=443, host='example.test\n\x00'))
    data = dict(uploadTotal=100, downloadTotal=200, memory=1000, connections=[conn])
    record = dict(local_ip='127.0.0.1', local_port=443, peer_ip='203.0.113.10', peer_port=12345, uid=988,
                  socket_cookie=[1, 2], rtt_seconds=.025, variation_seconds=.002, ack_age_seconds=1,
                  payload_idle_seconds=2, send_queue_bytes=4, receive_queue_bytes=2,
                  cwnd_segments=10, send_mss_bytes=1448, notsent_bytes=1, total_retransmissions=5)
    now = 1791763230
    def scenario(name, snapshots, records=None, cfg=None, fail=None, render_after=0):
        steps = [dict(now=now + i*2, service='core', data=d) for i, d in enumerate(snapshots)]
        t = steps[-1]['now'] if steps else now
        if records is not None:
            # Consecutive kernel snapshots exercise cookie baseline/delta tracking.
            steps.extend([dict(now=t, records=records), dict(now=t+1, records=[dict(r, total_retransmissions=r.get('total_retransmissions', 0)+2) for r in records])])
            t += 1
        if fail:
            steps.append(dict(now=t, fail=fail))
        return name, dict(config=cfg or config, steps=steps, now=t+render_after)
    yield scenario('baseline-authenticated', [data], [record])
    second = copy.deepcopy(data); second['connections'][0].update(upload=15, download=35)
    yield scenario('incremental-bytes', [data, second], [record])
    third = copy.deepcopy(second); third['connections'].append(dict(conn, id='two', upload=30))
    yield scenario('new-connection', [data, second, third], [record])
    reset = copy.deepcopy(second); reset.update(uploadTotal=0, downloadTotal=0)
    yield scenario('core-reset', [data, reset], [record])
    yield scenario('zero-idle', [dict(data, connections=[])], [])
    yield scenario('null-connections', [dict(data, connections=None)], [])
    yield scenario('failed-api-no-client-rtt', [data], [record], fail='core')
    yield scenario('stale-api-and-tcp', [data], [record], render_after=20)
    for name, changes in [('wrong-uid', {'uid': 0}), ('wrong-port', {'local_port': 80}),
                          ('unmatched-peer-port', {'peer_port': 999}), ('zero-rtt', {'rtt_seconds': 0}),
                          ('idle-ack', {'ack_age_seconds': 121}), ('idle-payload', {'payload_idle_seconds': 121}),
                          ('mapped-peer', {'peer_ip': '203.0.113.10'})]:
        yield scenario(name, [data], [dict(record, **changes)])
    old_kernel = {k: v for k, v in record.items() if k not in ('cwnd_segments', 'send_mss_bytes', 'notsent_bytes', 'total_retransmissions')}
    yield scenario('older-kernel', [data], [old_kernel])
    ambiguous = copy.deepcopy(config); ambiguous['services'][0]['inbounds']['duplicate'] = dict(service['inbounds']['ss'])
    yield scenario('ambiguous-listener', [data], [record], cfg=ambiguous)
    for mode in ('off', 'subnet'):
        yield scenario('privacy-'+mode, [data, second], [record], cfg=dict(config, source_mode=mode))
    for ip in ('2001:db8::1', '::ffff:203.0.113.10', 'invalid'):
        v6 = copy.deepcopy(data); v6['connections'][0]['metadata']['sourceIP'] = ip
        yield scenario('source-'+ip, [v6], [dict(record, peer_ip=ip.replace('::ffff:', ''))])
    unknown = copy.deepcopy(data); unknown['connections'][0]['metadata'].update(type='weird/tag', network='other')
    unknown['connections'][0]['chains'] = ['unknown', 'direct']
    yield scenario('unknown-inbound-network-egress', [unknown])
    closed = dict(data, connections=[])
    yield scenario('closed-source-ttl', [data, second, closed], [], render_after=3605)
    for name, final, render_after in [('recent-boundary', 302, 0), ('recent-render-expiry', 302, .001),
                                      ('idle-open-session', 303, 0)]:
        name, fixture = scenario(name, [data, second, second], render_after=render_after)
        fixture['steps'][-1]['now'] = now + final
        fixture['now'] = now + final + render_after
        yield name, fixture
    no_start = copy.deepcopy(data); no_start['connections'][0].pop('start')
    yield scenario('restart-unknown-activity', [no_start])


def compare(binary):
    count = 0
    with tempfile.TemporaryDirectory() as directory:
        for name, fixture in fixtures():
            collector = exporter.Collector(fixture['config'])
            for step in fixture['steps']:
                if 'data' in step:
                    service = next(s for s in fixture['config']['services'] if s['name'] == step['service'])
                    collector.update(service, step['data'], step['now'])
                if 'records' in step:
                    collector.tcp_update(step['records'], step['now'])
                if 'fail' in step:
                    state = collector.state[step['fail']]
                    state.update(up=0, errors=state.get('errors', 0)+1)
            with patch.object(exporter.time, 'time', return_value=fixture['now']):
                reference_text = collector.render().decode()
                reference = samples(reference_text)
            path = pathlib.Path(directory) / 'fixture.json'
            path.write_text(json.dumps(fixture))
            go = json.loads(subprocess.check_output([str(binary), '--fixture', str(path)]))
            actual = samples(go['metrics'])
            reference_types = dict(re.findall(r'^# TYPE (\w+) (\w+)$', reference_text, re.M))
            actual_types = dict(re.findall(r'^# TYPE (\w+) (\w+)$', go['metrics'], re.M))
            if reference_types != actual_types:
                raise AssertionError(name + ': metric type contracts differ')
            if set(reference) != set(actual):
                raise AssertionError(f'{name}: missing={set(reference)-set(actual)} extra={set(actual)-set(reference)}')
            for key, value in reference.items():
                if not math.isclose(value, actual[key], rel_tol=1e-12, abs_tol=1e-9):
                    raise AssertionError(f'{name}: {key} python={value} go={actual[key]}')
            if collector.connection_snapshot(fixture['now']) != go['connections']:
                raise AssertionError(name + ': connection details differ')
            count += 1
    print(json.dumps(dict(parity_scenarios=count, result='passed')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=pathlib.Path, required=True)
    compare(parser.parse_args().binary.resolve())
