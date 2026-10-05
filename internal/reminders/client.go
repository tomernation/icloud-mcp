// Package reminders connects the Go server to the pinned private CloudKit worker.
package reminders

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os/exec"
	"sync"
)

type Service interface {
	Call(context.Context, string, map[string]any) (json.RawMessage, error)
}
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *Error) Error() string { return e.Message }

type Client struct {
	Python, Script, SessionDir, Email, TimeZone string
	gate                                        chan struct{}
	cmd                                         *exec.Cmd
	input                                       io.WriteCloser
	output                                      *bufio.Scanner
	once                                        sync.Once
}

func (c *Client) stop() {
	if c.cmd != nil {
		_ = c.cmd.Process.Kill()
		_ = c.cmd.Wait()
		c.cmd = nil
	}
}
func (c *Client) Close() {
	c.once.Do(func() { c.gate = make(chan struct{}, 1) })
	c.gate <- struct{}{}
	defer func() { <-c.gate }()
	c.stop()
}
func (c *Client) Call(ctx context.Context, operation string, args map[string]any) (json.RawMessage, error) {
	c.once.Do(func() { c.gate = make(chan struct{}, 1) })
	select {
	case c.gate <- struct{}{}:
	case <-ctx.Done():
		return nil, &Error{"timeout", "Timed out waiting for Reminders worker; no request dispatched."}
	}
	defer func() { <-c.gate }()
	if c.cmd == nil {
		c.cmd = exec.Command(c.Python, c.Script, "--session-dir", c.SessionDir, "--email", c.Email, "--timezone", c.TimeZone)
		var err error
		c.input, err = c.cmd.StdinPipe()
		if err != nil {
			c.cmd = nil
			return nil, errors.New("Reminders worker unavailable")
		}
		out, err := c.cmd.StdoutPipe()
		if err != nil {
			c.cmd = nil
			return nil, errors.New("Reminders worker unavailable")
		}
		c.output = bufio.NewScanner(out)
		c.output.Buffer(make([]byte, 4096), 2<<20)
		if err = c.cmd.Start(); err != nil {
			c.cmd = nil
			return nil, errors.New("Reminders worker unavailable")
		}
	}
	request, _ := json.Marshal(map[string]any{"operation": operation, "args": args})
	type outcome struct {
		data []byte
		err  error
	}
	done := make(chan outcome, 1)
	go func() {
		if _, err := c.input.Write(append(request, '\n')); err != nil {
			done <- outcome{err: err}
			return
		}
		if !c.output.Scan() {
			done <- outcome{err: io.ErrUnexpectedEOF}
			return
		}
		done <- outcome{data: append([]byte(nil), c.output.Bytes()...)}
	}()
	var result outcome
	select {
	case result = <-done:
	case <-ctx.Done():
		c.stop()
		<-done
		return nil, &Error{"outcome_unknown", "Reminders deadline reached. Reconcile the target and retain the same idempotency_key before retrying a write."}
	}
	if result.err != nil {
		c.stop()
		return nil, &Error{"outcome_unknown", "Reminders worker interrupted. Reconcile before retrying a write."}
	}
	var response struct {
		Result json.RawMessage `json:"result"`
		Error  *Error          `json:"error"`
	}
	if json.Unmarshal(result.data, &response) != nil {
		c.stop()
		return nil, errors.New("Invalid Reminders worker response")
	}
	if response.Error != nil {
		return nil, response.Error
	}
	return response.Result, nil
}
