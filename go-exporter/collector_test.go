package main

import (
	"encoding/binary"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestDecodeRejectsInvalidSnapshots(t *testing.T) {
	for _, raw := range []string{`{}`, `{"uploadTotal":null,"downloadTotal":0,"memory":0}`, `{"uploadTotal":-1,"downloadTotal":0,"memory":0}`, `{"uploadTotal":0,"downloadTotal":0,"memory":0,"connections":{}}`} {
		if _, err := decodeSnapshot([]byte(raw)); err == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
	if _, err := decodeSnapshot([]byte(`{"uploadTotal":0,"downloadTotal":0,"memory":0,"connections":null}`)); err != nil {
		t.Fatal(err)
	}
}

func TestRejectMalformedNumbersAndMissingACK(t *testing.T) {
	for _, value := range []string{"NaN", "Infinity", "1.5"} {
		if _, err := number(value); err == nil {
			t.Fatal("accepted non-integer string", value)
		}
	}
	var r Record
	if err := json.Unmarshal([]byte(`{"local_ip":"127.0.0.1","local_port":443,"peer_ip":"203.0.113.1","peer_port":123,"rtt_seconds":0.01}`), &r); err != nil {
		t.Fatal(err)
	}
	cfg := defaultConfig()
	cfg.TCP = true
	cfg.Services = []Service{{Name: "core", Inbounds: map[string]Inbound{"ss": {Type: "shadowsocks", Port: 443}}}}
	c := newCollector(cfg)
	c.tcpUpdate([]Record{r}, 1)
	if c.tcp.Groups[Identity{"core", "shadowsocks", "ss"}].Count != 0 {
		t.Fatal("missing ACK accepted as fresh")
	}
}
func TestHTTPAuthenticationAndStaleness(t *testing.T) {
	cfg := defaultConfig()
	cfg.Token = "private-test-token"
	cfg.Details = true
	cfg.Services = []Service{{Name: "core"}}
	c := newCollector(cfg)
	for _, tc := range []struct {
		path, token string
		status      int
	}{{"/healthz", "", 401}, {"/healthz", "Bearer private-test-token", 503}, {"/connections", "Bearer private-test-token", 200}, {"/metrics", "Bearer private-test-token", 200}, {"/metrics?foo=1", "Bearer private-test-token", 404}} {
		request := httptest.NewRequest("GET", tc.path, nil)
		request.Header.Set("Authorization", tc.token)
		response := httptest.NewRecorder()
		c.handler().ServeHTTP(response, request)
		if response.Code != tc.status {
			t.Fatalf("%s: %d", tc.path, response.Code)
		}
	}
	c.states["core"] = &State{Up: true, Timestamp: stamp() - 1000}
	if c.healthy(stamp()) {
		t.Fatal("stale healthy")
	}
	if strings.Contains(string(c.render(stamp())), "singbox_traffic_bytes_total{") {
		t.Fatal("stale traffic exposed")
	}
}
func TestAPIRejectsRedirectAndIgnoresProxy(t *testing.T) {
	hit := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hit = true }))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target.URL, 302) }))
	defer redirect.Close()
	t.Setenv("HTTP_PROXY", target.URL)
	if _, err := fetch(apiClient(), Service{URL: redirect.URL, Secret: "secret"}); err == nil || hit {
		t.Fatal("redirect followed")
	}
}
func TestLengthGatedTCPAndDumpRejection(t *testing.T) {
	payload := make([]byte, 72+80)
	payload[0] = 2
	payload[1] = 1
	binary.BigEndian.PutUint16(payload[4:6], 443)
	copy(payload[8:12], []byte{127, 0, 0, 1})
	copy(payload[24:28], []byte{203, 0, 113, 10})
	native.PutUint16(payload[72:74], 80)
	native.PutUint16(payload[74:76], 2)
	native.PutUint32(payload[76+68:76+72], 10000)
	r, err := decodeRecord(payload)
	if err != nil || r.RTT != .01 || r.Cwnd != nil || r.Notsent != nil {
		t.Fatalf("%+v %v", r, err)
	}
	native.PutUint16(payload[72:74], 81)
	if _, err := decodeRecord(payload); err == nil {
		t.Fatal("attribute overflow accepted")
	}
	header := make([]byte, 16)
	native.PutUint32(header[:4], 16)
	native.PutUint32(header[8:12], 2)
	rows := []Record{}
	if _, err := parsePacket(header, 1, 2, 443, &rows, 10); err == nil {
		t.Fatal("wrong sequence")
	}
	native.PutUint32(header[8:12], 1)
	native.PutUint16(header[6:8], 0x10)
	if _, err := parsePacket(header, 1, 2, 443, &rows, 10); err == nil {
		t.Fatal("interrupted dump")
	}
	if _, err := socketSnapshot(map[int]bool{0: true}, 10); err == nil {
		t.Fatal("invalid port accepted")
	}
}
func TestSnapshotBudgetsAndTransactionalUpdate(t *testing.T) {
	cfg := defaultConfig()
	cfg.MaxConnections = 1
	c := newCollector(cfg)
	s := Service{Name: "core"}
	if err := c.update(s, Snapshot{Connections: []map[string]any{{}, {}}}, 1); err == nil {
		t.Fatal("truncated snapshot accepted")
	}
	if len(c.states) != 0 {
		t.Fatal("failed update published")
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(strings.Repeat("x", maxResponse+1))) }))
	defer server.Close()
	if _, err := fetch(apiClient(), Service{URL: server.URL}); err == nil {
		t.Fatal("oversize API accepted")
	}
}
