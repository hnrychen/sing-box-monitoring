package main

import (
	"strings"
	"testing"
)

func TestRecentActivityRollupsExpireAtRenderAndMatchSources(t *testing.T) {
	cfg := defaultConfig()
	svc := Service{Name: "core", Inbounds: map[string]Inbound{"ss": {Type: "shadowsocks", Port: 443}}}
	cfg.Services = []Service{svc}
	c := newCollector(cfg)
	mk := func(id, network, start string) map[string]any {
		return map[string]any{"id": id, "upload": float64(10), "download": float64(20), "start": start, "metadata": map[string]any{"type": "shadowsocks/ss", "network": network, "sourceIP": "192.0.2.1"}}
	}
	old := mk("old", "tcp", "1970-01-01T00:01:40Z")
	fresh := mk("new", "udp", "1970-01-01T00:16:40Z")
	data := Snapshot{Upload: 100, Download: 200, Connections: []map[string]any{old, fresh}}
	check := func(now float64, want string) {
		t.Helper()
		text := string(c.render(now))
		if !strings.Contains(text, "singbox_recent_connections{service=\"core\"} "+want+"\n") {
			t.Fatal(text)
		}
	}
	if err := c.update(svc, data, 1000); err != nil {
		t.Fatal(err)
	}
	check(1000, "1")
	old["download"] = float64(21)
	if err := c.update(svc, data, 1020); err != nil {
		t.Fatal(err)
	}
	check(1020, "2")
	if err := c.update(svc, data, 1320); err != nil {
		t.Fatal(err)
	}
	check(1320, "1")
	check(1320.001, "0")
	data.Connections = nil
	if err := c.update(svc, data, 1321); err != nil {
		t.Fatal(err)
	}
	check(1321, "0")
	if len(c.states[svc.Name].RouteActivity) != 0 {
		t.Fatal("closed connections retained")
	}
	c.states[svc.Name].Up = false
	if strings.Contains(string(c.render(1321)), "\nsingbox_recent_connections{") {
		t.Fatal("failed collection reported as idle zero")
	}
}
