package reminders

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestWorkerCancellationAndRestart(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 unavailable")
	}
	script := filepath.Join(t.TempDir(), "worker.py")
	err = os.WriteFile(script, []byte("import sys,json,time\nfor line in sys.stdin:\n r=json.loads(line)\n if r['operation']=='wait': time.sleep(30)\n print(json.dumps({'result':{'ok':True}}),flush=True)\n"), 0600)
	if err != nil {
		t.Fatal(err)
	}
	c := &Client{Python: python, Script: script}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_, err = c.Call(ctx, "wait", nil)
	if typed, ok := err.(*Error); !ok || typed.Code != "outcome_unknown" {
		t.Fatalf("unexpected error: %v", err)
	}
	ctx2, cancel2 := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel2()
	result, err := c.Call(ctx2, "read", nil)
	if err != nil || string(result) != `{"ok": true}` {
		t.Fatalf("restart: %s %v", result, err)
	}
}
