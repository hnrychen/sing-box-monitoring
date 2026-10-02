#!/usr/bin/env python3
"""Generate a portable Grafana dashboard. Uses only the built-in Prometheus plugin."""
import json
import pathlib

DS = {'type': 'prometheus', 'uid': '${datasource}'}
HOST = 'instance=~"$host",service=~"$service"'
ROUTE = HOST + ',protocol=~"$protocol"'
panels = []
next_id = 0


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
panel('Connections', 'stat', 4, 1, 4, 4, [(f'sum(singbox_connections{{{HOST}}})', 'Active')],
      description='Live routed connections and UDP sessions. DNS outbound excluded. Independent of protocol filter.')
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
      [(f'sum by(instance,protocol)(singbox_route_connections{{{ROUTE}}})', '{{instance}} · {{protocol}}')])
panel('TCP / UDP / ICMP sessions', 'timeseries', 12, 13, 12, 8,
      [(f'sum by(network)(singbox_route_connections{{{ROUTE}}})', '{{network}}')])
panel('Observed protocol bandwidth · sampled', 'timeseries', 0, 21, 12, 8,
      [(f'sum by(instance,protocol,direction)(rate(singbox_route_observed_bytes_total{{{ROUTE}}}[$__rate_interval]))', '{{instance}} · {{protocol}} · {{direction}}')], 'Bps',
      description='Lower-bound breakdown; API sampling interval is configurable per host. Short connections and traffic after their last sample are not attributed; use fleet totals for total traffic.')
panel('Observed new connections · sampled', 'timeseries', 12, 21, 12, 8,
      [(f'sum by(instance,protocol)(rate(singbox_route_observed_connections_total{{{ROUTE}}}[$__rate_interval]))', '{{instance}} · {{protocol}}')], 'cps',
      description='New sessions observed after first snapshot. Not exact connection acceptance or failed authentication counts.')
panel('Active routing paths', 'table', 0, 29, 16, 8,
      [(f'singbox_route_connections{{{ROUTE}}} > 0', '')], description='Protocol/inbound/network/egress intersections, not just separate filters.')
panel('Configured protocol listeners', 'table', 16, 29, 8, 8,
      [(f'singbox_inbound_info{{{ROUTE}}}', '')], description='Inventory includes idle listeners. API collection health is shown above; inventory is not a synthetic connectivity probe.')
row('Source clients · bounded labels', 37)
source_panel = panel('Source connections', 'table', 0, 38, 12, 8,
      [(f'sum by(instance,service,protocol,source_ip)(singbox_source_protocol_connections{{{ROUTE}}})', ''),
       (f'sum by(instance,service,protocol,source_ip)(singbox_tcp_source_rtt_sum{{{ROUTE}}}) / sum by(instance,service,protocol,source_ip)(singbox_tcp_source_rtt_count{{{ROUTE}}})', '')],
      description='Active routed sessions and socket-weighted mean physical TCP RTT by host/service/protocol/bounded source IP. TCP RTT is pooled across listeners of the same protocol; UDP/QUIC and idle/unsupported RTT are blank, not zero. Pre-auth TCP sockets can have RTT without a routed-session count. All three global filters apply. NAT/relay IPs are not individual users; maximum RTT remains in the latency chart.')
source_panel['transformations'].insert(0, dict(id='merge', options={}))
source_options = source_panel['transformations'][1]['options']
source_options['renameByName'].update({'Value #A': 'Active', 'Value #B': 'Mean RTT'})
source_options['indexByName'].update({'instance': 0, 'service': 1, 'protocol': 2, 'source_ip': 3, 'Value #A': 4, 'Value #B': 5})
source_panel['fieldConfig']['overrides'] = [dict(matcher=dict(id='byName', options=name), properties=[dict(id='custom.width', value=width)])
    for name, width in {'Host': 45, 'Service': 95, 'Protocol': 105, 'Source IP': 140, 'Active': 75, 'Mean RTT': 85}.items()]
source_panel['fieldConfig']['overrides'].append(dict(matcher=dict(id='byName', options='Mean RTT'), properties=[dict(id='unit', value='s'), dict(id='decimals', value=2)]))
panel('Observed source bandwidth · sampled', 'timeseries', 12, 38, 12, 8,
      [(f'topk(12,sum by(instance,source_ip,direction)(rate(singbox_source_observed_bytes_total{{{HOST}}}[$__rate_interval])))', '{{instance}} · {{source_ip}} · {{direction}}')], 'Bps',
      description='Top sampled client sources. No connection IDs, domains, destination IPs or ephemeral ports are stored in Prometheus.')
panel('Source active-connection bytes', 'table', 0, 46, 12, 8,
      [(f'sum by(instance,source_ip,direction)(singbox_source_active_bytes{{{HOST}}})', '')], 'bytes',
      description='Lifetime bytes of currently active connections; this is a gauge and can decrease when connections close.')
panel('Source label budget', 'timeseries', 12, 46, 12, 8,
      [('singbox_exporter_source_slots{instance=~"$host"}', '{{instance}} assigned'),
       ('singbox_exporter_source_slots_limit{instance=~"$host"}', '{{instance}} limit')],
      description='Default: 32 distinct IP labels per host, persisted permanently until explicitly reset. Further clients aggregate under other.')
row('Client transport latency · TCP', 54)
panel('TCP RTT · socket-weighted mean', 'timeseries', 0, 55, 12, 8,
      [(f'sum by(instance,protocol)(singbox_tcp_rtt_snapshot_sum{{{ROUTE}}}) / sum by(instance,protocol)(singbox_tcp_rtt_snapshot_count{{{ROUTE}}})', '{{instance}} · {{protocol}}')], 's',
      description='Passive Linux smoothed ACK round-trip time of physical client-facing TCP sockets. Not one-way or website latency. Includes pre-authentication sockets; excludes UDP/QUIC and samples whose last ACK is older than 120s by default. Idle/unsupported is no data, not zero.')
panel('TCP RTT · current p95 / maximum', 'timeseries', 12, 55, 12, 8,
      [(f'histogram_quantile(0.95,sum by(instance,protocol,le)(singbox_tcp_rtt_snapshot_bucket{{{ROUTE}}}))', '{{instance}} · {{protocol}} p95'),
       (f'max by(instance,protocol)(singbox_tcp_rtt_max_seconds{{{ROUTE}}})', '{{instance}} · {{protocol}} max')], 's',
      description='p95 is a bucket-based estimate across CURRENT physical TCP sockets, not the 95th percentile of all packets or past connections. Maximum is exact for the observed SRTTs. No rate() is used on these snapshot gauges.')
panel('TCP RTT sample coverage', 'timeseries', 0, 63, 8, 7,
      [(f'sum by(instance,protocol)(singbox_tcp_rtt_snapshot_count{{{ROUTE}}})', '{{instance}} · {{protocol}} recent RTT'),
       (f'sum by(instance,protocol)(singbox_tcp_established_sockets{{{ROUTE}}})', '{{instance}} · {{protocol}} established')],
      description='Counts physical TCP sockets, not Clash routed sessions. A lower recent-RTT count means missing initialization or last ACK older than the configured idle limit. It does not indicate packet loss.')
panel('TCP sample last-ACK age', 'timeseries', 8, 63, 8, 7,
      [(f'max by(instance,protocol)(singbox_tcp_ack_age_max_seconds{{{ROUTE}}})', '{{instance}} · {{protocol}}')], 's',
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
      description='Monitor series cost. Scrape intervals are configured per target; keep a bounded sample limit in Prometheus.')
for item in panels[resource_start:]:
    item['gridPos']['y'] += 16


def variable(name, label, query):
    return dict(name=name, label=label, type='query', datasource=DS, definition=query,
                query=dict(query=query, refId='StandardVariableQuery'), refresh=1, sort=1,
                multi=True, includeAll=True, allValue='.*', current=dict(text='All', value='$__all'), options=[])


dashboard = dict(uid='sing-box-fleet', title='sing-box · Fleet & connections', tags=['sing-box', 'network', 'fleet'],
    timezone='browser', schemaVersion=39, version=1, editable=True, refresh='10s',
    time=dict(**{'from': 'now-1h', 'to': 'now'}), graphTooltip=1,
    links=[],
    templating=dict(list=[dict(name='datasource', label='Datasource', type='datasource', query='prometheus',
                       current=dict(text='Prometheus', value=''), options=[], refresh=1),
        variable('host', 'Host', 'label_values(singbox_up, instance)'),
        variable('service', 'Service', 'label_values(singbox_up{instance=~"$host"}, service)'),
        variable('protocol', 'Protocol (routes / clients / RTT)', 'label_values(singbox_inbound_info{instance=~"$host",service=~"$service"}, protocol)')]),
    annotations=dict(list=[dict(name='Annotations & Alerts', type='dashboard', builtIn=1, enable=True, hide=True,
                               datasource=dict(type='grafana', uid='-- Grafana --'))]), panels=panels)

if __name__ == '__main__':
    destination = pathlib.Path(__file__).with_name('dashboard.json')
    destination.write_text(json.dumps(dashboard, indent=2) + '\n', encoding='utf-8')
    print(f'{destination}: {len(panels)} panels')
