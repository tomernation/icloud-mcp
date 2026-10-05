package mcptools

import (
	"context"
	"encoding/json"
	"io"
	"testing"

	"github.com/ThomasCrouzet/icloud-mcp/internal/icloud"
	"github.com/ThomasCrouzet/icloud-mcp/internal/security"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

type fakeReminders struct{ calls int }

func (f *fakeReminders) Call(context.Context, string, map[string]any) (json.RawMessage, error) {
	f.calls++
	return json.RawMessage(`{"id":"r1"}`), nil
}
func TestRemindersRegistrationGates(t *testing.T) {
	for _, writes := range []bool{false, true} {
		s := server.NewMCPServer("test", "test")
		f := &fakeReminders{}
		deps := Deps{RemindersService: f, Redactor: security.NewRedactor("unused", "unused-password"), Audit: security.NewAuditLogger(io.Discard)}
		got := registerReminders(s, deps, writes)
		if len(got) != len(remindersToolNames(writes)) {
			t.Fatal(got)
		}
		if _, ok := s.ListTools()["create_reminder"]; ok != writes {
			t.Fatal("read-only registration bypass")
		}
	}
}
func TestReminderValidationBeforeService(t *testing.T) {
	f := &fakeReminders{}
	deps := Deps{RemindersService: f, Redactor: security.NewRedactor("unused", "unused-password"), Audit: security.NewAuditLogger(io.Discard)}
	cases := []map[string]any{{"list_id": "l1", "title": "Test"}, {"list_id": "l1", "title": "Test", "idempotency_key": "k", "priority": 2}, {"list_id": "l1", "title": "Test", "idempotency_key": "k", "due_date": "2026-02-30"}, {"list_id": "l1", "title": "Test", "idempotency_key": "k", "unexpected": true}}
	for _, args := range cases {
		req := mcp.CallToolRequest{}
		req.Params.Arguments = args
		out, err := reminderHandler("create_reminder", deps)(context.Background(), req)
		if err != nil || !out.IsError {
			t.Fatal("invalid mutation accepted")
		}
	}
	if f.calls != 0 {
		t.Fatal("validation dispatched a request")
	}
}

func TestRemindersUnifiedManifest(t *testing.T) {
	for _, readOnly := range []bool{true, false} {
		s := server.NewMCPServer("test", "test")
		deps := Deps{Service: &icloud.MockService{}, RemindersService: &fakeReminders{}, Redactor: security.NewRedactor("unused"), Audit: security.NewAuditLogger(io.Discard)}
		plan := NewCapabilityPlan(readOnly, false, false, false, false).WithReminders(true)
		got := RegisterUnified(s, deps, plan)
		if len(got) != plan.ToolCount() {
			t.Fatal("manifest mismatch")
		}
		res, err := icloudCapabilitiesHandler(deps, plan)(context.Background(), mcp.CallToolRequest{})
		if err != nil || res.IsError {
			t.Fatal("capabilities failed")
		}
		var payload icloudCapabilitiesResponse
		if err := json.Unmarshal([]byte(res.Content[0].(mcp.TextContent).Text), &payload); err != nil {
			t.Fatal(err)
		}
		if !payload.Domains.Reminders || payload.CapabilityGroups.RemindersWrite == readOnly {
			t.Fatal("incorrect reminders capabilities")
		}
	}
}

func TestRepeatValidation(t *testing.T) {
	for _, args := range []map[string]any{
		{"repeat_frequency": "weekly", "due_date": "2026-10-11", "repeat_interval": float64(2)},
		{"repeat_frequency": "none"},
	} {
		args["reminder_id"] = "r1"
		args["idempotency_key"] = "k"
		if err := validateReminderArgs("update_reminder", args); err != nil {
			t.Fatal(err)
		}
	}
	for _, args := range []map[string]any{
		{"repeat_frequency": "invalid"}, {"repeat_interval": float64(2)},
		{"repeat_frequency": "daily", "repeat_interval": float64(0)},
		{"repeat_frequency": "weekly", "due_date": ""},
	} {
		args["reminder_id"] = "r1"
		args["idempotency_key"] = "k"
		if err := validateReminderArgs("update_reminder", args); err == nil {
			t.Fatal("invalid repeat accepted", args)
		}
	}
}
