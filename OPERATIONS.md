# Unified monitoring operations

`monitorctl.py` provides one native systemd management entry point for these
helpers: **exporter**, **ipinfo**, **connections**, **probes**, and **sessions**.
It keeps their existing private configuration paths and resource limits. It does not
manage sing-box proxy cores, Prometheus, Grafana, node exporter or unrelated monitors.
No additional container stack is required.

## Install and manage

First install the exporter using the README quick start, or install any optional
helper below. From the reviewed source directory, group installed helpers:

```sh
sudo python3 monitorctl.py bootstrap --source "$PWD"
sudo sing-box-monitor status
sudo sing-box-monitor check               # authenticated local TLS health
sudo sing-box-monitor rtt                 # inspect configured versus live socket UID
sudo sing-box-monitor rtt --apply         # repair mismatches; restart only exporter
sudo sing-box-monitor logs probes --lines 30
sudo sing-box-monitor restart probes     # just this helper
sudo sing-box-monitor restart            # all installed monitoring helpers
```

The persistent `sing-box-monitoring.target` starts installed helpers at boot.
`start`/`stop`/`restart` accept a component name or `all` (the default).
Individual `Restart=always` policies remain effective. Stopping the group stops
only its helpers; restarting it does not restart the proxy cores.

For reproducible deployment, copy `deployment.example.json` to a root-only
manifest, remove components not needed on this machine, and supply actual
addresses/arguments and private configuration paths. Deploy with:

```sh
sudo sing-box-monitor deploy --manifest /etc/sing-box-monitoring/deployment.json
```

The manifest references private files rather than embedding tokens/passwords.
Installers require those inputs to exist; they do not obtain credentials from
remote servers. The optional Prometheus integration expects the central
host's existing Docker container named `prometheus`, validates the current
configuration with its `promtool`, and narrowly updates managed scrape jobs.
Other Prometheus topologies can use the example scrape job manually.
Exporter deployment may configure the core API/firewall as described in the
README: for code-only maintenance use `upgrade`, not `deploy`.

## Code updates and recovery

Copy a reviewed source release to a staging directory, then:

```sh
sudo sing-box-monitor upgrade all --source /path/to/reviewed-source --dry-run
sudo sing-box-monitor upgrade all --source /path/to/reviewed-source
sudo python3 /path/to/reviewed-source/monitorctl.py bootstrap --source /path/to/reviewed-source
```

Updates compile Python first, replace only changed helper modules, restart
affected helpers one at a time, and verify systemd liveness. Private configs,
credentials, sampling intervals and quotas are not rewritten. Original modules
are retained in root-only `/var/backups/sing-box-monitoring/<timestamp>/` and
restored automatically if that helper fails to restart. A running process alone
does not prove upstream connectivity: inspect Grafana health/checks afterwards.
Keep these backups until the new version has been validated.

## Go exporter rollout

The [Go exporter](go-exporter/README.md) is the only maintained runtime for Linux
amd64 and arm64. Python deployment and probe helpers remain separate. It preserves the same private JSON
configuration, TLS certificates, bearer tokens, service account and `singbox_*`
metric definitions. Socket diagnostics and Clash API polling remain read-only.

`monitorctl.py` / `sing-box-monitor` provides native lifecycle commands:

- `native shadow --binary /path/to/binary`: starts a shadow instance on loopback port 19119 using existing credentials.
- `native promote --binary /path/to/binary`: backs up the current binary and systemd drop-in, checks authenticated HTTPS health on port 9119, and rolls back automatically if health check fails.
- `native rollback --backup /var/backups/sing-box-monitoring/native-exporter-N`: restores the previous executable and configuration.
- `native remove-shadow`: cleans up the temporary shadow unit.

### Resource comparison

In production rollout comparisons with identical 2s API/TCP and 5s process polling under live traffic:

| Metric | Python exporter | Go exporter |
| --- | ---: | ---: |
| Single-core CPU % | ~0.8–1.2% | ~0.7–0.9% |
| Process RSS | ~21–28 MiB | ~13–17 MiB |
| Systemd cgroup Memory | ~15–22 MiB | ~10–11 MiB |
| Authenticated TLS scrape latency | ~6–11 ms | ~7–15 ms |

This historical comparison used matching fixtures and real traffic validation;
the Python collector is now frozen as a test reference and cannot run as a service.
The Go implementation enforces `MemoryMax=64M`, soft heap `GOMEMLIMIT=40MiB`, and `GOMAXPROCS=1`.

## Central end-to-end HTTPS checks

Requires existing Python 3 and sing-box on the central host; curl is required
only for the explicit `request_backend: "curl"` fallback. Copy
`probe.example.json`, supply one real client **outbound** per listener, and keep
the result private. Never publish this file; it contains proxy credentials.
An outbound must be compatible with the central sing-box version.

```sh
sudo python3 install_proxy_probe.py --config-stdin < /root/private-probes.json
sudo python3 integrate_probe_prometheus.py --path /opt/monitoring/prometheus.yml
```

The dedicated `sing-box-probe.service` exposes cached metrics on loopback
`127.0.0.1:9122`. Default cadence is 60s, timeout 8s, concurrency two, up to four
small HTTPS checks per listener. Each cycle creates a short-lived, isolated
loopback SOCKS client with fresh transports, without TUN or system route changes.
The default native request backend uses SOCKS5 remote DNS and verified TLS,
without curl children or response-body files. A reusable TLS context avoids
reloading the trust store per check; every request still creates a fresh socket
and TLS transport. There is no direct fallback, pooling, redirect or retry.
The complete SOCKS/TLS/HTTP/body request shares the monotonic overall deadline;
`connect_timeout` also covers through the TLS handshake. Bodies are capped at
32KiB, HTTP wire data at 128KiB, and truncated Content-Length/chunked bodies fail.
Temporary client configurations remain private and are removed after each cycle;
credential-bearing child output and response bodies are not logged. Set
`request_backend` to `curl` and restart only the probe helper for diagnostic
rollback; a failed native request never automatically retries with curl.

Probe metrics use only configured host/service/protocol/listener/check/vantage
labels, never URL, destination IP, source IP, exit IP or connection IDs:

- `singbox_probe_success`: latest **fresh** check, 1 passed or 0 failed.
- `singbox_probe_duration_seconds`: successful HTTPS request time, including
  proxy setup, remote-path DNS, TLS and response; excludes client process startup.
  Failed checks omit this value rather than showing zero or an old success.
- `singbox_probe_attempt_duration_seconds`: elapsed attempt including failure.
- `singbox_probe_http_status_code`: observed response code, 0 if none.
- `singbox_probe_last_run_timestamp_seconds`, `result_fresh`, `runner_up`:
  freshness/runner diagnostics; stale success/duration samples are omitted after
  twice the configured interval plus 20s. Pending startup is not reported as OK.
- `singbox_probe_checks_total{outcome="success|failure"}`: persistent outcome
  counters. Scrapes repeating a cached result are not additional attempts.

The dashboard includes a latest-results table, success history and response-time
chart, immediately above Service resources and collector health. Global
Host/Service/Protocol filters apply. Existing installations can
patch only these new panels with `publish_network_panels.py`; supply Grafana
credentials via stdin and optionally the existing host-label file.
The latest-results table groups by host/protocol, with separate Cloudflare and
ChatGPT status, time, HTTP and age columns. If a filtered group contains multiple
listeners/services/vantages, status is the worst fresh result, time is the
slowest successful request (only when all pass), and age is the oldest result.
Any stale/pending contributor makes that endpoint unavailable; mixed HTTP codes
are blank. The hidden freshness query retains configured pending/stale rows.
Use `--table-only` to apply just this table revision to an existing dashboard.

## Passive TCP diagnostics

These fields are decoded in the exporter's **existing** TCP socket poll. No
packet capture, per-socket metric labels or additional host agent is required.
Only established physical client-facing TCP sockets matched to configured
listeners are included, including idle/pre-authentication sockets. UDP/QUIC
cannot use these fields. Optional fields are length-gated for kernel support.

- `singbox_tcp_observed_retransmissions_total`: positive retransmission changes
  for sockets observed in consecutive snapshots, keyed internally by socket
  cookie. First observation establishes a baseline. It is a **lower bound**:
  short-lived sockets and changes after the final observation can be missed.
  Counter resets/replaced sockets are not attributed as retransmissions.
- `singbox_tcp_retransmissions_current`: lifetime retransmission total across
  current supported sockets; a **gauge** that falls when sockets close. Do not
  apply `rate()` or interpret it as a fleet-lifetime counter.
- `singbox_tcp_cwnd_bytes_sum` / `singbox_tcp_cwnd_socket_count`: socket-weighted
  mean server congestion window, send MSS times cwnd segments. Capacity, not
  throughput or available bandwidth. Empty/unsupported mean stays blank.
- `singbox_tcp_send_queue_bytes`: unsent plus unacknowledged bytes.
- `singbox_tcp_receive_queue_bytes`: bytes waiting for the proxy to read.
- `singbox_tcp_notsent_bytes`: optional unsent **subset** of the send queue.
  Do not add these together. A zero queue is valid; stale collection omits values.

Source: [Linux TCP_INFO](https://github.com/torvalds/linux/blob/master/include/uapi/linux/tcp.h).

## Idle-session cleanup and clean live views

The exporter remains read-only and unprivileged. Main RTT panels use
`singbox_tcp_client_rtt_*`: fresh API-authenticated sessions matched by exact
inbound, source IP and source port, with TCP payload and ACK activity within
`tcp_max_idle` (120 seconds by default). Raw `singbox_tcp_rtt_*` metrics and
physical socket/queue/retransmission metrics remain available for diagnostics.
QUIC/Hysteria2 client RTT is still unsupported; gaps are not zero latency.

Live destinations defaults to byte activity observed in the last five minutes.
Choose **Destination view → All open sessions** to inspect idle sessions and
the **Idle** column. A collector restart establishes a new conservative baseline;
unchanged sessions disappear from the recent view after five minutes. This is a
display filter, not a claim that an idle session has closed. Source/route API
counts remain exact for all open sessions.

Install the separate, opt-in guard on a native Linux/systemd host:

```sh
sudo python3 install_session_guard.py                   # audit only
sudo python3 install_session_guard.py --enable-cleanup  # explicit selective closure
sudo python3 monitorctl.py bootstrap --source .
sudo sing-box-monitor logs sessions
```

It runs every five minutes, keeps bounded current-socket state, and closes only
sing-box MainPID-owned **FIN-WAIT-2** sockets observed in that state for at least
one hour, without TCP payload for one hour, with empty receive/send queues.
The process executable, PID/FD ownership, current socket state and kernel cookie
are revalidated. It never closes ESTABLISHED sockets, UDP/QUIC transports, listeners
or whole services, and never creates IP bans. At most 256 closures per run, with
2% CPU and 64 MiB memory limits. Initial deployment waits one hour before any
closure; it does not infer state duration from connection age. Kernel
`CONFIG_INET_DIAG_DESTROY` support is required; errors fail closed and are logged.
Only this separately confined root service has socket-destroy capabilities;
the exporter has none. Inspect its journal for actual closure counts.

`verify_session_guard_kernel.py` can verify kernel/capability support on a new
host without touching a production session: it creates one loopback TCP socket,
moves it to FIN-WAIT-2, destroys that exact test socket, then exits. Run it as
root from the reviewed source directory before relying on cleanup, especially
inside LXC containers. This verifies the kernel operation, not that a real
sing-box session has yet met the one-hour eligibility rule.

Manifest entry: `"sessions": {"enable_cleanup": true}` (false/default is audit).
Rollback: `sudo systemctl disable --now sing-box-session-guard.timer`; leave
historical metrics alone. No proxy restart or global TCP timeout change is needed.

## Consistent listener names

Use `shadowsocks-in` as the server inbound tag for a service with one
Shadowsocks listener. Existing servers can migrate with:

```sh
sudo python3 normalize_inbound_tags.py --service sing-box=/etc/sing-box/config.json
```

This changes only the listener tag and matching nested inbound selectors in
route/DNS rules, retaining passwords, ports and other configuration. It validates
the candidate, updates the corresponding exporter inventory, briefly restarts
the affected core/exporter and retains private backups under
`/var/backups/sing-box-inbound-tags/`. Ambiguous multiple Shadowsocks listeners or
tag conflicts are rejected. Update the central probe's target ID to reflect the
new name separately; client proxy passwords/settings do not need changing.

## Removal

Stop/disable only the intended helper and remove its `monitoring-group.conf`
drop-in. Re-run bootstrap from the source bundle after removing that helper's
owned unit, or explicitly update the target's `Wants=` list. For probe removal,
also remove only its marked scrape block from the **current** Prometheus config,
validate and reload, and remove panels 1000–1003 from Grafana. TCP charts are
1004–1006; removing them does not disable passive collection. Preserve unrelated
jobs, datasets and current proxy configuration. Keep protected rollback backups
until removal is confirmed, then review runtime credentials/state for deletion.
