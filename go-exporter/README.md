# Go sing-box exporter

Dependency-free Go implementation of `../exporter.py`, for Linux amd64 and arm64.
It uses the same private JSON configuration, bearer-authenticated HTTPS endpoints,
service account and `singbox_*` metric names/labels/types. TCP socket diagnostics
remain unprivileged and read-only. Route monitoring is a separate service.

The API transport reuses loopback HTTP connections and ignores environment
proxies and redirects. TCP dumps reject truncation, interrupted dumps, byte
overflow and socket overflow. Authentication requires the exact API source
tuple and inbound, with recent payload and ACK. Snapshot RTT buckets remain
gauges; observed byte/retransmission counters remain sampling lower bounds.

TLS is at least 1.2, with a three-second HTTP deadline, a 32 KiB header budget
and at most 16 concurrent incoming connections. Request handlers read cached
state; scrapes do not start probes. The managed unit retains `MemoryMax=64M`,
uses `GOMEMLIMIT=40MiB` and `GOMAXPROCS=1`, and does not receive capabilities.
The Go heap limit is soft and is separate from the systemd memory ceiling.

## Build and validate

Use Go 1.23 or newer on Linux. The rollout was built and tested with Go 1.27.2.

```sh
cd go-exporter
go test -race ./...
go vet ./...
mkdir -p ../.runtime
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags '-s -w' \
  -o ../.runtime/sing-box-exporter-go .
python3 ../compare_exporter.py --binary ../.runtime/sing-box-exporter-go
```

Use `GOARCH=arm64` for ARM64 hosts such as SG Oracle; the managed rollout checks
the ELF architecture against the host before executing the candidate.
Windows can cross-compile the same static binary with Go's `GOOS=linux`,
`GOARCH=amd64` (or `arm64`), `CGO_ENABLED=0` environment variables. Run the Linux
tests and offline parity checker on a host matching the target architecture.
Build artifacts belong in ignored `.runtime/`; do not commit executables or
private test snapshots.

`--fixture FILE` is an offline CLI compatibility harness, with no network calls.
Its JSON output includes supplied connection rows; use only synthetic fixtures
or a private temporary directory for real snapshots. It is not an HTTP endpoint.

## Deploy

Bootstrap the current management bundle first. On an existing Python exporter:

```sh
sudo sing-box-monitor native shadow --binary /path/to/sing-box-exporter-go
# Compare authenticated loopback port 19119 with production port 9119.
sudo sing-box-monitor native promote --binary /path/to/sing-box-exporter-go
sudo sing-box-monitor check exporter
sudo sing-box-monitor native remove-shadow
```

For subsequent native upgrades, use `native promote --binary` directly. It saves
the current binary and implementation drop-in, restarts only the collector,
checks authenticated health, and restores the previous implementation if health
fails. The original Python files and main unit remain available for rollback:

```sh
sudo sing-box-monitor native rollback --backup /var/backups/sing-box-monitoring/native-exporter-N
```

The Python `upgrade exporter` command refuses a running Go exporter, so it cannot
report a successful native upgrade after copying unrelated Python files.
`upgrade all` and the fleet deployment script skip native exporter code; upgrade
the native binary explicitly. See [operations](../OPERATIONS.md#go-exporter-rollout)
for the deployed comparison, limitations and exact recovery paths.
