#!/usr/bin/env python3
"""Read-only central PromQL validation. No IPinfo calls or credentials needed."""
import json
import time
import urllib.parse
import urllib.request
from build_dashboard import enrich, INFO_QUERY


def main():
    at = str(time.time())
    def query(expression):
        url = 'http://127.0.0.1:9090/api/v1/query?' + urllib.parse.urlencode(dict(query=expression, time=at))
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.load(response)
        assert data['status'] == 'success', data.get('error', 'PromQL failed')
        return data['data']['result']
    def normalized(rows):
        return {tuple(sorted((k, v) for k, v in r['metric'].items() if k not in ('country_code', 'asn', 'as_name', 'source_ip_display', '__name__'))): r['value'][1] for r in rows}
    assert not query('count by(source_ip)(source_ipinfo_info) > 1'), 'duplicate IP metadata would multiply rows'
    assert query('up{job="source-ipinfo"} == 1'), 'central enrichment target not healthy'
    assert query('source_ipinfo_discovery_up == 1'), 'discovery is unavailable'
    for base in (
        'sum by(instance,service,protocol,source_ip)(singbox_source_protocol_connections)',
        'sum by(instance,service,protocol,source_ip)(singbox_tcp_source_rtt_sum) / sum by(instance,service,protocol,source_ip)(singbox_tcp_source_rtt_count)',
        'sum by(instance,source_ip,direction)(singbox_source_active_bytes)',
    ):
        before = normalized(query(base))
        after = normalized(query(enrich(base)))
        assert before == after, 'enrichment changed source rows or values'
        absent = enrich(base).replace(INFO_QUERY, '(vector(1) > 2)')
        assert normalized(query(absent)) == before, 'missing attribution dropped source rows'
        assert all(r['metric']['source_ip_display'] == r['metric']['source_ip'] for r in query(absent)), 'unknown country must show plain IP'
    print('PASS: live country/ASN metadata is unique; all three enriched queries retain identical source rows/values, including simulated unavailable enrichment')


if __name__ == '__main__':
    main()
