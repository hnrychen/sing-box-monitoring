#!/usr/bin/env python3
"""Generate a portable Grafana dashboard; optional live JSON uses Infinity."""
import json
import pathlib

DS = {'type': 'prometheus', 'uid': '${datasource}'}
HOST = 'instance=~"$host",service=~"$service"'
ROUTE = HOST + ',protocol=~"$protocol"'
panels = []
next_id = 0
INFO_QUERY = 'max by(source_ip,country_code,asn,as_name,source_ip_display)(source_ipinfo_info{source_ip_display!=""})'


def enrich(expression):
    # Explicit left join: missing/failed enrichment must not drop source rows.
    fallback = f'label_replace(({expression}), "source_ip_display", "$1", "source_ip", "(.*)")'
    return f'(({expression}) * on(source_ip) group_left(country_code,asn,as_name,source_ip_display) {INFO_QUERY}) or ignoring(country_code,asn,as_name,source_ip_display) ({fallback})'


def panel(title, kind, x, y, w, h, expressions=None, unit='short', description='', **extra):
    global next_id
    next_id += 1
    p = dict(id=next_id, title=title, type=kind, gridPos=dict(x=x, y=y, w=w, h=h), datasource=DS,
             description=description, fieldConfig=dict(defaults=dict(unit=unit, decimals=2,
             color=dict(mode='palette-classic'), custom=dict(drawStyle='line', lineInterpolation='smooth',
             lineWidth=2, fillOpacity=8, showPoints='never', axisBorderShow=False,
             axisLabel='', axisPlacement='auto', spanNulls=False)), overrides=[]),
             options=dict(legend=dict(displayMode='table', placement='bottom', calcs=['lastNotNull', 'max']),
                          tooltip=dict(mode='multi', sort='desc')))
    if expressions:
        p['targets'] = [dict(refId=chr(65 + i), expr=expr, legendFormat=legend, datasource=DS,
                             range=kind == 'timeseries', instant=kind != 'timeseries', format='table' if kind == 'table' else 'time_series')
                        for i, (expr, legend) in enumerate(expressions)]
    if kind == 'stat':
        if unit == 'short':
            p['fieldConfig']['defaults']['decimals'] = 0
        p['options'] = dict(reduceOptions=dict(calcs=['lastNotNull'], fields='', values=False),
                            orientation='auto', textMode='value_and_name', colorMode='value',
                            graphMode='none', justifyMode='auto')
        p['fieldConfig']['defaults']['thresholds'] = dict(mode='absolute', steps=[dict(color='green', value=None)])
    if kind == 'table':
        p['fieldConfig']['defaults']['custom'] = dict(minWidth=60, cellOptions=dict(type='auto'), filterable=False)
        if unit == 'short':
            p['fieldConfig']['defaults']['decimals'] = 0
        p['options'] = dict(showHeader=True, cellHeight='sm', sortBy=[dict(displayName='Value', desc=True)])
        p['transformations'] = [dict(id='organize', options=dict(excludeByName={'Time': True, '__name__': True, 'job': True},
                    indexByName={'instance': 0, 'service': 1, 'source_ip': 2, 'protocol': 2, 'inbound': 3,
                                 'network': 4, 'outbound': 5, 'direction': 3, 'port': 4, 'Value': 6},
                    renameByName={'instance': 'Host', 'service': 'Service', 'protocol': 'Protocol', 'inbound': 'Inbound',
                                  'network': 'Network', 'outbound': 'Egress', 'source_ip': 'Source IP', 'port': 'Port', 'Value': 'Value'}))]
        widths = {'Host': 60, 'Service': 110, 'Source IP': 175, 'Protocol': 115, 'Inbound': 140,
                  'Network': 85, 'Egress': 100, 'direction': 85, 'Value': 100, 'Port': 70}
        value_name = 'Active' if title in ('Active routing paths', 'Source connections') else ('Uptime' if title == 'Service uptime' else ('Bytes' if title == 'Source active-connection bytes' else 'Value'))
        p['transformations'][0]['options']['renameByName']['Value'] = value_name
        widths[value_name] = 80 if value_name == 'Active' else 100
        p['options']['sortBy'][0]['displayName'] = value_name
        p['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options=name), properties=[dict(id='custom.width', value=width)]) for name, width in widths.items()]
        p['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options='Port'), properties=[dict(id='unit', value='none'), dict(id='decimals', value=0)]))
        if title == 'Configured protocol listeners':
            p['transformations'][0]['options']['excludeByName'].update(service=True, inbound=True, Value=True)
    p.update(extra)
    panels.append(p)
    return p


def row(title, y):
    return panel(title, 'row', 0, y, 24, 1, collapsed=False, panels=[])


row('Fleet overview', 0)
panel('Services', 'stat', 0, 1, 4, 4,
      [(f'sum(singbox_up{{{HOST}}})', 'Healthy'), (f'count(singbox_up{{{HOST}}})', 'Configured')],
      description='Every configured sing-box service must have a fresh successful API snapshot. Zero is a failure, not idle traffic.')
CONNECTION_DESCRIPTION = ('Connection view applies here and to Source connections: Recent traffic counts each '
    'routed session only while its last observed payload byte change is within five minutes. '
    'All open sessions shows the full current API inventory, including idle logical sessions. '
    'Idle is not proof of disconnection; no sessions are terminated. Historical points retain their value at that time.')
panel('Connections', 'stat', 4, 1, 4, 4, [(f'sum(singbox_route_${{source_activity}}{{{ROUTE}}})', 'Active')],
      description=CONNECTION_DESCRIPTION + ' Host, Service and Protocol filters apply; DNS outbound excluded.')
panel('Upload', 'stat', 8, 1, 4, 4, [(f'sum(rate(singbox_traffic_bytes_total{{{HOST},direction="upload"}}[$__rate_interval]))', 'Client → destination')], 'Bps')
panel('Download', 'stat', 12, 1, 4, 4, [(f'sum(rate(singbox_traffic_bytes_total{{{HOST},direction="download"}}[$__rate_interval]))', 'Destination → client')], 'Bps')
panel('Period traffic', 'stat', 16, 1, 4, 4, [(f'sum(increase(singbox_traffic_bytes_total{{{HOST}}}[$__range]))', 'Payload')], 'bytes',
      description='Exact API total counters, reset-aware Prometheus increase. Sampling/extrapolation at range edges; not network wire bytes.')
panel('Process RSS', 'stat', 20, 1, 4, 4, [(f'sum(singbox_process_resident_memory_bytes{{{HOST}}})', 'Resident memory')], 'bytes')
panel('Service availability', 'timeseries', 0, 5, 8, 7, [(f'singbox_up{{{HOST}}}', '{{instance}} / {{service}}')],
      description='Fresh API availability; missing exporter scrapes are gaps, not zero.',
      fieldConfig=dict(defaults=dict(unit='short', min=0, max=1, color=dict(mode='palette-classic'),
            custom=dict(drawStyle='line', lineInterpolation='stepAfter', lineWidth=2, fillOpacity=15, spanNulls=False)), overrides=[]))
panel('Payload bandwidth by host', 'timeseries', 8, 5, 16, 7,
      [(f'sum by(instance,direction)(rate(singbox_traffic_bytes_total{{{HOST}}}[$__rate_interval]))', '{{instance}} · {{direction}}')], 'Bps',
      description='Exact service counters. Includes connections that begin and end between polls. Upload and download are both positive.')
row('Protocols, connections and routing', 12)
panel('Active connections by protocol', 'timeseries', 0, 13, 12, 8,
      [(f'sum by(instance,protocol)(singbox_route_${{source_activity}}{{{ROUTE}}})', '{{instance}} · {{protocol}}')],
      description=CONNECTION_DESCRIPTION)
panel('TCP / UDP / ICMP sessions', 'timeseries', 12, 13, 12, 8,
      [(f'sum by(network)(singbox_route_${{source_activity}}{{{ROUTE}}})', '{{network}}')],
      description=CONNECTION_DESCRIPTION)
for connection_panel in panels[-2:]:
    # Ended series must not display their last historical value as current.
    connection_panel['options']['legend']['calcs'] = ['last', 'max']
panel('Observed protocol bandwidth · sampled', 'timeseries', 0, 21, 12, 8,
      [(f'sum by(instance,protocol,direction)(rate(singbox_route_observed_bytes_total{{{ROUTE}}}[$__rate_interval]))', '{{instance}} · {{protocol}} · {{direction}}')], 'Bps',
      description='Lower-bound breakdown; API sampling interval is configurable per host. Short connections and traffic after their last sample are not attributed; use fleet totals for total traffic.')
panel('Observed new connections · sampled', 'timeseries', 12, 21, 12, 8,
      [(f'sum by(instance,protocol)(rate(singbox_route_observed_connections_total{{{ROUTE}}}[$__rate_interval]))', '{{instance}} · {{protocol}}')], 'cps',
      description='New sessions observed after first snapshot. Not exact connection acceptance or failed authentication counts.')
panel('Active routing paths', 'table', 0, 29, 16, 8,
      [(f'singbox_route_${{source_activity}}{{{ROUTE}}} > 0', '')], description=CONNECTION_DESCRIPTION + ' Protocol/inbound/network/egress intersections.')
panel('Configured protocol listeners', 'table', 16, 29, 8, 8,
      [(f'singbox_inbound_info{{{ROUTE}}}', '')], description='Inventory includes idle listeners. API collection health is shown above; inventory is not a synthetic connectivity probe.')
row('Source clients', 37)
SOURCE_KEY = 'instance,service,protocol,source_ip'
source_active = f'sum by({SOURCE_KEY})(singbox_source_protocol_${{source_activity}}{{{ROUTE}}}) > 0'
source_rtt = f'(sum by({SOURCE_KEY})(singbox_tcp_source_rtt_sum{{{ROUTE}}}) / sum by({SOURCE_KEY})(singbox_tcp_source_rtt_count{{{ROUTE}}})) > 0'
source_quic = f'sum by({SOURCE_KEY})(singbox_source_protocol_${{source_activity}}{{{ROUTE},protocol=~"hysteria|hysteria2|tuic"}}) > 0'
source_without_rtt = f'({source_active}) unless on({SOURCE_KEY}) ({source_rtt})'
source_quic_without_rtt = f'({source_without_rtt}) and on({SOURCE_KEY}) ({source_quic})'
# -1 is a dashboard-only categorical marker mapped to N/A, not a measured RTT.
# TCP-capable protocols without a sample remain null and display "No sample".
source_rtt_display = f'({source_rtt}) or (({source_quic_without_rtt}) * 0 - 1)'
# Match raw identities before optional enrichment. A TCP peer is not necessarily
# an authenticated/routed session; retain those peers, but sort them last.
source_state = (f'((({source_active}) and on({SOURCE_KEY}) ({source_rtt})) * 0 + 1) or '
                f'(({source_quic_without_rtt}) * 0 + 2) or '
                f'((({source_without_rtt}) unless on({SOURCE_KEY}) ({source_quic})) * 0 + 3) or '
                f'((({source_rtt}) unless on({SOURCE_KEY}) ({source_active})) * 0 + 4)')
source_panel = panel('Source connections', 'table', 0, 38, 19, 8,
      [(enrich(source_active), ''), (enrich(source_rtt_display), ''), (enrich(source_state), '')],
      description='Source view defaults to Recent traffic: routed sessions expire individually after five minutes without observed payload byte changes. Counts include only those recent sessions; All open sessions includes idle API sessions. A collector restart uses session start time until new byte changes are observed, so old or unknown sessions do not become recent just because the exporter restarted. This is display expiry, not a request to terminate proxy sessions. Current API-reported routed sessions and physical TCP peers with recent payload and ACK activity by host/service/protocol/source IP, without an IP-count cap. Default order: Active + RTT, Active / QUIC, API active / no RTT, then TCP peer only; active counts descend within each group. Hysteria/Hysteria2/TUIC use QUIC, so this TCP collector cannot measure their client transport RTT: Mean RTT is N/A. API active / no RTT means the API reports routed sessions but no recent physical TCP RTT sample exists: Mean RTT is No sample. Possible reasons include Shadowsocks UDP sessions, idle TCP (last payload or ACK older than 120s by default), retained logical API sessions without a matching established TCP socket, or unavailable/unsupported collector data. It does not by itself prove disconnection; do not equate API sessions with physical TCP sockets. TCP peer only means recent payload and RTT but no corresponding active routed session, not proof of authentication or proxy use. Keepalive-only sockets expire from this table once payload has been idle for 120s by default; they remain in fleet-level socket diagnostics. Missing Active shows a dash, not a fabricated count. RTT is socket-weighted and pooled across listeners of the same protocol. Optional IPinfo flag/Organization enrichment never drops unattributed peers. All three global filters apply; NAT/relay IPs are not individual users, and country is peer-IP attribution, not verified residence.')
source_panel['transformations'].insert(0, dict(id='merge', options={}))
source_options = source_panel['transformations'][1]['options']
source_options['renameByName'].update({'source_ip_display': 'Source IP', 'as_name': 'Organization', 'Value #A': 'Active', 'Value #B': 'Mean RTT', 'Value #C': 'State'})
source_options['excludeByName'].update(asn=True, country_code=True, source_ip=True)
source_options['indexByName'] = {'instance': 0, 'service': 1, 'protocol': 2, 'source_ip_display': 3, 'as_name': 4, 'Value #C': 5, 'Value #A': 6, 'Value #B': 7}
source_panel['options']['sortBy'] = [dict(displayName='State', desc=False), dict(displayName='Active', desc=True)]
source_panel['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options=name), properties=[dict(id='custom.width', value=width)])
    for name, width in {'Host': 45, 'Service': 95, 'Protocol': 105, 'Source IP': 165, 'Organization': 300, 'State': 170, 'Active': 75, 'Mean RTT': 100}.items()]
source_panel['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options='Organization'), properties=[dict(id='custom.wrapText', value=True)]))
source_panel['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options='Mean RTT'), properties=[dict(id='unit', value='s'), dict(id='decimals', value=2), dict(id='noValue', value='No sample'), dict(id='mappings', value=[dict(type='value', options={'-1': dict(text='N/A')})])]))
source_panel['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options='Active'), properties=[dict(id='noValue', value='—')]))
source_panel['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options='State'), properties=[dict(id='unit', value='none'), dict(id='mappings', value=[dict(type='value', options={
    '1': dict(text='Active + RTT'), '2': dict(text='Active / QUIC'), '3': dict(text='API active / no RTT'), '4': dict(text='TCP peer only')})])]))
panel('Observed source bandwidth · sampled', 'timeseries', 12, 46, 12, 8,
      [(f'sum by(instance,source_ip,direction)(rate(singbox_source_observed_bytes_total{{{HOST}}}[$__rate_interval]))', '{{instance}} · {{source_ip}} · {{direction}}')], 'Bps',
      description='All sampled client sources over the selected time range, including disconnected clients. No source-IP count cap. No connection IDs, domains, destination IPs or ephemeral ports are stored in Prometheus.')
source_bytes = panel('Source active-connection bytes', 'table', 0, 46, 12, 8,
      [(enrich(f'sum by(instance,source_ip)(singbox_source_active_bytes{{{HOST},direction="upload"}})'), ''),
       (enrich(f'sum by(instance,source_ip)(singbox_source_active_bytes{{{HOST},direction="download"}})'), '')], 'bytes',
      description='One row per host/source IP, with Upload and Download columns. Lifetime bytes of currently active connections; these gauges can decrease when connections close. Upload is client to destination, Download is destination to client. The same IP on different hosts stays separate.')
source_bytes['transformations'].insert(0, dict(id='merge', options={}))
byte_options = source_bytes['transformations'][1]['options']
byte_options['renameByName'].update({'source_ip_display': 'Source IP', 'Value #A': 'Upload', 'Value #B': 'Download'})
byte_options['excludeByName'].update(asn=True, as_name=True, direction=True, country_code=True, source_ip=True)
byte_options['indexByName'] = dict(instance=0, source_ip_display=1, **{'Value #A': 2, 'Value #B': 3})
source_bytes['options']['sortBy'] = [dict(displayName='Download', desc=True)]
source_bytes['fieldConfig']['overrides'].extend([
    dict(matcher=dict(id='byName', options=name), properties=[dict(id='custom.width', value=115)])
    for name in ('Upload', 'Download')])
source_ips_panel = panel('Source IP counts', 'timeseries', 19, 38, 5, 8,
      [('singbox_exporter_source_ips_active{instance=~"$host"}', '{{instance}} · Routed IPs'),
       ('singbox_exporter_source_ips_tracked{instance=~"$host"}', '{{instance}} · Retained IPs')],
      description='Routed IPs: distinct source IP labels in the latest sing-box API snapshots, deduplicated across services on each host. Retained IPs: distinct source IP labels whose sampled traffic counters are still retained, including active IPs and inactive IPs until the configured idle TTL expires (one hour by default). These are IP counts, not connection counts, tracking slots, all physical TCP peers, or individual users. RTT-only peers in Source connections are not counted as routed IPs. No source-IP count limit or overflow bucket. Only the Host filter applies; Service and Protocol filters do not change these host-level counts.')
row('Client transport latency · TCP', 54)
panel('TCP RTT · socket-weighted mean', 'timeseries', 0, 55, 12, 8,
      [(f'sum by(instance,protocol)(singbox_tcp_client_rtt_snapshot_sum{{{ROUTE}}}) / sum by(instance,protocol)(singbox_tcp_client_rtt_snapshot_count{{{ROUTE}}})', '{{instance}} · {{protocol}}')], 's',
      description='Passive Linux smoothed ACK RTT, restricted to authenticated routed TCP sessions matched by exact inbound/source IP/source port, with payload and ACK activity within 120s. Keepalive-only and unauthenticated peers are excluded. Not one-way or website latency. UDP/QUIC has no sample; absent is a gap, not zero. Raw singbox_tcp_rtt_* metrics remain available for transport diagnostics.')
panel('TCP RTT · current p95 / maximum', 'timeseries', 12, 55, 12, 8,
      [(f'histogram_quantile(0.95,sum by(instance,protocol,le)(singbox_tcp_client_rtt_snapshot_bucket{{{ROUTE}}}))', '{{instance}} · {{protocol}} p95'),
       (f'max by(instance,protocol)(singbox_tcp_client_rtt_max_seconds{{{ROUTE}}})', '{{instance}} · {{protocol}} max')], 's',
      description='p95 is a bucket-based estimate across CURRENT physical TCP sockets, not the 95th percentile of all packets or past connections. Maximum is exact for the observed SRTTs. No rate() is used on these snapshot gauges.')
panel('TCP RTT sample coverage', 'timeseries', 0, 63, 8, 7,
      [(f'sum by(instance,protocol)(singbox_tcp_client_rtt_snapshot_count{{{ROUTE}}})', '{{instance}} · {{protocol}} authenticated recent RTT'),
       (f'sum by(instance,protocol)(singbox_tcp_established_sockets{{{ROUTE}}})', '{{instance}} · {{protocol}} established')],
      description='Recent RTT counts authenticated, exact API-matched TCP sockets with recent payload and ACK. Established includes all physical listener sockets, including idle/pre-authentication peers. A gap does not indicate packet loss.')
panel('TCP sample last-ACK age', 'timeseries', 8, 63, 8, 7,
      [(f'max by(instance,protocol)(singbox_tcp_client_rtt_ack_age_max_seconds{{{ROUTE}}})', '{{instance}} · {{protocol}}')], 's',
      description='Oldest last-ACK age among included RTT samples. Passive sampling reads existing kernel estimates; it cannot refresh an idle connection without new traffic.')
panel('TCP collector availability', 'timeseries', 16, 63, 8, 7,
      [('singbox_tcp_collector_up{instance=~"$host"}', '{{instance}}')],
      description='Fresh successful socket diagnostic snapshots. Independent of the Clash API; failures omit stale RTT. Collection runs without root, packet capture, pings or eBPF.',
      fieldConfig=dict(defaults=dict(unit='short', min=0, max=1, custom=dict(drawStyle='line', lineInterpolation='stepAfter', spanNulls=False)), overrides=[]))
resource_start = len(panels)
row('Service resources and collector health', 54)
panel('sing-box CPU', 'timeseries', 0, 55, 8, 7,
      [(f'rate(singbox_process_cpu_seconds_total{{{HOST}}}[$__rate_interval])*100', '{{instance}} / {{service}}')], 'percent',
      description='Percent of one CPU core. Process counter refresh interval is configurable per host.')
panel('RSS / Go memory', 'timeseries', 8, 55, 8, 7,
      [(f'singbox_process_resident_memory_bytes{{{HOST}}}', '{{instance}} / {{service}} RSS'),
       (f'singbox_memory_bytes{{{HOST}}}', '{{instance}} / {{service}} Go in-use')], 'bytes')
panel('Service uptime', 'table', 16, 55, 8, 7, [(f'singbox_process_uptime_seconds{{{HOST}}}', '')], 's')
panel('Exporter CPU', 'timeseries', 0, 62, 8, 7,
      [('rate(singbox_exporter_cpu_seconds_total{instance=~"$host"}[$__rate_interval])*100', '{{instance}}')], 'percent')
panel('Exporter peak RSS', 'timeseries', 8, 62, 8, 7,
      [('singbox_exporter_max_rss_bytes{instance=~"$host"}', '{{instance}}')], 'bytes')
panel('Snapshot age', 'timeseries', 16, 62, 8, 7,
      [(f'time()-singbox_last_success_timestamp_seconds{{{HOST}}}', '{{instance}} / {{service}}')], 's')
panel('Collection latency', 'timeseries', 0, 69, 8, 7,
      [(f'singbox_collection_duration_seconds{{{HOST}}}', '{{instance}} / {{service}}')], 's')
panel('Collection failures / API resets', 'timeseries', 8, 69, 8, 7,
      [(f'increase(singbox_collection_errors_total{{{HOST}}}[$__rate_interval])', '{{instance}} / {{service}} failures'),
       (f'increase(singbox_api_resets_total{{{HOST}}}[$__rate_interval])', '{{instance}} / {{service}} resets')])
panel('Prometheus scrape samples', 'timeseries', 16, 69, 8, 7,
      [('scrape_samples_scraped{job="sing-box",instance=~"$host"}', '{{instance}}')],
      description='Monitor series cost. Source scrape sample-count limits are disabled; growing distinct-IP history increases TSDB storage. Byte/time and host resource guards remain.')
for item in panels[resource_start:]:
    item['gridPos']['y'] += 27

# Live JSON is separate from Prometheus: no destination/connection-ID series.
live_start = len(panels)
live_row = row('Active connection destinations', 70)
live_row['id'] = 900
live_table = panel('Live destinations', 'table', 0, 71, 24, 10, description=
    'Current routed TCP/UDP sessions, grouped by host/service/source/protocol/network/domain/destination/egress. '
    'Domain is best effort from client metadata or HTTP/TLS/QUIC sniffing; blank means unknown. '
    'Upload/Download are lifetime bytes of currently active sessions; Oldest is their maximum age. '
    'The Destination view defaults to sessions with byte changes in the last 5 minutes; All open sessions includes idle ones. '
    'Idle is the longest time since an observed byte change in the group; collector restart resets the activity baseline conservatively. '
    'All global filters apply. This view ignores the historical time range and refreshes every 5s from cached snapshots. '
    'Short-lived sessions between polls can be missed; UDP destinations describe tracked session metadata. '
    'When some selected collectors are unavailable, their Status rows explicitly report Collector unavailable while healthy hosts remain visible. '
    'An entirely unavailable selection raises a query error. Health checks remain degraded until all selected collectors recover. '
    'Stale destinations are never reused. No history or destination metric labels.')
live_table['id'] = 901
live_ds = dict(type='yesoreyeram-infinity-datasource', uid='sing-box-connections')
live_table['datasource'] = live_ds
columns = [('host', 'Host', 'string'), ('status', 'Status', 'string'), ('service', 'Service', 'string'), ('source_ip_display', 'Source IP', 'string'),
           ('protocol', 'Protocol', 'string'), ('network', 'Network', 'string'), ('domain', 'Domain', 'string'),
           ('destination', 'Destination', 'string'), ('active', 'Active', 'number'),
           ('upload_bytes', 'Upload', 'number'), ('download_bytes', 'Download', 'number'),
           ('age_seconds', 'Oldest', 'number'), ('idle_seconds', 'Idle', 'number'), ('outbound', 'Egress', 'string')]
live_table['targets'] = [dict(refId='A', datasource=live_ds, type='json', source='url', parser='backend',
    format='table', url='/connections?${host:queryparam}&${service:queryparam}&${protocol:queryparam}&${activity:queryparam}',
    url_options=dict(method='GET'), root_selector='rows',
    columns=[dict(selector=key, text=title, type=kind) for key, title, kind in columns])]
live_table['transformations'] = [dict(id='organize', options=dict(indexByName={title: i for i, (_, title, _) in enumerate(columns)}))]
live_table['options']['sortBy'] = [dict(displayName='Status', desc=False), dict(displayName='Download', desc=True)]
live_table['fieldConfig']['defaults']['custom'].update(filterable=False)
live_table['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options=name),
    properties=[dict(id='custom.width', value=width)]) for name, width in
    {'Host': 70, 'Status': 155, 'Service': 105, 'Source IP': 165, 'Protocol': 110, 'Network': 75, 'Domain': 215,
     'Destination': 185, 'Active': 65, 'Upload': 100, 'Download': 100, 'Oldest': 85, 'Egress': 110}.items()]
for name in ('Source IP', 'Domain', 'Destination'):
    live_table['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options=name),
        properties=[dict(id='custom.filterable', value=True)]))
live_table['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options='Domain'),
    properties=[dict(id='custom.wrapText', value=True)]))
for name, unit in [('Upload', 'bytes'), ('Download', 'bytes'), ('Oldest', 's'), ('Idle', 's')]:
    live_table['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options=name),
        properties=[dict(id='unit', value=unit), dict(id='decimals', value=2)]))
panels[resource_start:resource_start] = panels[live_start:]
del panels[live_start + 2:]

# Reuse existing counters for an instant operational summary, not extra polling.
activity = panel('Service activity', 'table', 0, 5, 8, 7,
    [(f'max by(instance,service)(singbox_${{source_activity}}{{{HOST}}})', ''),
     (f'sum by(instance,service)(rate(singbox_traffic_bytes_total{{{HOST},direction="upload"}}[$__rate_interval]))', ''),
     (f'sum by(instance,service)(rate(singbox_traffic_bytes_total{{{HOST},direction="download"}}[$__rate_interval]))', ''),
     (f'max by(instance,service)(singbox_process_uptime_seconds{{{HOST}}})', '')],
    description='One row per host/service at the selected dashboard end time. Active is current routed sessions; Upload/Download are exact payload counter rates averaged over $__rate_interval. Uptime is process age. Host and Service filters apply; protocol filter does not, matching fleet totals. Failed API snapshots omit traffic/connection values rather than reporting zero; missing values stay blank. This is activity, not an end-to-end connectivity test.')
activity['id'] = 902
activity['transformations'] = [dict(id='merge', options={}), dict(id='organize', options=dict(
    excludeByName={'Time': True, '__name__': True, 'job': True},
    indexByName={'instance': 0, 'service': 1, 'Value #A': 2, 'Value #B': 3, 'Value #C': 4, 'Value #D': 5},
    renameByName={'instance': 'Host', 'service': 'Service', 'Value #A': 'Active',
                  'Value #B': 'Upload', 'Value #C': 'Download', 'Value #D': 'Uptime'}))]
activity['options']['sortBy'] = [dict(displayName='Active', desc=True)]
activity['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options=name),
    properties=[dict(id='custom.width', value=width), dict(id='unit', value=unit), dict(id='decimals', value=decimals)])
    for name, width, unit, decimals in [('Host', 60, 'none', 0), ('Service', 100, 'none', 0),
        ('Active', 80, 'short', 0), ('Upload', 85, 'Bps', 1), ('Download', 100, 'Bps', 1), ('Uptime', 85, 's', 1)]]
panels.pop()  # update_layout inserts the table at the original overview slot.
from dashboard_layout import update_layout
panels = update_layout(dict(panels=panels), activity)['panels']

# Optional central probes and passive TCP diagnostics; no new core polling.
network_start = len(panels)
probe_row = row('End-to-end proxy checks', 0)
probe_row['id'] = 1000
probe_table = panel('Latest proxy checks', 'table', 0, 1, 24, 10,
    description='One row per host/protocol, with separate Cloudflare and ChatGPT results. Host/Service/Protocol filters apply before grouping. For multiple selected listeners/vantages, status is their worst result, time is their slowest successful request, and age is the oldest result. Any stale/pending contributor makes that endpoint unavailable; failed endpoints omit successful-response time. HTTP is blank for mixed codes. Runs every 60s centrally, independently of Grafana refresh. Proves the configured HTTPS paths, not every destination, browser login or arbitrary UDP applications. Not client TCP/QUIC RTT.')
probe_table['id'] = 1001
probe_expressions = []
probe_columns = [('instance', 'Host', 70, 'none', 0), ('protocol', 'Protocol', 110, 'none', 0)]
for check, title in [('cloudflare', 'Cloudflare'), ('chatgpt', 'ChatGPT')]:
    selector = f'{ROUTE},probe="{check}"'
    fresh = f'(min by(instance,protocol)(singbox_probe_result_fresh{{{selector}}}) == 1)'
    passed = f'(min by(instance,protocol)(singbox_probe_success{{{selector}}}) == 1)'
    probe_expressions.extend([
        f'min by(instance,protocol)(singbox_probe_success{{{selector}}}) and on(instance,protocol) {fresh}',
        f'max by(instance,protocol)(singbox_probe_duration_seconds{{{selector}}}) and on(instance,protocol) {passed} and on(instance,protocol) {fresh}',
        f'(min by(instance,protocol)(singbox_probe_http_status_code{{{selector}}}) == on(instance,protocol) max by(instance,protocol)(singbox_probe_http_status_code{{{selector}}})) and on(instance,protocol) {fresh}',
        f'time()-min by(instance,protocol)(singbox_probe_last_run_timestamp_seconds{{{selector}}})'])
    for label, width, unit, decimals in [(title, 95, 'short', 0), (title + ' time', 140, 's', 2),
                                        (title + ' HTTP', 125, 'none', 0), (title + ' age', 115, 's', 1)]:
        ref = chr(ord('A') + len(probe_columns) - 2)
        probe_columns.append(('Value #' + ref, label, width, unit, decimals))
# Keep pending/stale configured rows visible, without treating missing as success.
probe_expressions.append(f'min by(instance,protocol)(singbox_probe_result_fresh{{{ROUTE},probe=~"cloudflare|chatgpt"}})')
probe_table['targets'] = [dict(refId=chr(ord('A') + i), expr=expr, legendFormat='',
    instant=True, range=False, format='table', datasource=DS) for i, expr in enumerate(probe_expressions)]
probe_table['transformations'] = [dict(id='merge', options={}), dict(id='organize', options=dict(
    excludeByName={'Time': True, '__name__': True, 'job': True, 'Value #I': True},
    indexByName={field: i for i, (field, *_) in enumerate(probe_columns)},
    renameByName={field: label for field, label, *_ in probe_columns}))]
probe_table['options']['sortBy'] = [dict(displayName='Cloudflare', desc=False), dict(displayName='ChatGPT', desc=False)]
probe_table['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options=name),
    properties=[dict(id='custom.width', value=width), dict(id='unit', value=unit), dict(id='decimals', value=decimals)])
    for _, name, width, unit, decimals in probe_columns]
for name in ('Cloudflare', 'ChatGPT'):
    probe_table['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options=name), properties=[
        dict(id='mappings', value=[dict(type='value', options={'0': dict(text='Failed', color='red'), '1': dict(text='OK', color='green')}),
                                 dict(type='special', options=dict(match='null', result=dict(text='Unavailable', color='gray')))]),
        dict(id='noValue', value='Unavailable'), dict(id='custom.cellOptions', value=dict(type='color-text'))]))
success = panel('End-to-end proxy success', 'timeseries', 0, 11, 12, 8,
    [(f'singbox_probe_success{{{ROUTE}}}', '{{instance}} · {{protocol}} · {{probe}}')],
    description='Latest scheduled HTTP check: 1 passed, 0 failed. Freshness gaps mean unavailable results, not successful checks. Each result may repeat over multiple scrapes; not a per-request success ratio.',
    fieldConfig=dict(defaults=dict(unit='short', min=0, max=1,
        custom=dict(drawStyle='line', lineInterpolation='stepAfter', spanNulls=False, fillOpacity=8)), overrides=[]))
success['id'] = 1002
response = panel('End-to-end HTTPS response time', 'timeseries', 12, 11, 12, 8,
    [(f'singbox_probe_duration_seconds{{{ROUTE}}}', '{{instance}} · {{protocol}} · {{probe}}')], 's',
    description='Successful curl request duration through a real proxy client, including connection setup, DNS on the configured proxy path, TLS and the small HTTPS response. Central client process startup is excluded. Failed attempts have no successful-response duration; see success/HTTP/Age. Not client transport RTT or a browser/application benchmark.')
response['id'] = 1003
retrans = panel('TCP retransmissions · observed rate', 'timeseries', 0, 0, 8, 7,
    [(f'sum by(instance,protocol)(rate(singbox_tcp_observed_retransmissions_total{{{ROUTE}}}[$__rate_interval]))', '{{instance}} · {{protocol}}')], 'suffix: retrans/s',
    description='Passive retransmission increments for client-facing established TCP sockets seen in consecutive snapshots. First observation is baseline; short-lived sockets and bytes/events after the last sample can be missed. Not a packet-loss percentage or exact lifetime count. No UDP/QUIC coverage.')
retrans['id'] = 1004
cwnd = panel('TCP congestion window · per-socket mean', 'timeseries', 8, 0, 8, 7,
    [(f'sum by(instance,protocol)(singbox_tcp_cwnd_bytes_sum{{{ROUTE}}}) / sum by(instance,protocol)(singbox_tcp_cwnd_socket_count{{{ROUTE}}})', '{{instance}} · {{protocol}}')], 'bytes',
    description='Mean server send congestion window across CURRENT supported physical TCP sockets: Linux cwnd segments × send MSS. Includes idle/pre-authentication sockets. Capacity, not queued traffic, throughput or available bandwidth. Empty/unsupported is blank, not zero.')
cwnd['id'] = 1005
pending = panel('TCP pending bytes', 'timeseries', 16, 0, 8, 7,
    [(f'sum by(instance,protocol)(singbox_tcp_send_queue_bytes{{{ROUTE}}})', '{{instance}} · {{protocol}} send queue'),
     (f'sum by(instance,protocol)(singbox_tcp_receive_queue_bytes{{{ROUTE}}})', '{{instance}} · {{protocol}} receive queue'),
     (f'sum by(instance,protocol)(singbox_tcp_notsent_bytes{{{ROUTE}}})', '{{instance}} · {{protocol}} unsent')], 'bytes',
    description='Current client-facing established TCP queues from Linux SOCK_DIAG. Send queue includes unacknowledged + unsent bytes; unsent is a SUBSET, do not add them. Receive queue waits for sing-box to read. Server perspective, includes idle sockets. Optional unsent fields are omitted on unsupported kernels.')
pending['id'] = 1006
network_templates = panels[network_start:]
del panels[network_start:]
from network_layout import update_network_layout
panels = update_network_layout(dict(panels=panels), network_templates)['panels']


def variable(name, label, query):
    return dict(name=name, label=label, type='query', datasource=DS, definition=query,
                query=dict(query=query, refId='StandardVariableQuery'), refresh=1, sort=1,
                multi=True, includeAll=True, allValue='.*', current=dict(text='All', value='$__all'), options=[])


dashboard = dict(uid='sing-box-fleet', title='sing-box monitor', tags=['sing-box', 'network', 'fleet'],
    timezone='browser', schemaVersion=39, version=1, editable=True, refresh='5s',
    time=dict(**{'from': 'now-1h', 'to': 'now'}), graphTooltip=1,
    links=[],
    templating=dict(list=[dict(name='datasource', label='Datasource', type='datasource', query='prometheus',
                       current=dict(text='Prometheus', value=''), options=[], refresh=1),
        variable('host', 'Host', 'label_values(singbox_up, instance)'),
        variable('service', 'Service', 'label_values(singbox_up{instance=~"$host"}, service)'),
        variable('protocol', 'Protocol (routes / clients / RTT)', 'label_values(singbox_inbound_info{instance=~"$host",service=~"$service"}, protocol)'),
        dict(name='source_activity', label='Connection view', type='custom',
             query='Recent traffic : recent_connections,All open sessions : connections',
             multi=False, includeAll=False, current=dict(text='Recent traffic', value='recent_connections'),
             options=[dict(text='Recent traffic', value='recent_connections', selected=True),
                      dict(text='All open sessions', value='connections', selected=False)]),
        dict(name='activity', label='Destination view', type='custom', query='Recent traffic : recent,All open sessions : all',
             multi=False, includeAll=False, current=dict(text='Recent traffic', value='recent'),
             options=[dict(text='Recent traffic', value='recent', selected=True),
                      dict(text='All open sessions', value='all', selected=False)])]),
    annotations=dict(list=[dict(name='Annotations & Alerts', type='dashboard', builtIn=1, enable=True, hide=True,
                               datasource=dict(type='grafana', uid='-- Grafana --'))]), panels=panels)

if __name__ == '__main__':
    destination = pathlib.Path(__file__).with_name('dashboard.json')
    destination.write_text(json.dumps(dashboard, indent=2) + '\n', encoding='utf-8')
    print(f'{destination}: {len(panels)} panels')
