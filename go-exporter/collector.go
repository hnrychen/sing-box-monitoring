// Read-only exporter. Metric identities and sampled counter semantics match exporter.py.
package main

import (
	"encoding/json"
	"fmt"
	"math"
	"net/netip"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
)

const version = "1.4.0-go"
const maxResponse = 8 << 20

var bounds = []float64{.005, .01, .025, .05, .075, .1, .15, .25, .5, 1, 2, 5, math.Inf(1)}

type Inbound struct {
	Type    string `json:"type"`
	Port    int    `json:"port"`
	Listen  string `json:"listen"`
	Network string `json:"network"`
}
type Service struct {
	Name      string             `json:"name"`
	URL       string             `json:"url"`
	Secret    string             `json:"secret"`
	UID       *uint32            `json:"tcp_uid"`
	Inbounds  map[string]Inbound `json:"inbounds"`
	Outbounds []string           `json:"outbounds"`
}
type Config struct {
	Listen         string    `json:"listen"`
	Token          string    `json:"scrape_token"`
	Cert           string    `json:"tls_cert"`
	Key            string    `json:"tls_key"`
	Poll           float64   `json:"poll_interval"`
	TCPPoll        float64   `json:"tcp_poll_interval"`
	ProcessPoll    float64   `json:"process_interval"`
	TCP            bool      `json:"tcp_rtt"`
	Details        bool      `json:"connection_details"`
	SourceMode     string    `json:"source_mode"`
	MaxConnections int       `json:"max_connections"`
	Idle           float64   `json:"tcp_max_idle"`
	SourceTTL      float64   `json:"source_idle_ttl"`
	Services       []Service `json:"services"`
}

func defaultConfig() Config {
	return Config{Listen: "127.0.0.1:9119", Poll: 5, TCPPoll: 30, ProcessPoll: 30, MaxConnections: 10000, Idle: 120, SourceTTL: 3600, SourceMode: "off"}
}
func stamp() float64 { return float64(time.Now().UnixNano()) / 1e9 }
func fresh(up bool, timestamp, now, interval float64) bool {
	return up && now-timestamp < math.Max(2, interval)*3+3
}

type Route [4]string
type Identity [3]string
type SourceProtocol [2]string
type Endpoint struct {
	Key  Identity
	IP   string
	Port int
}
type Pair [2]float64
type RouteValues [4]float64
type SourceValues [3]float64
type Detail struct {
	Service     string  `json:"service"`
	Source      string  `json:"source_ip"`
	Protocol    string  `json:"protocol"`
	Network     string  `json:"network"`
	Domain      string  `json:"domain"`
	Destination string  `json:"destination_ip"`
	Port        *int    `json:"destination_port"`
	Outbound    string  `json:"outbound"`
	Age         float64 `json:"age_seconds"`
	Activity    float64 `json:"last_activity_timestamp"`
	Upload      float64 `json:"upload_bytes"`
	Download    float64 `json:"download_bytes"`
}
type State struct {
	Up                                          bool
	Timestamp, Duration, Errors, Resets, Memory float64
	Totals                                      Pair
	Initialized                                 bool
	Groups                                      map[Route]RouteValues
	Observed                                    map[Route]SourceValues
	Sources                                     map[string]SourceValues
	SourceObserved                              map[string]Pair
	Protocols                                   map[SourceProtocol]float64
	SourceActivity                              map[SourceProtocol][]float64
	RouteActivity                               map[Route][]float64
	Seen                                        map[string]float64
	Previous                                    map[string]Pair
	Activity                                    map[string]float64
	Endpoints                                   map[Endpoint]bool
	Details                                     []Detail
	Dropped, Connections                        int
}
type Snapshot struct {
	Upload      float64          `json:"uploadTotal"`
	Download    float64          `json:"downloadTotal"`
	Memory      float64          `json:"memory"`
	Connections []map[string]any `json:"connections"`
}

func decodeSnapshot(raw []byte) (Snapshot, error) {
	var s Snapshot
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return s, err
	}
	for _, key := range []string{"uploadTotal", "downloadTotal", "memory"} {
		var value float64
		if len(fields[key]) == 0 || string(fields[key]) == "null" || json.Unmarshal(fields[key], &value) != nil || value < 0 || math.IsNaN(value) || math.IsInf(value, 0) {
			return s, fmt.Errorf("invalid total schema")
		}
	}
	err := json.Unmarshal(raw, &s)
	return s, err
}
func stringValue(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}
func number(v any) (float64, error) {
	switch value := v.(type) {
	case nil:
		return 0, nil
	case float64:
		if !math.IsNaN(value) && !math.IsInf(value, 0) {
			return value, nil
		}
	case string:
		integer, err := strconv.ParseInt(value, 10, 64)
		return float64(integer), err
	}
	return 0, fmt.Errorf("invalid connection number")
}
func text(v any, limit int) string {
	var b strings.Builder
	for i, r := range []rune(stringValue(v)) {
		if i >= limit {
			break
		}
		if unicode.IsPrint(r) {
			b.WriteRune(r)
		}
	}
	return b.String()
}
func source(c Config, address string) string {
	if c.SourceMode == "off" {
		return "hidden"
	}
	ip, err := netip.ParseAddr(address)
	if err != nil {
		return "unknown"
	}
	if c.SourceMode == "raw" {
		if ip.Is4In6() {
			b := ip.As16()
			return fmt.Sprintf("::ffff:%x:%x", uint16(b[12])<<8|uint16(b[13]), uint16(b[14])<<8|uint16(b[15]))
		}
		return ip.String()
	}
	bits := 64
	if ip.Is4() {
		bits = 24
	}
	return netip.PrefixFrom(ip, bits).Masked().String()
}
func newState() *State {
	return &State{Groups: map[Route]RouteValues{}, Observed: map[Route]SourceValues{}, Sources: map[string]SourceValues{}, SourceObserved: map[string]Pair{}, Protocols: map[SourceProtocol]float64{}, Seen: map[string]float64{}, Previous: map[string]Pair{}, Activity: map[string]float64{}, Endpoints: map[Endpoint]bool{}, Details: []Detail{}}
}

type Collector struct {
	mu                  sync.Mutex
	config              Config
	states              map[string]*State
	processes           map[string]map[string]float64
	tcp                 TCPState
	previous            map[SocketIdentity]uint32
	retrans             map[Identity]float64
	started, certExpiry float64
}

func newCollector(c Config) *Collector {
	return &Collector{config: c, states: map[string]*State{}, processes: map[string]map[string]float64{}, previous: map[SocketIdentity]uint32{}, retrans: map[Identity]float64{}, started: stamp()}
}
func (c *Collector) update(s Service, data Snapshot, now float64) error {
	if len(data.Connections) > c.config.MaxConnections {
		return fmt.Errorf("active connection budget exceeded")
	}
	old := c.states[s.Name]
	if old == nil {
		old = newState()
	}
	n := newState()
	n.Up = true
	n.Timestamp = now
	n.Initialized = true
	n.Memory = data.Memory
	n.Totals = Pair{data.Upload, data.Download}
	n.Errors = old.Errors
	n.Resets = old.Resets
	reset := data.Upload < old.Totals[0] || data.Download < old.Totals[1]
	if reset {
		n.Resets++
	}
	for k, v := range old.Observed {
		n.Observed[k] = v
	}
	for k, v := range old.SourceObserved {
		n.SourceObserved[k] = v
	}
	for k, v := range old.Seen {
		n.Seen[k] = v
	}
	outbounds := map[string]bool{"direct": true, "DIRECT": true}
	for _, o := range s.Outbounds {
		outbounds[o] = true
	}
	for _, conn := range data.Connections {
		md, ok := conn["metadata"].(map[string]any)
		if !ok && conn["metadata"] != nil {
			return fmt.Errorf("invalid metadata schema")
		}
		typ := stringValue(md["type"])
		if typ == "" {
			typ = "unknown"
		}
		parts := strings.SplitN(typ, "/", 2)
		protocol, inbound := parts[0], parts[0]
		if len(parts) > 1 && parts[1] != "" {
			inbound = parts[1]
		}
		if info, ok := s.Inbounds[inbound]; ok {
			protocol = info.Type
		} else if len(s.Inbounds) > 0 {
			protocol = "unknown"
			inbound = "unknown"
		}
		network := stringValue(md["network"])
		if network != "tcp" && network != "udp" && network != "icmp" {
			network = "unknown"
		}
		outbound := "direct"
		if chain, ok := conn["chains"].([]any); ok && len(chain) > 0 {
			outbound = stringValue(chain[0])
		}
		rawOutbound := outbound
		if !outbounds[outbound] {
			outbound = "other"
		}
		route := Route{protocol, inbound, network, outbound}
		_, observed := n.Observed[route]
		_, active := n.Groups[route]
		if (!observed && len(n.Observed) >= 128) || (!active && len(n.Groups) >= 128) {
			route = Route{"other", "other", "other", "other"}
			n.Dropped++
		}
		ip := source(c.config, stringValue(md["sourceIP"]))
		n.Protocols[SourceProtocol{protocol, ip}]++
		n.Seen[ip] = now
		upload, err := number(conn["upload"])
		if err != nil {
			return err
		}
		download, err := number(conn["download"])
		if err != nil {
			return err
		}
		bytes := Pair{math.Max(0, math.Trunc(upload)), math.Max(0, math.Trunc(download))}
		id, exists := conn["id"]
		if !exists {
			return fmt.Errorf("missing connection identity")
		}
		cid := stringValue(id)
		before, seen := old.Previous[cid]
		if reset {
			seen = false
		}
		n.Activity[cid] = now
		// Existing sessions are not new traffic after a collector restart/reset.
		// Their start is a conservative lower bound until bytes actually change.
		if !old.Initialized || reset {
			n.Activity[cid] = 0
			if start, err := time.Parse(time.RFC3339Nano, stringValue(conn["start"])); err == nil {
				n.Activity[cid] = math.Min(now, float64(start.UnixNano())/1e9)
			}
		}
		if seen && before == bytes && !reset {
			if t, ok := old.Activity[cid]; ok {
				n.Activity[cid] = t
			}
		}
		if n.SourceActivity == nil {
			n.SourceActivity = map[SourceProtocol][]float64{}
		}
		k := SourceProtocol{protocol, ip}
		n.SourceActivity[k] = append(n.SourceActivity[k], n.Activity[cid])
		if n.RouteActivity == nil {
			n.RouteActivity = map[Route][]float64{}
		}
		n.RouteActivity[route] = append(n.RouteActivity[route], n.Activity[cid])
		if network == "tcp" && !udpProtocol(protocol) {
			ip, err := netip.ParseAddr(stringValue(md["sourceIP"]))
			port, e := number(md["sourcePort"])
			if err == nil && e == nil && port > 0 && port < 65536 {
				n.Endpoints[Endpoint{Identity{s.Name, protocol, inbound}, ip.Unmap().String(), int(port)}] = true
			}
		}
		delta := Pair{}
		if seen {
			delta = Pair{math.Max(0, bytes[0]-before[0]), math.Max(0, bytes[1]-before[1])}
		} else if old.Initialized || old.Errors > 0 {
			delta = bytes
		}
		if old.Initialized || old.Errors > 0 {
			v := n.Observed[route]
			v[0] += delta[0]
			v[1] += delta[1]
			if !seen {
				v[2]++
			}
			n.Observed[route] = v
			v2 := n.SourceObserved[ip]
			v2[0] += delta[0]
			v2[1] += delta[1]
			n.SourceObserved[ip] = v2
		}
		n.Previous[cid] = bytes
		age := float64(0)
		if start, err := time.Parse(time.RFC3339Nano, stringValue(conn["start"])); err == nil {
			age = math.Max(0, now-float64(start.UnixNano())/1e9)
		}
		g := n.Groups[route]
		g[0]++
		g[1] += bytes[0]
		g[2] += bytes[1]
		g[3] = math.Max(g[3], age)
		n.Groups[route] = g
		sv := n.Sources[ip]
		sv[0]++
		sv[1] += bytes[0]
		sv[2] += bytes[1]
		n.Sources[ip] = sv
		if c.config.Details {
			destination := ""
			if ip, err := netip.ParseAddr(text(md["destinationIP"], 253)); err == nil {
				destination = ip.String()
			}
			var port *int
			if v, err := number(md["destinationPort"]); err == nil && v > 0 && v < 65536 {
				p := int(v)
				port = &p
			}
			n.Details = append(n.Details, Detail{s.Name, ip, protocol, network, text(md["host"], 253), destination, port, text(rawOutbound, 120), age, n.Activity[cid], bytes[0], bytes[1]})
		}
	}
	for ip, t := range n.Seen {
		if now-t > math.Max(60, c.config.SourceTTL) {
			delete(n.Seen, ip)
			delete(n.SourceObserved, ip)
		}
	}
	for inbound, info := range s.Inbounds {
		for _, network := range []string{"tcp", "udp"} {
			k := Route{info.Type, inbound, network, "direct"}
			if _, ok := n.Groups[k]; !ok {
				n.Groups[k] = RouteValues{}
			}
			if _, ok := n.Observed[k]; !ok {
				n.Observed[k] = SourceValues{}
			}
		}
	}
	n.Connections = len(data.Connections)
	c.states[s.Name] = n
	return nil
}
func udpProtocol(s string) bool { return s == "hysteria" || s == "hysteria2" || s == "tuic" }

type RTT struct {
	Count, Sum, Maximum, Variation, AckAge, PayloadAge                                                   float64
	Buckets                                                                                              [13]float64
	Sockets, SendQueue, ReceiveQueue, CwndCount, CwndBytes, NotsentCount, Notsent, RetransCount, Retrans float64
}
type SourceKey struct {
	Key    Identity
	Source string
}
type SocketIdentity struct {
	Key    Identity
	Cookie [2]uint32
}
type TCPState struct {
	Up                          bool
	Timestamp, Duration, Errors float64
	Unmatched                   int
	Groups, Clients             map[Identity]*RTT
	Sources                     map[SourceKey][4]float64
}
type Record struct {
	LocalIP      string     `json:"local_ip"`
	LocalPort    int        `json:"local_port"`
	PeerIP       string     `json:"peer_ip"`
	PeerPort     int        `json:"peer_port"`
	UID          uint32     `json:"uid"`
	Cookie       *[2]uint32 `json:"socket_cookie"`
	RTT          float64    `json:"rtt_seconds"`
	Variation    float64    `json:"variation_seconds"`
	AckAge       float64    `json:"ack_age_seconds"`
	PayloadAge   float64    `json:"payload_idle_seconds"`
	SendQueue    float64    `json:"send_queue_bytes"`
	ReceiveQueue float64    `json:"receive_queue_bytes"`
	Cwnd         *uint32    `json:"cwnd_segments"`
	MSS          *uint32    `json:"send_mss_bytes"`
	Notsent      *uint32    `json:"notsent_bytes"`
	Retrans      *uint32    `json:"total_retransmissions"`
}

func (r *Record) UnmarshalJSON(raw []byte) error {
	type plain Record
	v := plain{AckAge: math.Inf(1), PayloadAge: math.Inf(1)}
	if err := json.Unmarshal(raw, &v); err != nil {
		return err
	}
	*r = Record(v)
	return nil
}

func includeRTT(v *RTT, r Record) {
	v.Count++
	v.Sum += r.RTT
	v.Maximum = math.Max(v.Maximum, r.RTT)
	v.Variation += r.Variation
	v.AckAge = math.Max(v.AckAge, r.AckAge)
	v.PayloadAge = math.Max(v.PayloadAge, r.PayloadAge)
	for i, b := range bounds {
		if r.RTT <= b {
			v.Buckets[i]++
		}
	}
}
func (c *Collector) tcpUpdate(records []Record, now float64) {
	n := TCPState{Up: true, Timestamp: now, Errors: c.tcp.Errors, Groups: map[Identity]*RTT{}, Clients: map[Identity]*RTT{}, Sources: map[SourceKey][4]float64{}}
	type candidate struct {
		key  Identity
		info Inbound
		uid  *uint32
	}
	candidates := []candidate{}
	authenticated := map[Endpoint]bool{}
	following := map[SocketIdentity]uint32{}
	for _, s := range c.config.Services {
		if state := c.states[s.Name]; state != nil && fresh(state.Up, state.Timestamp, now, c.config.Poll) {
			for ep := range state.Endpoints {
				authenticated[ep] = true
			}
		}
		for inbound, info := range s.Inbounds {
			if info.Port == 0 || udpProtocol(info.Type) || info.Network == "udp" {
				continue
			}
			key := Identity{s.Name, info.Type, inbound}
			candidates = append(candidates, candidate{key, info, s.UID})
			n.Groups[key] = &RTT{}
			n.Clients[key] = &RTT{}
		}
	}
	for _, r := range records {
		var key Identity
		matches := 0
		for _, candidate := range candidates {
			bind := strings.TrimPrefix(candidate.info.Listen, "::ffff:")
			if r.LocalPort == candidate.info.Port && (candidate.uid == nil || *candidate.uid == r.UID) && (bind == "" || bind == "::" || bind == "0.0.0.0" || bind == r.LocalIP) {
				key = candidate.key
				matches++
			}
		}
		if matches != 1 {
			n.Unmatched++
			continue
		}
		g := n.Groups[key]
		g.Sockets++
		g.SendQueue += r.SendQueue
		g.ReceiveQueue += r.ReceiveQueue
		if r.Cwnd != nil && r.MSS != nil {
			g.CwndCount++
			g.CwndBytes += float64(*r.Cwnd) * float64(*r.MSS)
		}
		if r.Notsent != nil {
			g.NotsentCount++
			g.Notsent += float64(*r.Notsent)
		}
		if r.Retrans != nil {
			g.RetransCount++
			g.Retrans += float64(*r.Retrans)
			if r.Cookie != nil && *r.Cookie != [2]uint32{0xffffffff, 0xffffffff} {
				id := SocketIdentity{key, *r.Cookie}
				if previous, ok := c.previous[id]; ok && *r.Retrans >= previous {
					c.retrans[key] += float64(*r.Retrans - previous)
				}
				following[id] = *r.Retrans
			}
		}
		if r.RTT <= 0 || r.AckAge > c.config.Idle {
			continue
		}
		includeRTT(g, r)
		if r.PayloadAge <= c.config.Idle {
			if authenticated[Endpoint{key, r.PeerIP, r.PeerPort}] {
				includeRTT(n.Clients[key], r)
			}
			sk := SourceKey{key, source(c.config, r.PeerIP)}
			v := n.Sources[sk]
			v[0]++
			v[1] += r.RTT
			v[2] = math.Max(v[2], r.RTT)
			v[3] = math.Max(v[3], r.AckAge)
			n.Sources[sk] = v
		}
	}
	c.previous = following
	c.tcp = n
}
