package main

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

type Tags map[string]string

func extend(tags Tags, kvs ...string) Tags {
	n := Tags{}
	for k, v := range tags {
		n[k] = v
	}
	for i := 0; i < len(kvs); i += 2 {
		n[kvs[i]] = kvs[i+1]
	}
	return n
}

type metrics struct {
	strings.Builder
	names map[string]bool
}

func (m *metrics) add(name string, value float64, tags Tags, kind string) {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return
	}
	full := "singbox_" + name
	if !m.names[full] {
		help := metricHelp[name]
		if help == "" {
			help = "Native sing-box collector metric; compatible with the Python exporter."
		}
		fmt.Fprintf(&m.Builder, "# HELP %s %s\n# TYPE %s %s\n", full, help, full, kind)
		m.names[full] = true
	}
	keys := []string{}
	for k := range tags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	fmt.Fprintf(&m.Builder, "%s{", full)
	for i, k := range keys {
		if i > 0 {
			m.WriteByte(',')
		}
		v := strings.NewReplacer("\\", "\\\\", "\n", "\\n", "\"", "\\\"").Replace(tags[k])
		fmt.Fprintf(&m.Builder, "%s=\"%s\"", k, v)
	}
	fmt.Fprintf(&m.Builder, "} %s\n", strconv.FormatFloat(value, 'g', -1, 64))
}
func (m *metrics) gauge(name string, value float64, tags Tags)   { m.add(name, value, tags, "gauge") }
func (m *metrics) counter(name string, value float64, tags Tags) { m.add(name, value, tags, "counter") }
func boolNumber(v bool) float64 {
	if v {
		return 1
	}
	return 0
}
func routeTags(service string, key Route) Tags {
	return Tags{"service": service, "protocol": key[0], "inbound": key[1], "network": key[2], "outbound": key[3]}
}
func identityTags(key Identity) Tags {
	return Tags{"service": key[0], "protocol": key[1], "inbound": key[2]}
}
func (m *metrics) buckets(name string, v *RTT, t Tags) {
	for i, b := range bounds {
		label := strconv.FormatFloat(b, 'f', -1, 64)
		if math.IsInf(b, 1) {
			label = "+Inf"
		}
		m.gauge(name, v.Buckets[i], extend(t, "le", label))
	}
}
func (c *Collector) render(now float64) []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	m := metrics{names: map[string]bool{}}
	active, tracked := map[string]bool{}, map[string]bool{}
	for _, s := range c.config.Services {
		state := c.states[s.Name]
		if state == nil {
			state = newState()
		}
		t := Tags{"service": s.Name}
		up := fresh(state.Up, state.Timestamp, now, c.config.Poll)
		m.gauge("up", boolNumber(up), t)
		m.gauge("last_success_timestamp_seconds", state.Timestamp, t)
		m.gauge("collection_duration_seconds", state.Duration, t)
		m.counter("collection_errors_total", state.Errors, t)
		m.counter("api_resets_total", state.Resets, t)
		for key, value := range c.processes[s.Name] {
			if key == "collected" {
				continue
			}
			kind := "gauge"
			if strings.HasSuffix(key, "_total") {
				kind = "counter"
			}
			m.add("process_"+key, value, t, kind)
		}
		for ip := range state.Sources {
			active[ip] = true
		}
		for ip := range state.Seen {
			tracked[ip] = true
		}
		if !up {
			continue
		}
		m.counter("traffic_bytes_total", state.Totals[0], extend(t, "direction", "upload"))
		m.counter("traffic_bytes_total", state.Totals[1], extend(t, "direction", "download"))
		m.gauge("memory_bytes", state.Memory, t)
		m.gauge("connections", float64(state.Connections), t)
		m.gauge("label_overflow_connections", float64(state.Dropped), t)
		for key, v := range state.Groups {
			tags := routeTags(s.Name, key)
			m.gauge("route_connections", v[0], tags)
			m.gauge("route_oldest_connection_seconds", v[3], tags)
			m.gauge("route_active_bytes", v[1], extend(tags, "direction", "upload"))
			m.gauge("route_active_bytes", v[2], extend(tags, "direction", "download"))
		}
		for key, v := range state.Observed {
			tags := routeTags(s.Name, key)
			m.counter("route_observed_bytes_total", v[0], extend(tags, "direction", "upload"))
			m.counter("route_observed_bytes_total", v[1], extend(tags, "direction", "download"))
			m.counter("route_observed_connections_total", v[2], tags)
		}
		for ip, v := range state.Sources {
			tags := extend(t, "source_ip", ip)
			m.gauge("source_connections", v[0], tags)
			m.gauge("source_active_bytes", v[1], extend(tags, "direction", "upload"))
			m.gauge("source_active_bytes", v[2], extend(tags, "direction", "download"))
		}
		for key, v := range state.Protocols {
			tags := extend(t, "protocol", key[0], "source_ip", key[1])
			m.gauge("source_protocol_connections", v, tags)
			recent := float64(0)
			for _, activity := range state.SourceActivity[key] {
				if activity > 0 && now-activity <= 300 {
					recent++
				}
			}
			m.gauge("source_protocol_recent_connections", recent, tags)
		}
		for ip, v := range state.SourceObserved {
			tags := extend(t, "source_ip", ip)
			m.counter("source_observed_bytes_total", v[0], extend(tags, "direction", "upload"))
			m.counter("source_observed_bytes_total", v[1], extend(tags, "direction", "download"))
		}
		for inbound, info := range s.Inbounds {
			m.gauge("inbound_info", 1, extend(t, "inbound", inbound, "protocol", info.Type, "port", strconv.Itoa(info.Port)))
		}
	}
	if c.config.TCP {
		tcp := c.tcp
		up := fresh(tcp.Up, tcp.Timestamp, now, c.config.TCPPoll)
		m.gauge("tcp_collector_up", boolNumber(up), nil)
		m.gauge("tcp_collector_last_success_timestamp_seconds", tcp.Timestamp, nil)
		m.gauge("tcp_collector_duration_seconds", tcp.Duration, nil)
		m.counter("tcp_collector_errors_total", tcp.Errors, nil)
		if up {
			m.gauge("tcp_unmatched_sockets", float64(tcp.Unmatched), nil)
			for key, v := range tcp.Groups {
				t := identityTags(key)
				m.gauge("tcp_established_sockets", v.Sockets, t)
				m.gauge("tcp_send_queue_bytes", v.SendQueue, t)
				m.gauge("tcp_receive_queue_bytes", v.ReceiveQueue, t)
				m.gauge("tcp_cwnd_socket_count", v.CwndCount, t)
				m.gauge("tcp_notsent_socket_count", v.NotsentCount, t)
				if v.CwndCount > 0 {
					m.gauge("tcp_cwnd_bytes_sum", v.CwndBytes, t)
				}
				if v.NotsentCount > 0 {
					m.gauge("tcp_notsent_bytes", v.Notsent, t)
				}
				if v.RetransCount > 0 || v.Sockets == 0 {
					m.gauge("tcp_retransmissions_current", v.Retrans, t)
					m.counter("tcp_observed_retransmissions_total", c.retrans[key], t)
				}
				m.gauge("tcp_rtt_snapshot_count", v.Count, t)
				if v.Count == 0 {
					continue
				}
				m.gauge("tcp_rtt_snapshot_sum", v.Sum, t)
				m.gauge("tcp_rtt_max_seconds", v.Maximum, t)
				m.gauge("tcp_rtt_variation_sum_seconds", v.Variation, t)
				m.gauge("tcp_ack_age_max_seconds", v.AckAge, t)
				m.buckets("tcp_rtt_snapshot_bucket", v, t)
			}
			for key, v := range tcp.Sources {
				t := extend(identityTags(key.Key), "source_ip", key.Source)
				for i, suffix := range []string{"count", "sum", "max_seconds", "ack_age_max_seconds"} {
					m.gauge("tcp_source_rtt_"+suffix, v[i], t)
				}
			}
			for key, v := range tcp.Clients {
				state := c.states[key[0]]
				if state == nil || !fresh(state.Up, state.Timestamp, now, c.config.Poll) {
					continue
				}
				t := identityTags(key)
				m.gauge("tcp_client_rtt_snapshot_count", v.Count, t)
				if v.Count == 0 {
					continue
				}
				m.gauge("tcp_client_rtt_snapshot_sum", v.Sum, t)
				m.gauge("tcp_client_rtt_max_seconds", v.Maximum, t)
				m.gauge("tcp_client_rtt_ack_age_max_seconds", v.AckAge, t)
				m.gauge("tcp_client_rtt_payload_age_max_seconds", v.PayloadAge, t)
				m.buckets("tcp_client_rtt_snapshot_bucket", v, t)
			}
		}
	}
	m.gauge("exporter_source_ips_active", float64(len(active)), nil)
	m.gauge("exporter_source_ips_tracked", float64(len(tracked)), nil)
	if c.certExpiry > 0 {
		m.gauge("exporter_certificate_expiry_timestamp_seconds", c.certExpiry, nil)
	}
	cpu, rss := ownUsage()
	m.counter("exporter_cpu_seconds_total", cpu, nil)
	m.gauge("exporter_max_rss_bytes", rss, nil)
	m.gauge("exporter_start_time_seconds", c.started, nil)
	m.gauge("exporter_build_info", 1, Tags{"version": version})
	return []byte(m.String())
}

type ServiceHealth struct {
	Service   string  `json:"service"`
	Healthy   bool    `json:"healthy"`
	Timestamp float64 `json:"timestamp"`
	MaxAge    float64 `json:"max_age_seconds"`
}
type ConnectionSnapshot struct {
	Generated float64         `json:"generated_at"`
	Services  []ServiceHealth `json:"services"`
	Rows      []Detail        `json:"rows"`
}

func (c *Collector) connections(now float64) ConnectionSnapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	snapshot := ConnectionSnapshot{Generated: now, Services: []ServiceHealth{}, Rows: []Detail{}}
	for _, s := range c.config.Services {
		state := c.states[s.Name]
		if state == nil {
			state = newState()
		}
		healthy := fresh(state.Up, state.Timestamp, now, c.config.Poll)
		snapshot.Services = append(snapshot.Services, ServiceHealth{s.Name, healthy, state.Timestamp, math.Max(2, c.config.Poll)*3 + 3})
		if healthy {
			snapshot.Rows = append(snapshot.Rows, state.Details...)
		}
	}
	return snapshot
}
func (c *Collector) healthy(now float64) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, s := range c.config.Services {
		state := c.states[s.Name]
		if state == nil || !fresh(state.Up, state.Timestamp, now, c.config.Poll) {
			return false
		}
	}
	return !c.config.TCP || fresh(c.tcp.Up, c.tcp.Timestamp, now, c.config.TCPPoll)
}
