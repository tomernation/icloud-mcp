package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ThomasCrouzet/icloud-mcp/internal/security"
	"github.com/mark3labs/mcp-go/mcp"
)

func TestRemoteProtocolAndAuthorization(t *testing.T) {
	oauth := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id, secret, ok := r.BasicAuth()
		if !ok || id != "client" || secret != "synthetic-secret" {
			t.Error("missing introspection client auth")
		}
		r.ParseForm()
		token := r.Form.Get("token")
		subject := "owner"
		if token == "other" {
			subject = "other"
		}
		audience := "https://mcp.example/mcp"
		if token == "wrong-audience" {
			audience = "https://other.example"
		}
		issuer := "https://issuer.example"
		if token == "wrong-issuer" {
			issuer = "https://other.example"
		}
		exp := time.Now().Add(time.Minute).Unix()
		if token == "expired" {
			exp = 1
		}
		scope := "calendar"
		if token == "wrong-scope" {
			scope = "mail"
		}
		json.NewEncoder(w).Encode(map[string]any{"active": token != "inactive", "sub": subject, "aud": audience, "iss": issuer, "exp": exp, "scope": scope})
	}))
	defer oauth.Close()
	c := &remoteConfig{resource: "https://mcp.example/mcp", issuer: "https://issuer.example", introspection: oauth.URL, owner: "owner", clientID: "client", secret: "synthetic-secret", client: oauth.Client()}
	red := security.NewRedactor("synthetic-secret")
	s := newMCPServer(red)
	calls := 0
	s.AddTool(mcp.NewTool("fixture"), func(context.Context, mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		calls++
		return mcp.NewToolResultText("synthetic-result"), nil
	})
	handler := remoteHandler(s, c)
	request := func(token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", c.resource, strings.NewReader(body))
		r.TLS = &tls.ConnectionState{}
		r.RemoteAddr = "192.0.2.1:1234"
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("Accept", "application/json, text/event-stream")
		if token != "" {
			r.Header.Set("Authorization", "Bearer "+token)
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	call := `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fixture","arguments":{}}}`
	for token, status := range map[string]int{"": 401, "inactive": 401, "other": 403, "wrong-audience": 401, "wrong-issuer": 401, "expired": 401, "wrong-scope": 403} {
		w := request(token, call)
		if w.Code != status {
			t.Errorf("%s status %d: %s", token, w.Code, w.Body.String())
		}
		if strings.Contains(w.Body.String(), "synthetic-secret") {
			t.Fatal("secret leak")
		}
	}
	if calls != 0 {
		t.Fatal("unauthorized handler called")
	}
	for _, body := range []string{`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}`, `{"jsonrpc":"2.0","method":"notifications/initialized"}`, `{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`, call} {
		w := request("owner", body)
		if w.Code != 200 && w.Code != 202 {
			t.Fatalf("protocol %d: %s", w.Code, w.Body.String())
		}
		if strings.Contains(w.Body.String(), `"error":`) {
			t.Fatal(w.Body.String())
		}
	}
	if calls != 1 {
		t.Fatalf("calls=%d", calls)
	}
	// Untrusted certificates must fail closed with no introspection response disclosure.
	c.client = &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS13}}, Timeout: time.Second}
	if w := request("owner", call); w.Code != 401 {
		t.Fatalf("invalid certificate status=%d", w.Code)
	}
}

func TestRemoteBoundary(t *testing.T) {
	c := &remoteConfig{resource: "https://mcp.example/mcp", issuer: "https://issuer.example"}
	h := remoteHandler(newMCPServer(security.NewRedactor()), c)
	for _, tc := range []struct {
		path, host, origin string
		plain              bool
		status             int
	}{
		{"/healthz", "mcp.example", "", false, 200},
		{"/.well-known/oauth-protected-resource/mcp", "mcp.example", "", false, 200},
		{"/healthz", "evil.example", "", false, 403},
		{"/healthz", "mcp.example", "https://evil.example", false, 403},
		{"/healthz", "mcp.example", "", true, 400},
	} {
		t.Run(fmt.Sprint(tc), func(t *testing.T) {
			r := httptest.NewRequest("GET", "https://"+tc.host+tc.path, nil)
			if tc.plain {
				r.TLS = nil
			}
			r.Header.Set("Origin", tc.origin)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != tc.status {
				t.Fatal(w.Code)
			}
			if strings.Contains(w.Body.String(), "owner") {
				t.Fatal("private metadata")
			}
		})
	}
}
