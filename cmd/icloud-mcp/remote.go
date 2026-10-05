package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/mark3labs/mcp-go/server"
)

// OAuth is supplied by an external authorization server, never by this binary.
// RFC 7662 introspection is deliberately uncached so revocation is immediate.
type remoteConfig struct {
	address, resource, issuer, introspection, owner, clientID, secret, cert, key string
	client                                                                       *http.Client
}

func loadRemoteConfig(address string) (*remoteConfig, error) {
	if address == "" {
		return nil, nil
	}
	c := &remoteConfig{address: address, resource: os.Getenv("MCP_RESOURCE"), issuer: os.Getenv("MCP_ISSUER"), introspection: os.Getenv("MCP_INTROSPECTION_URL"), owner: os.Getenv("MCP_OWNER_SUB"), clientID: os.Getenv("MCP_INTROSPECTION_CLIENT_ID"), cert: os.Getenv("MCP_TLS_CERT"), key: os.Getenv("MCP_TLS_KEY")}
	for _, value := range []string{c.resource, c.issuer, c.introspection} {
		u, e := url.Parse(value)
		if e != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return nil, errors.New("remote configuration requires HTTPS resource, issuer and introspection URLs")
		}
	}
	resourceURL, _ := url.Parse(c.resource)
	if resourceURL.Path != "/mcp" {
		return nil, errors.New("MCP_RESOURCE must end in /mcp")
	}
	if c.owner == "" || c.clientID == "" || c.cert == "" || c.key == "" {
		return nil, errors.New("remote configuration missing owner, OAuth client or TLS paths")
	}
	// Secret must be a bounded, owner-only regular file mounted by the secrets manager.
	f, e := os.Open(os.Getenv("MCP_INTROSPECTION_SECRET_FILE"))
	if e != nil {
		return nil, errors.New("cannot open OAuth secret file")
	}
	defer f.Close()
	st, e := f.Stat()
	if e != nil || !st.Mode().IsRegular() || st.Mode().Perm()&0077 != 0 || st.Size() > 4096 {
		return nil, errors.New("OAuth secret file must be regular, <=4096 bytes and owner-only")
	}
	b, e := io.ReadAll(io.LimitReader(f, 4097))
	if e != nil || len(b) > 4096 {
		return nil, errors.New("cannot read OAuth secret")
	}
	c.secret = strings.TrimSpace(string(b))
	if c.secret == "" {
		return nil, errors.New("empty OAuth secret")
	}
	c.client = &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS13}, MaxConnsPerHost: 16}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	if _, e = tls.LoadX509KeyPair(c.cert, c.key); e != nil {
		return nil, errors.New("cannot load remote TLS certificate/key")
	}
	return c, nil
}

type tokenInfo struct {
	Active   bool            `json:"active"`
	Subject  string          `json:"sub"`
	Issuer   string          `json:"iss"`
	Audience json.RawMessage `json:"aud"`
	Scope    string          `json:"scope"`
	Exp      int64           `json:"exp"`
}

func (c *remoteConfig) authorize(r *http.Request) int {
	h := r.Header.Values("Authorization")
	if len(h) != 1 || !strings.HasPrefix(h[0], "Bearer ") {
		return http.StatusUnauthorized
	}
	token := strings.TrimPrefix(h[0], "Bearer ")
	if token == "" || len(token) > 8192 || strings.ContainsAny(token, " \t\r\n") {
		return http.StatusUnauthorized
	}
	req, e := http.NewRequestWithContext(r.Context(), http.MethodPost, c.introspection, strings.NewReader(url.Values{"token": {token}, "token_type_hint": {"access_token"}}.Encode()))
	if e != nil {
		return http.StatusUnauthorized
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.SetBasicAuth(c.clientID, c.secret)
	resp, e := c.client.Do(req)
	if e != nil {
		return http.StatusUnauthorized
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return http.StatusUnauthorized
	}
	b, e := io.ReadAll(io.LimitReader(resp.Body, 16385))
	if e != nil || len(b) > 16384 {
		return http.StatusUnauthorized
	}
	var info tokenInfo
	if json.Unmarshal(b, &info) != nil || !info.Active || info.Issuer != c.issuer || info.Exp <= time.Now().Unix() {
		return http.StatusUnauthorized
	}
	var audience string
	var audiences []string
	match := json.Unmarshal(info.Audience, &audience) == nil && audience == c.resource
	if json.Unmarshal(info.Audience, &audiences) == nil {
		for _, a := range audiences {
			match = match || a == c.resource
		}
	}
	if !match {
		return http.StatusUnauthorized
	}
	if info.Subject != c.owner {
		return http.StatusForbidden
	}
	for _, scope := range strings.Fields(info.Scope) {
		if scope == "calendar" {
			return 0
		}
	}
	return http.StatusForbidden
}

func remoteHandler(s *server.MCPServer, c *remoteConfig) http.Handler {
	transport := server.NewStreamableHTTPServer(s, server.WithStateLess(true), server.WithDisableStreaming(true))
	u, _ := url.Parse(c.resource)
	metadata := u.Scheme + "://" + u.Host + "/.well-known/oauth-protected-resource"
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.TLS == nil {
			http.Error(w, "TLS required", 400)
			return
		}
		if r.Host != u.Host {
			http.Error(w, "invalid host", 403)
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && origin != u.Scheme+"://"+u.Host {
			http.Error(w, "invalid origin", 403)
			return
		}
		switch r.URL.Path {
		case "/healthz":
			if r.Method != http.MethodGet {
				w.WriteHeader(405)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			io.WriteString(w, `{"status":"ok"}`)
			return
		case "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp":
			if r.Method != http.MethodGet {
				w.WriteHeader(405)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]any{"resource": c.resource, "authorization_servers": []string{c.issuer}, "scopes_supported": []string{"calendar"}, "bearer_methods_supported": []string{"header"}})
			return
		case "/mcp":
			if r.Method != http.MethodPost && r.Method != http.MethodGet && r.Method != http.MethodDelete {
				w.WriteHeader(405)
				return
			}
			if status := c.authorize(r); status != 0 {
				w.Header().Set("WWW-Authenticate", `Bearer resource_metadata="`+metadata+`", scope="calendar"`)
				http.Error(w, http.StatusText(status), status)
				return
			}
			r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
			transport.ServeHTTP(w, r)
		default:
			http.NotFound(w, r)
		}
	})
}

func serveRemote(s *server.MCPServer, c *remoteConfig) error {
	srv := &http.Server{Addr: c.address, Handler: remoteHandler(s, c), TLSConfig: &tls.Config{MinVersion: tls.VersionTLS13}, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			shutdown, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			srv.Shutdown(shutdown)
		case <-done:
		}
	}()
	err := srv.ListenAndServeTLS(c.cert, c.key)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}
