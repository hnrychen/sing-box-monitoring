# sing-box monitoring

[![Latest release](https://img.shields.io/github/v/release/hnrychen/sing-box-monitoring?sort=semver)](https://github.com/hnrychen/sing-box-monitoring/releases/latest)
[![CI](https://github.com/hnrychen/sing-box-monitoring/actions/workflows/tests.yml/badge.svg)](https://github.com/hnrychen/sing-box-monitoring/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A Prometheus exporter and portable Grafana dashboard for monitoring sing-box
servers. It collects traffic, live sessions, routing, source-IP activity, and
optional passive TCP round-trip time (RTT). The exporter uses Python's standard
library and the sing-box Clash API; it does not require a custom sing-box build.

**Latest release:** [download the dashboard JSON](https://github.com/hnrychen/sing-box-monitoring/releases/latest/download/dashboard.json)
or browse the [release notes and source bundle](https://github.com/hnrychen/sing-box-monitoring/releases/latest).

## Contents

- [Features](#features)
- [Requirements and compatibility](#requirements-and-compatibility)
- [Quick start](#quick-start-native-exporter)
- [Connect Prometheus and Grafana](#connect-prometheus-and-grafana)
- [Manual and container deployment](#manual-and-container-deployment)
- [Unified operations guide](OPERATIONS.md)
- [Metric accuracy and privacy](#accuracy-and-metric-semantics)
- [Operations and removal](#tests-and-operations)
- [Optional IP enrichment](#optional-central-ipinfo-lookup)
- [License and references](#license-and-upstream-references)

## Features

- Optional central end-to-end HTTPS checks through real proxy clients, with
  success history, response-time charts and an operational results table.
- Passive TCP retransmission increments, congestion-window and pending-byte
  diagnostics using the existing socket poll, with no packet capture.
- [Unified native deployment and management](OPERATIONS.md) of exporter,
  IPinfo, live connections and probes through `sing-box-monitor`.
- Fleet traffic and availability, inbound protocol inventory, live connections,
  TCP/UDP session breakdowns, egress routing, and individual source-IP activity.
- Passive client-facing **TCP RTT**: socket-weighted mean, current-socket p95,
  maximum, per-source mean, coverage, and last-ACK age.
  Source connections retains a compact two-column layout and shows Protocol,
  activity and mean RTT together; maximum RTT remains in the latency chart.
- Optional **central IPinfo Lite enrichment**: country flags before source IPs
  in both tables, plus Organization (`as_name`) in Source connections. Proxy
  agents need no changes.
- Native process CPU/RSS/uptime, exporter health/resources, authenticated HTTPS,
  CA-pinned scrapes, and optional alert rules.
- Configurable sampling and resource limits per host. One exporter per server;
  central Prometheus/Grafana rather than a monitoring stack on each small VPS.
- A Grafana dashboard with host, service, and protocol filters. Core metric
  panels use built-in datasources and contain no fixed machines or addresses.
- Optional live destination table using Grafana Infinity: best-effort domain,
  destination IP/port, source, protocol, active count, bytes and age. Detailed
  records stay outside Prometheus and are not retained as browsing history.

```text
sing-box servers ─ authenticated loopback Clash API ─ exporters ─ HTTPS/Bearer ─ Prometheus ─ Grafana
                         Linux SOCK_DIAG TCP_INFO ────────┘
```

## Requirements and compatibility

| Component | Requirement |
| --- | --- |
| Exporter | Linux and Python 3.11 or newer |
| sing-box | A build with the Clash API enabled; routing metrics cover protocols tracked by that version |
| Native installer | Debian/Ubuntu-style system with systemd, `openssl`, and `nft` |
| Passive TCP RTT | Linux SOCK_DIAG access in the same network namespace as the sockets |

The native installer supports one JSON configuration file per service. For
multi-file or JSONC configs, non-systemd systems, containerized sing-box, or
other Linux distributions, use the manual exporter configuration. The installer
currently configures monitoring/firewall addresses over IPv4; TCP diagnostics
can report IPv4 and IPv6 clients.

Validated with sing-box 1.14.2, Python 3.13, Debian 13, x86_64, and ARM64. The
only sing-box configuration change is enabling an authenticated loopback Clash
API. The first install may briefly restart the proxy service to apply it; later
installs do not restart it when the API is already configured.

## Quick start: native exporter

Clone the repository on the monitored server. Review its current sing-box
configuration, then substitute the monitoring server's reachable IP and this
server's certificate IP:

```sh
git clone https://github.com/hnrychen/sing-box-monitoring.git
cd sing-box-monitoring

sudo python3 install.py \
  --monitor-ip 192.0.2.20 --certificate-ip 192.0.2.10 \
  --source-mode subnet --tcp-rtt
```

The example addresses are reserved for documentation; replace them before
running the command. See [Connect Prometheus and Grafana](#connect-prometheus-and-grafana)
to add the target and import the dashboard.

The default service discovery checks active `sing-box` and `sing-box-cn` units.
Custom names/paths do not require editing the source:

```sh
sudo python3 install.py \
  --monitor-ip 192.0.2.20 --certificate-ip 192.0.2.10 \
  --service proxy-east=/etc/proxy-east/server.json \
  --service proxy-west=/etc/proxy-west/server.json \
  --source-mode subnet --tcp-rtt
```

The installer places the exporter in `/opt/sing-box-exporter`, protected config
and TLS files under `/etc/sing-box-exporter`, and creates enabled systemd units.
Port 9119 is allowed from loopback and the configured monitoring IP through a
dedicated nftables table; existing firewall rules are not flushed. Existing
firewall policy must also permit the monitoring server. Installation stops for
review if a Clash API is already listening beyond loopback.

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

From the Prometheus host, verify the exporter endpoint using its pinned
certificate and scrape token:

```sh
curl --fail --show-error \
  --cacert /etc/prometheus/secrets/proxy-1.crt \
  -H "Authorization: Bearer $(cat /etc/prometheus/secrets/proxy-1.token)" \
  https://192.0.2.10:9119/metrics
```

Replace the example certificate, token, and target address with your configured
paths and exporter IP. A successful response contains Prometheus text-format
metrics, including `singbox_up`.

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

Fleet overview includes a per-service activity table (active sessions, payload
upload/download rates and uptime), using existing metrics without extra polling.
Service and TCP collector availability graphs are grouped in the resource/health
section. Host/Service filters apply to service totals; protocol filtering does not.

`build_dashboard.py` regenerates the portable dashboard. It includes no fixed
links to dashboards that might not exist on someone else's Grafana.

### Optional host flag prefixes

Create a private JSON mapping of raw host IDs to display labels, for example
`{"proxy-1": "🇸🇬 proxy-1"}`. The hostname must remain after the prefix.
Pass `--host-labels /path/to/host-labels.json` to `publish_dashboard.py` when
importing the portable dashboard. To update existing dashboards in place:

```sh
python3 host_labels.py --labels /path/to/host-labels.json \
  --uid YOUR_SING_BOX_UID --uid YOUR_NODE_EXPORTER_UID \
  --credentials-stdin --backup-dir /private/dashboard-backups
```

Supply Grafana credentials as the same JSON stdin object used by the publisher.
This preserves metric calculations, layout, filters, refresh and access. Friendly
selector text is separate from raw values; tables use value mappings and legends
use a display-only query-result label. Modified targets retain their base
expression/legend for repeatable updates. Stored Prometheus/exporter labels and history do not change.
Unknown hosts remain visible under their original names. Keep machine-specific
mapping files private; the generic dashboard does not hardcode host locations.

Windows Chrome/Edge can show country letters instead of emoji flags. The optional
Grafana image customization self-hosts a 78KB flag-only font. Ordinary text still
uses Inter, and code retains its own font. It does not affect exporters:

```sh
python3 fetch_flag_font.py
docker build -f grafana-flags.Dockerfile \
  --build-arg BASE_IMAGE=YOUR_EXISTING_PINNED_GRAFANA_IMAGE \
  -t grafana-country-flags:local .
```

Update only the Grafana image in your existing deployment, preserving all data
volumes, secrets, networks, ports and other settings. Rebuild this customization
when upgrading Grafana; it modifies the frontend template to load a separate,
cache-versioned stylesheet. No client extension or external font CDN is needed
after deployment. See `FLAG-FONT-LICENSE.md` for Twemoji/Mozilla/TalkJS attribution.

## Manual and container deployment

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
| `singbox_source_protocol_connections` | Current routed sessions by protocol and individual source IP; reconciles to total without IP overflow |
| `singbox_route_active_bytes`, `source_active_bytes` | Lifetime bytes of **currently active** connections; gauges, not counters |
| `singbox_route_observed_bytes_total`, `source_observed_bytes_total` | Sampled lower bounds; misses short sessions and bytes after their final sample |
| `singbox_inbound_info`, `process_*`, `exporter_*` | Inventory, native process resources, collection/resource/TLS health |
| `singbox_tcp_rtt_snapshot_{bucket,sum,count}` | Current physical TCP socket RTT distribution; **all gauges** |
| `singbox_tcp_rtt_max_seconds`, `tcp_source_rtt_*` | Maximum and per-peer RTT summaries without an IP-count cap |
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
`raw`. There is no source-IP count limit or source overflow bucket, including
protocol connection counts and TCP RTT. Legacy `max_sources`/`source_state`
configuration is ignored. Inactive sampled source counters expire after one hour
(`source_idle_ttl`, installer `--source-idle-ttl`); active sources are not evicted.
No connection IDs, ephemeral ports, domains or destination IPs become labels.
More distinct IPs increase historical TSDB cardinality: restrict access and retain
data deliberately. Old `other` history cannot be split back into individual IPs.

Upgrade existing agents by replacing `/opt/sing-box-exporter/exporter.py` with
the current file and restarting `sing-box-exporter.service`; preserve existing
config, secrets, TLS and CPU/memory settings. Old slot files can remain for
rollback but are no longer read or written. Update the central
`/opt/sing-box-ipinfo/ipinfo_exporter.py` and restart `sing-box-ipinfo.service`;
legacy `max_ips` is ignored. Change `sample_limit` to `0` on source scrape jobs,
remove `SingBoxSourceSlotsFull` from rules, validate using `promtool`, and reload
Prometheus. Republish the dashboard with the same UID and any existing host-label
mapping. No sing-box proxy restart or historic-data deletion is needed.

Collection is cached independently of scraping/UI refresh. API and TCP snapshots
have byte/socket budgets (8 MiB / 4096 by default); interrupted or oversized dumps
fail visibly rather than publishing truncated exact-looking data. TCP requests
are filtered by local port inside the kernel. Route dimensions remain bounded;
source-IP counts are not. Scrape sample-count limits are disabled for source jobs.
Native default limits include 64 MiB memory, 16 tasks and 128 FDs, with configurable
CPU quota. Increasing inventory/cardinality requires reviewing Prometheus sample
limits and collector budgets. Resource usage is workload-dependent, not guaranteed.

## Optional live destination table

The dashboard includes a **Live destinations** table between TCP latency and
service resources. It requires the signed `yesoreyeram-infinity-datasource`
plugin (tested with 4.0.0) and the optional central `connections_view.py` worker.
For metric-only installations, remove panels 900/901; no Infinity plugin is then
needed. The live view ignores the historical time range, uses the same global
selectors, and refreshes every 5s. Rows group source and destination; byte totals
belong only to currently active sessions and can decrease when sessions close.

1. Opt each exporter into `install.py --connection-details`, or set
   `"connection_details": true` in its current config and restart the exporter.
   Authenticated HTTPS `/connections` reuses cached API snapshots and the same
   source privacy mode. No additional core polling or Prometheus labels are added.
2. On the monitoring machine, edit `connections.example.json` with each exporter's
   host label, HTTPS URL, pinned CA file and bearer token file. Bind the worker to
   loopback or an explicitly selected private Docker gateway reachable by Grafana.
   Run `sudo python3 install_connections.py --config-stdin < connections.example.json`.
3. Install Infinity in Grafana and restart it. Supply Grafana credentials as JSON
   on stdin to `sudo python3 publish_connections.py --credentials-stdin
   --bridge-url http://PRIVATE_BIND_IP:9121`. This creates the private datasource
   with UID `sing-box-connections` and inserts only the new section into the
   current dashboard, backing it up first. Tokens stay in secure datasource fields.

The central worker uses Python's standard library, fetches exporter caches in
parallel, shares a two-second cache across viewers and stores no history. Failed
or stale selected collectors raise a visible query error. Sessions closed between
polls can be missed; UDP rows describe tracked sessions, not packet-level targets.
Unknown domains stay blank. A destination IP alone is not a unique website identity.
Optional `ipinfo_metrics_url` reads the existing local IPinfo worker's `/metrics`
cache to prefix Source IP with a country flag. It adds no external lookups and
does not change raw IP identities; unavailable metadata leaves a plain IP.
Omit this setting if the IPinfo worker is not installed. Windows needs a flag-capable
font, such as the optional self-hosted country font described above.

Domains may come directly from the client. Optional server HTTP/TLS/QUIC sniffing
can discover additional names, using
`sudo python3 enable_domain_sniffing.py --service UNIT=/absolute/config.json`.
The helper validates, backs up and preserves existing rules, using a 100ms timeout
when it adds a rule. Sniffing can affect existing domain-based routing, and adds
connection setup work: review rules and measure CPU first on restricted hosts.
HTTPS URL paths and content stay encrypted; ECH can hide the actual SNI hostname.

Remove the feature by deleting panels 900/901 and datasource `sing-box-connections`,
stopping/disabling the central service, and disabling `connection_details` on
exporters. Keep backups for rollback as needed. Run `python3 -m unittest -v
test_connections` to check grouping, privacy, staleness, session removal and
idempotent dashboard/sniff updates.

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

## Optional central IPinfo lookup

Run `install_ipinfo.py` only on the central Prometheus machine (Linux/systemd,
Python 3.11+), not on every proxy. It reads the source-IP labels
from local Prometheus, skips private/reserved/multicast IPs and privacy/overflow
placeholders, and stores one country/ASN record per distinct public IP. Raw-IP
source mode is required; subnet and hidden labels deliberately are not queried.

Supply an IPinfo Lite token via stdin, never a command argument or tracked file:

```sh
# Supply {"token":"YOUR_TOKEN"} privately on stdin, for example from a secret
# manager. The installer intentionally does not accept --token as an argument.
sudo python3 install_ipinfo.py --token-stdin
```

Token/config permissions are root/worker-group only under `/etc/sing-box-ipinfo`.
Metrics bind to IPv4 loopback `127.0.0.1:9120`; Prometheus must share the host
network. Uncomment the optional job in `prometheus.example.yml`, validate with
`promtool check config`, then reload. The installer's optional
`--integrate-prometheus /ABSOLUTE/prometheus.yml` automates integration only for
an existing host-network Docker Prometheus with the documented
`/etc/prometheus/sing-box` bind mount. Other layouts require manual integration;
do not expose port 9120 publicly. Grafana dashboard UID stays `sing-box-fleet`;
its title is `sing-box monitor`.

Defaults: discover sources every 2s, retain inactive cache entries for seven days,
no IP-count limit, at most 32 lookups per cycle and ten per second, four-second HTTP
timeouts. New uncached IPs take priority over expired cache refreshes. Configure
`discovery_interval` (1–3600 seconds), `lookup_spacing` (0.1–60 seconds), and
`lookup_batch` (1–256) in the worker config to tune pacing. Prometheus scrapes
the enrichment worker every 5s; the dashboard defaults to 5s refresh.
Errors back off; authentication/rate-limit failures pause external
requests for an hour. Last-good attribution survives temporary API failures,
but is omitted after 30 days without refresh. API calls never run in scrape
handlers or browser refreshes. The native worker has a 64MiB memory limit and
5% one-core CPU cap; the proxy service is unaffected. Its systemd state directory
contains private source-IP cache data and is not suitable for publication.

Lookup failures or an uninstalled worker leave the original source rows/values
intact through an explicit left join. Country/ASN describe the observed peer IP,
not authenticated user identity or residence. Public IPs are sent to IPinfo;
enable this opt-in service only if appropriate for your privacy policy. The
token is never included in metrics, dashboard JSON, URLs, logs or public sources.
Organization is displayed in a wide, wrapping column instead of numeric ASN.
Source connections occupies 19/24 width beside the narrow Source IPs chart;
Observed source bandwidth is below, beside Source active-connection bytes.
Source active-connection bytes shows one row per host/source IP with Upload and
Download columns instead of separate direction rows; sorted by Download, with
the existing byte-gauge meaning and host/service filters unchanged.
ASN remains available in metadata, but hidden from the table.
Attribution changes can create new metadata time series; cache cardinality
is time-managed, while historical series remain until Prometheus retention removes them.

Both source tables prefix Source IP with small country-flag emoji instead of a
separate image column. The central metadata metric's `source_ip_display` label
is joined only for presentation; raw `source_ip` and agent measurements remain
unchanged. Missing metadata falls back to a plain IP without dropping rows.
There are no additional API lookups or metric series per IP. On Windows, install
the optional self-hosted flag font described above. Existing metric-only Grafana
installations can update only these tables using `publish_source_tables.py
--credentials-stdin`; current dashboard backups are saved before changes.

Run `python3 -m unittest -v test_ipinfo` for cache/privacy/failure tests, and
`python3 verify_ipinfo.py` on the central host for real PromQL join/fallback
reconciliation without making additional external lookups.

## License and upstream references

MIT. Independent project, not affiliated with SagerNet, Prometheus or Grafana.

- [sing-box Clash API](https://sing-box.sagernet.org/configuration/experimental/clash-api/)
- [sing-box connection snapshot source](https://github.com/SagerNet/sing-box/blob/af6e64c3b69e6132ebaee0e1a3d24e93903f6709/experimental/clashapi/connections.go)
- [Linux inet_diag ABI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/inet_diag.h)
- [Linux TCP_INFO ABI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/tcp.h)
- [ss RTT definition](https://man7.org/linux/man-pages/man8/ss.8.html)
