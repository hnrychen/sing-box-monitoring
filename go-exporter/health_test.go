package main

import (
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func TestHealthCheckTLSAuthenticationAndUnavailable(t *testing.T) {
	var status atomic.Int64
	status.Store(200)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/healthz" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer test-token" {
			w.WriteHeader(401)
			return
		}
		w.WriteHeader(int(status.Load()))
	}))
	defer server.Close()
	cert := filepath.Join(t.TempDir(), "server.crt")
	if err := os.WriteFile(cert, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.TLS.Certificates[0].Certificate[0]}), 0600); err != nil {
		t.Fatal(err)
	}
	cfg := defaultConfig()
	cfg.Listen = strings.TrimPrefix(server.URL, "https://")
	cfg.Token = "test-token"
	cfg.Cert = cert
	if err := checkHealth(cfg); err != nil {
		t.Fatal(err)
	}
	status.Store(503)
	if err := checkHealth(cfg); err == nil {
		t.Fatal("accepted unhealthy collector")
	}
	cfg.Token = "wrong-token"
	if err := checkHealth(cfg); err == nil || strings.Contains(err.Error(), cfg.Token) {
		t.Fatal("bad authentication or secret exposed", err)
	}
	cfg.Cert = filepath.Join(t.TempDir(), "missing")
	if err := checkHealth(cfg); err == nil {
		t.Fatal("accepted missing certificate")
	}
}
