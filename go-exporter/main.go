package main

import (
	"context"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

func readConfig(path string) (Config, error) {
	c := defaultConfig()
	raw, err := os.ReadFile(path)
	if err != nil {
		return c, err
	}
	if err = json.Unmarshal(raw, &c); err != nil {
		return c, fmt.Errorf("invalid configuration JSON")
	}
	if c.Token == "" || c.Cert == "" || c.Key == "" || len(c.Services) == 0 || len(c.Services) > 32 || c.MaxConnections < 1 || c.MaxConnections > 100000 || c.Idle < 0 {
		return c, fmt.Errorf("invalid configuration limits or missing authentication/TLS")
	}
	for _, n := range []float64{c.Poll, c.TCPPoll, c.ProcessPoll, c.Idle, c.SourceTTL} {
		if math.IsNaN(n) || math.IsInf(n, 0) || n < 0 {
			return c, fmt.Errorf("invalid configuration interval")
		}
	}
	if c.SourceMode != "off" && c.SourceMode != "raw" && c.SourceMode != "subnet" {
		return c, fmt.Errorf("invalid source mode")
	}
	if _, _, err := net.SplitHostPort(c.Listen); err != nil {
		return c, fmt.Errorf("invalid listen address")
	}
	names := map[string]bool{}
	ports := map[int]bool{}
	for _, s := range c.Services {
		u, err := url.Parse(s.URL)
		if err != nil || u.Scheme != "http" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || s.Secret == "" {
			return c, fmt.Errorf("API must use authenticated loopback HTTP")
		}
		ip := net.ParseIP(u.Hostname())
		if ip == nil || !ip.IsLoopback() {
			return c, fmt.Errorf("API must use loopback IP")
		}
		if s.Name == "" || strings.HasPrefix(s.Name, "-") || strings.ContainsAny(s.Name, "\n\r\x00") || names[s.Name] {
			return c, fmt.Errorf("invalid/duplicate service identity")
		}
		names[s.Name] = true
		for _, info := range s.Inbounds {
			if info.Port < 0 || info.Port > 65535 {
				return c, fmt.Errorf("invalid listener port")
			}
			if info.Port > 0 && !udpProtocol(info.Type) && info.Network != "udp" {
				ports[info.Port] = true
			}
		}
	}
	if len(ports) > 32 {
		return c, fmt.Errorf("TCP port budget exceeded")
	}
	return c, nil
}
func apiClient() *http.Client {
	return &http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: 3 * time.Second}).DialContext, MaxIdleConns: 32, MaxIdleConnsPerHost: 1, IdleConnTimeout: 90 * time.Second, MaxResponseHeaderBytes: 32768, DisableCompression: true}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}
func fetch(client *http.Client, s Service) (Snapshot, error) {
	req, err := http.NewRequest("GET", strings.TrimRight(s.URL, "/")+"/connections", nil)
	if err != nil {
		return Snapshot{}, err
	}
	req.Header.Set("Authorization", "Bearer "+s.Secret)
	response, err := client.Do(req)
	if err != nil {
		return Snapshot{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return Snapshot{}, fmt.Errorf("API status")
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxResponse+1))
	if err != nil {
		return Snapshot{}, err
	}
	if len(raw) > maxResponse {
		return Snapshot{}, fmt.Errorf("snapshot response exceeds byte budget")
	}
	return decodeSnapshot(raw)
}
func processValues(name string) map[string]float64 {
	values := map[string]float64{"collected": stamp()}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	raw, err := exec.CommandContext(ctx, "systemctl", "show", name, "-p", "MainPID", "-p", "ActiveState").Output()
	if err != nil {
		return values
	}
	fields := map[string]string{}
	for _, line := range strings.Split(string(raw), "\n") {
		k, v, ok := strings.Cut(line, "=")
		if ok {
			fields[k] = v
		}
	}
	values["unit_active"] = boolNumber(fields["ActiveState"] == "active")
	pid, err := strconv.Atoi(fields["MainPID"])
	if err != nil || pid <= 0 {
		return values
	}
	stat, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return values
	}
	i := strings.LastIndex(string(stat), ")")
	if i < 0 {
		return values
	}
	parts := strings.Fields(string(stat)[i+1:])
	if len(parts) <= 21 {
		return values
	}
	// Linux USER_HZ is 100 on the supported amd64/arm64 builds, distinct from kernel HZ.
	ticks := float64(100)
	numbers := map[int]float64{}
	for _, index := range []int{11, 12, 17, 19, 21} {
		n, e := strconv.ParseFloat(parts[index], 64)
		if e != nil {
			return values
		}
		numbers[index] = n
	}
	uptime, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return values
	}
	up, err := strconv.ParseFloat(strings.Fields(string(uptime))[0], 64)
	if err != nil {
		return values
	}
	values["cpu_seconds_total"] = (numbers[11] + numbers[12]) / ticks
	values["resident_memory_bytes"] = numbers[21] * float64(os.Getpagesize())
	values["threads"] = numbers[17]
	values["uptime_seconds"] = math.Max(0, up-numbers[19]/ticks)
	return values
}
func (c *Collector) poll(s Service, client *http.Client) {
	started := time.Now()
	c.mu.Lock()
	cached := c.processes[s.Name]
	due := stamp()-cached["collected"] >= c.config.ProcessPoll
	c.mu.Unlock()
	if due {
		values := processValues(s.Name)
		c.mu.Lock()
		c.processes[s.Name] = values
		c.mu.Unlock()
	}
	data, err := fetch(client, s)
	c.mu.Lock()
	defer c.mu.Unlock()
	if err == nil {
		err = c.update(s, data, stamp())
	}
	if err != nil {
		state := c.states[s.Name]
		if state == nil {
			state = newState()
			c.states[s.Name] = state
		}
		state.Up = false
		state.Errors++
		log.Printf("%s API collection failed", s.Name)
	}
	c.states[s.Name].Duration = time.Since(started).Seconds()
}
func (c *Collector) tcpPoll() {
	started := time.Now()
	ports := map[int]bool{}
	for _, s := range c.config.Services {
		for _, info := range s.Inbounds {
			if info.Port > 0 && !udpProtocol(info.Type) && info.Network != "udp" {
				ports[info.Port] = true
			}
		}
	}
	records, err := socketSnapshot(ports, c.config.MaxConnections)
	c.mu.Lock()
	defer c.mu.Unlock()
	if err == nil {
		c.tcpUpdate(records, stamp())
	} else {
		c.tcp.Up = false
		c.tcp.Errors++
		log.Print("TCP socket collection failed")
	}
	c.tcp.Duration = time.Since(started).Seconds()
}
func worker(ctx context.Context, interval float64, fn func()) {
	duration := time.Duration(math.Max(2, interval) * float64(time.Second))
	for {
		start := time.Now()
		fn()
		timer := time.NewTimer(max(200*time.Millisecond, duration-time.Since(start)))
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}
func (c *Collector) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Connection", "close")
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+c.config.Token)) != 1 {
			http.Error(w, "Unauthorized", 401)
			return
		}
		if r.Method != "GET" {
			http.Error(w, "Method not allowed", 405)
			return
		}
		var body []byte
		status := 200
		contentType := "text/plain; charset=utf-8"
		now := stamp()
		switch r.URL.RequestURI() {
		case "/metrics":
			body = c.render(now)
			contentType = "text/plain; version=0.0.4; charset=utf-8"
		case "/connections":
			if !c.config.Details {
				http.NotFound(w, r)
				return
			}
			body, _ = json.Marshal(c.connections(now))
			contentType = "application/json; charset=utf-8"
		case "/healthz":
			if c.healthy(now) {
				body = []byte("ok\n")
			} else {
				status = 503
				body = []byte("collection failed\n")
			}
		default:
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", contentType)
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		w.WriteHeader(status)
		_, _ = w.Write(body)
	})
}
func ownUsage() (float64, float64) {
	var usage syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_SELF, &usage) != nil {
		return 0, 0
	}
	return float64(usage.Utime.Sec+usage.Stime.Sec) + float64(usage.Utime.Usec+usage.Stime.Usec)/1e6, float64(usage.Maxrss) * 1024
}
func main() {
	path := flag.String("config", "/etc/sing-box-exporter/config.json", "Configuration path")
	showVersion := flag.Bool("version", false, "Print build version")
	fixture := flag.String("fixture", "", "Offline parity fixture (no network)")
	healthcheck := flag.Bool("healthcheck", false, "Check the local authenticated HTTPS endpoint and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}
	if *fixture != "" {
		if err := runFixture(*fixture); err != nil {
			log.Fatal(err)
		}
		return
	}
	cfg, err := readConfig(*path)
	if err != nil {
		log.Fatal(err)
	}
	if *healthcheck {
		if err := checkHealth(cfg); err != nil {
			log.Fatal(err)
		}
		return
	}
	collector := newCollector(cfg)
	raw, err := os.ReadFile(cfg.Cert)
	if err != nil {
		log.Fatal("cannot read TLS certificate")
	}
	block, _ := pem.Decode(raw)
	if block == nil {
		log.Fatal("invalid TLS certificate")
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		log.Fatal("invalid TLS certificate")
	}
	collector.certExpiry = float64(cert.NotAfter.Unix())
	// The service's TasksMax and soft Go heap limit bound requests/allocations;
	// serving is capped too, so slow authenticated readers cannot fan out without limit.
	listener, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		log.Fatal("cannot bind exporter")
	}
	defer listener.Close()
	server := &http.Server{Handler: collector.handler(), ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second, IdleTimeout: 3 * time.Second, MaxHeaderBytes: 32768, TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12}, ErrorLog: log.New(io.Discard, "", 0)}
	// One request per connection, as in the original collector. Disable automatic
	// HTTP/2 multiplexing so the listener's connection cap also caps handlers.
	server.TLSNextProto = map[string]func(*http.Server, *tls.Conn, http.Handler){}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	for _, s := range cfg.Services {
		s := s
		client := apiClient()
		go worker(ctx, cfg.Poll, func() { collector.poll(s, client) })
	}
	if cfg.TCP {
		go worker(ctx, cfg.TCPPoll, collector.tcpPoll)
	}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	log.Printf("sing-box exporter %s listening at %s", version, cfg.Listen)
	err = server.ServeTLS(&boundedListener{Listener: listener, slots: make(chan struct{}, 16)}, cfg.Cert, cfg.Key)
	if err != nil && err != http.ErrServerClosed {
		log.Fatal("exporter HTTP server failed")
	}
}

type boundedListener struct {
	net.Listener
	slots chan struct{}
}
type boundedConn struct {
	net.Conn
	release func()
	once    sync.Once
}

func (c *boundedConn) Close() error { err := c.Conn.Close(); c.once.Do(c.release); return err }
func (l *boundedListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	select {
	case l.slots <- struct{}{}:
		return &boundedConn{Conn: conn, release: func() { <-l.slots }}, nil
	default:
		_ = conn.Close()
		return nil, tempError{}
	}
}

type tempError struct{}

func (tempError) Error() string   { return "connection limit" }
func (tempError) Timeout() bool   { return false }
func (tempError) Temporary() bool { return true }
