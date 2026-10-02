# sing-box monitoring

A lightweight Prometheus exporter and portable Grafana dashboard for sing-box.
Python standard library only. No custom sing-box build, pip dependencies, packet
capture, eBPF, log scraping, database, or active background latency probes.

## Features

- Fleet traffic and availability, inbound protocol inventory, live connections,
  TCP/UDP session breakdowns, egress routing, and bounded source-IP activity.
- Passive client-facing **TCP RTT**: socket-weighted mean, current-socket p95,
  maximum, per-source mean, coverage, and last-ACK age.
  Source connections retains a compact two-column layout and shows Protocol,
  activity and mean RTT together; maximum RTT remains in the latency chart.
- Native process CPU/RSS/uptime, exporter health/resources, authenticated HTTPS,
  CA-pinned scrapes, and optional alert rules.
- Configurable sampling and resource limits per host. One exporter per server;
  central Prometheus/Grafana rather than a monitoring stack on each small VPS.
- A built-in-plugin-only Grafana dashboard with host, service, and protocol
  filters. No fixed machines, addresses, datasource UID, or external dashboard ID.

```text
sing-box servers ─ authenticated loopback Clash API ─ exporters ─ HTTPS/Bearer ─ Prometheus ─ Grafana
                         Linux SOCK_DIAG TCP_INFO ────────┘
```

## Requirements and compatibility

Exporter: Linux, Python 3.11+, sing-box built with Clash API, and access to the
same network namespace for optional TCP RTT. API routing metrics work with any
protocol that the installed sing-box version tracks; inventory is supplied in
the exporter config, not limited to a hardcoded protocol list.

Native installer: Debian/Ubuntu-style Linux with systemd, `sing-box`, `openssl`,
and `nft`. It supports a **single JSON configuration file per service**. Supply
custom unit names and config paths explicitly. Multi-file/JSONC configurations,
non-systemd hosts, containerized proxy cores, and other distributions should use
the documented manual exporter configuration rather than pretending the native
installer can safely discover every layout. Monitoring/firewall installer
addresses currently use IPv4; TCP diagnostics support IPv4 and IPv6 clients.

Tested with sing-box 1.14.2, Python 3.13, Debian 13, x86_64 and ARM64. No changes
to proxy routing, users, certificates or protocol settings are required beyond
enabling an authenticated loopback Clash API. First-time API changes briefly
restart that proxy service; subsequent installs with unchanged APIs do not.

## Quick start: native exporter

Copy this repository to the server. Inspect its current proxy configuration,
then substitute your actual monitoring and server IPs:

```sh
sudo python3 install.py \
  --monitor-ip 192.0.2.20 --certificate-ip 192.0.2.10 \
  --source-mode subnet --tcp-rtt
```

The default service discovery checks active `sing-box` and `sing-box-cn` units.
Custom names/paths do not require editing the source:

```sh
sudo python3 install.py \
  --monitor-ip 192.0.2.20 --certificate-ip 192.0.2.10 \
  --service proxy-east=/etc/proxy-east/server.json \
  --service proxy-west=/etc/proxy-west/server.json \
  --source-mode subnet --tcp-rtt
```

The examples use reserved documentation addresses, not real machines. The
installer creates `/opt/sing-box-exporter/{exporter,tcpdiag}.py`, protected
configuration/TLS files in `/etc/sing-box-exporter`, persistent source slots in
`/var/lib/sing-box-exporter`, and two enabled systemd units. Exporter port 9119 is
allowed only from loopback and the monitoring IP using a dedicated nftables
table; existing rules are not flushed. Existing stricter firewalls must also
permit the monitoring source. It refuses an existing non-loopback Clash API.

Choose sampling **per machine**, not globally:

| Profile | API polling | TCP RTT polling | Process polling | Suggested scrape | Exporter CPU cap |
| --- | --- | --- | --- | --- | --- |
| Conservative small VPS | 10s | 30s | 30s | 30s | 2% of one core |
| Responsive server | 2s | 2s | 5s | 5s | None, or your own cap |

```sh
# Restricted host: cap applies to exporter, not sing-box.
sudo python3 install.py --monitor-ip 192.0.2.20 --certificate-ip 192.0.2.10 \
  --tcp-rtt --poll-interval 10 --tcp-poll-interval 30 \
  --process-interval 30 --cpu-quota 2

# Unrestricted host: 0 explicitly removes the exporter CPU quota.
sudo python3 install.py --monitor-ip 192.0.2.20 --certificate-ip 192.0.2.10 \
  --tcp-rtt --poll-interval 2 --tcp-poll-interval 2 \
  --process-interval 5 --cpu-quota 0
```

Intervals accept 2..300 seconds. Faster polling improves session sampling but
does not recover sessions that start and finish between polls. RTT estimates
only change when the kernel receives relevant TCP acknowledgments.

## Connect Prometheus and Grafana

1. Securely copy **only** `server.crt` and the scrape token from the exporter
   config to your monitoring server. Never transfer the TLS private key or proxy
   credentials. Token files should be readable only by the Prometheus service.
2. Adapt `prometheus.example.yml`: target IP, CA/token paths, instance label and
   polling interval. Use one job per independent CA/token; relabel to
   `job="sing-box"` for the supplied dashboard/rules. Do not disable TLS checks.
3. Validate with your installed `promtool check config ...`, then reload
   Prometheus. Keep its web/API interface private now that metrics may contain IPs.
4. Import `dashboard.json` into Grafana and select your Prometheus datasource.
   All datasource, host, service and protocol choices are portable. Source-IP
   data should stay behind authenticated Grafana access.

Optional import helper (credentials stay in a protected environment or stdin):

```sh
GRAFANA_URL=http://127.0.0.1:3000 GRAFANA_USER=YOUR_USER \
  GRAFANA_PASSWORD=YOUR_PASSWORD python3 publish_dashboard.py
```

`publish_dashboard.py --credentials-stdin` accepts a JSON object with those three
keys. It resolves the actual Prometheus datasource automatically. Subsequent
imports overwrite only dashboard UID `sing-box-fleet`; export/review live edits
first. Anonymous access/public dashboard sharing is never enabled automatically.

`build_dashboard.py` regenerates the portable dashboard. It includes no fixed
links to dashboards that might not exist on someone else's Grafana.

## Manual / container deployment

For an existing Clash API or nonstandard layout, start from
`config.example.json`. Set independent random scrape/API tokens, existing
loopback API URLs, service identities, listener inventory and real TLS paths.
Keep config/private keys root- or exporter-readable only. Run:

```sh
python3 exporter.py --config /etc/sing-box-exporter/config.json
```

For TCP RTT, set `tcp_rtt: true`, listener `port`, `listen`, and optionally
`tcp_uid` (the proxy process's numeric UID) per service. Ownership/bind checks
prevent accidental attribution to a different listener. Shared ambiguous
listeners are omitted and counted by `singbox_tcp_unmatched_sockets`.

`Dockerfile` and `compose.yaml` provide an optional non-root, read-only, resource-
limited **exporter** container, not another Prometheus on each VPS. First prepare
the API/firewall/TLS settings, stop the native exporter without removing its
access firewall, and change TLS config paths to `/config/server.crt` and
`/config/server.key`. Set `EXPORTER_UID`/`EXPORTER_GID` to the native exporter
account's numeric IDs and keep the state directory owned by that account:

```sh
EXPORTER_UID=YOUR_UID EXPORTER_GID=YOUR_GID docker compose up -d --build
```

Linux host networking is required for loopback APIs and host TCP diagnostics.
No Docker socket, host `/proc`, systemd bus, capabilities, or privileged mode is
mounted/enabled. **Native process metrics are unavailable in this container**;
API metrics and host-network TCP RTT work. Do not run both agents on port 9119.
The default container CPU limit is 0.02 cores; adjust `EXPORTER_CPUS` per host.
The Python multi-architecture base image is pinned; review/update the pin for
security maintenance.

## Accuracy and metric semantics

| Metric family | Meaning |
| --- | --- |
| `singbox_up`, `collection_*`, `last_success_timestamp_seconds` | API success/freshness; failed/stale snapshots omit traffic |
| `singbox_traffic_bytes_total{direction}` | Exact API payload totals since core start, including short-lived sessions; excludes wire overhead |
| `singbox_connections`, `route_connections` | Current routed connections/UDP sessions, excluding DNS outbound; bounded route groups reconcile to total |
| `singbox_source_protocol_connections` | Current routed sessions by protocol and bounded source IP; reconciles to total, including explicit overflow |
| `singbox_route_active_bytes`, `source_active_bytes` | Lifetime bytes of **currently active** connections; gauges, not counters |
| `singbox_route_observed_bytes_total`, `source_observed_bytes_total` | Sampled lower bounds; misses short sessions and bytes after their final sample |
| `singbox_inbound_info`, `process_*`, `exporter_*` | Inventory, native process resources, collection/resource/TLS health |
| `singbox_tcp_rtt_snapshot_{bucket,sum,count}` | Current physical TCP socket RTT distribution; **all gauges** |
| `singbox_tcp_rtt_max_seconds`, `tcp_source_rtt_*` | Maximum and bounded per-peer RTT summaries |
| `singbox_tcp_established_sockets`, `tcp_ack_age_max_seconds`, `tcp_collector_*` | Socket coverage, ACK age and independent diagnostic health |

Upload means client → destination; download means destination → client. Fleet
totals count hop payloads, so relay traffic can appear on two servers. They are
not unique end-user traffic or provider billing. First API snapshots establish
a baseline; exporter restarts reset observed attribution counters. Prometheus
rates handle upstream total resets. Failed authentication/handshakes, exact
per-client closed-session accounting and packet loss are not exposed by this API.

### TCP RTT limitations

Linux reports **smoothed acknowledgment RTT**, not one-way latency, HTTP response
time, QUIC latency or application jitter. The collector passively reads
`NETLINK_SOCK_DIAG`/`TCP_INFO`, filtered by configured local listener ports;
outbound destination sockets are not included. It works without root or
`CAP_NET_ADMIN`. It includes established **pre-authentication** TCP sockets,
not only authenticated/routed sessions. Multiple logical proxy sessions can
share one TCP socket, while a NAT/relay IP can represent multiple clients.
Behind a TCP reverse proxy/load balancer, RTT measures the immediate transport
peer, not the original client beyond that extra hop. TCP delayed ACKs and kernel
smoothing can differ from ping/application echo measurements; do not label it as
raw instantaneous path latency.

Hysteria2/TUIC/UDP have **no TCP RTT**. Unsupported/idle latency is absent, never
fabricated as zero. A positive kernel RTT with a last-ACK age ≤120s is included;
`tcp_max_idle` can change that cutoff in manual config. An idle connection can
retain an old SRTT, so inspect ACK age and coverage before interpreting it.

Mean is weighted by current physical socket count. p95 is an interpolated
**current-socket** histogram estimate, not a time/packet percentile. Bucket
boundaries are 5, 10, 25, 50, 75, 100, 150, 250, 500 ms, 1, 2, 5 seconds and +Inf.
Use the snapshot buckets directly, **without `rate()`**:

```promql
sum by(instance,protocol)(singbox_tcp_rtt_snapshot_sum)
  / sum by(instance,protocol)(singbox_tcp_rtt_snapshot_count)

histogram_quantile(0.95,
  sum by(instance,protocol,le)(singbox_tcp_rtt_snapshot_bucket))
```

### Privacy, bounds and low-resource behavior

Source mode defaults to `off`; choose `subnet` (IPv4 /24, IPv6 /64) or opt into
`raw`. Default 32 persistent source slots, installer maximum 64; excess IPs
aggregate under `other`. No connection IDs, ephemeral ports, domains or
destination IPs become labels. Do not routinely reset slots: that expands
historical cardinality. Restrict access and retain data deliberately.

Collection is cached independently of scraping/UI refresh. API and TCP snapshots
have byte/socket budgets (8 MiB / 4096 by default); interrupted or oversized dumps
fail visibly rather than publishing truncated exact-looking data. TCP requests
are filtered by local port inside the kernel. Source/route dimensions are bounded.
Native default limits include 64 MiB memory, 16 tasks and 128 FDs, with configurable
CPU quota. Increasing inventory/cardinality requires reviewing Prometheus sample
limits and collector budgets. Resource usage is workload-dependent, not guaranteed.

## Tests and operations

```sh
python3 -m unittest -v
systemctl status sing-box-exporter
journalctl -u sing-box-exporter -n 30
```

Tests cover counters, route reconciliation, IP privacy/cardinality, RTT offsets,
IPv4/mapped IPv6, loopback TCP_INFO agreement, listener identity, stale/idle
samples and diagnostic budgets. `probe_rtt.py` optionally compares TCP_INFO,
`ss`, and a small bounded ICMP check on your own servers. Normal collection never
sends probes. `probe_proxy_rtt.py` provides an opt-in temporary authenticated echo
check using a temporary sing-box client; configuration containing proxy secrets
is accepted on stdin and removed on exit. This is not a throughput benchmark.

TLS certificates last 365 days. Renew before the expiry alert, retain valid SANs,
replace the pinned public certificate on Prometheus, restart/reload and verify.
Never bypass certificate checks. Rotate API/scrape tokens in both current configs
and restart affected agents. Alert rules are optional; notification delivery
requires your own Alertmanager. Provider-specific CPU policies belong in your
own alert labels/configuration, not hardcoded machines in this project.

## Safe removal

Stop/disable the exporter first. Remove only monitoring-added Clash API settings
from the **current** proxy config, validate with `sing-box check`, then restart
only the affected service. Pre-change root-only backups are under
`/var/backups/sing-box-monitoring`, never inside a `-C` config directory.
Do not restore whole old backups over newer routing or credentials.

Remove only this project's scrape/rule entries from your current Prometheus
configuration, validate/reload, then remove the exporter firewall unit/table and
runtime secrets/state when no longer needed. Existing services/rules/history are
not cleanup targets. This project never flushes a host firewall.

## License and upstream references

MIT. Independent project, not affiliated with SagerNet, Prometheus or Grafana.

- [sing-box Clash API](https://sing-box.sagernet.org/configuration/experimental/clash-api/)
- [sing-box connection snapshot source](https://github.com/SagerNet/sing-box/blob/af6e64c3b69e6132ebaee0e1a3d24e93903f6709/experimental/clashapi/connections.go)
- [Linux inet_diag ABI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/inet_diag.h)
- [Linux TCP_INFO ABI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/tcp.h)
- [ss RTT definition](https://man7.org/linux/man-pages/man8/ss.8.html)
