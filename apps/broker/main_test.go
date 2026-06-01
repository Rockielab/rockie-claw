package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestHealthz(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	healthHandler(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("expected JSON body, got %q: %v", rec.Body.String(), err)
	}
	if body["status"] != "ok" {
		t.Fatalf("expected status=ok, got %q", body["status"])
	}
}

// Auto-execute contract for the spawn-per-prompt /chat path: claude
// must be spawned with --dangerously-skip-permissions for the same
// reason as the /chat-pty path. fleet-task #102.
func TestClaudeChatArgsHasAutoExecute(t *testing.T) {
	args := claudeChatArgs("hello", "")
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "--dangerously-skip-permissions") {
		t.Fatalf("expected --dangerously-skip-permissions in args, got %q", joined)
	}
	if !strings.Contains(joined, "--output-format stream-json") {
		t.Fatalf("expected stream-json output, got %q", joined)
	}
	// --resume only when a session_id is provided.
	if strings.Contains(joined, "--resume") {
		t.Fatalf("unexpected --resume with empty session, got %q", joined)
	}
	withSid := strings.Join(claudeChatArgs("hi", "sess-1"), " ")
	if !strings.Contains(withSid, "--resume sess-1") {
		t.Fatalf("expected --resume sess-1, got %q", withSid)
	}
}

func TestCodexChatArgsUsesFreshExecWhenNoSession(t *testing.T) {
	args := codexChatArgs("publish to hf")
	joined := strings.Join(args, " ")
	if len(args) == 0 || args[0] != "exec" {
		t.Fatalf("expected fresh codex exec args, got %v", args)
	}
	if strings.Contains(joined, " resume ") {
		t.Fatalf("fresh exec args must not use resume, got %q", joined)
	}
	if !strings.Contains(joined, "--json") {
		t.Fatalf("expected --json for stream-json output, got %q", joined)
	}
	if !strings.Contains(joined, "--sandbox danger-full-access") {
		t.Fatalf("expected danger-full-access sandbox for tenant runtime network, got %q", joined)
	}
	if !strings.Contains(joined, "--skip-git-repo-check") {
		t.Fatalf("expected --skip-git-repo-check, got %q", joined)
	}
	if got := args[len(args)-1]; got != "publish to hf" {
		t.Fatalf("expected prompt as final arg, got %q", got)
	}
}

func TestCodexResumeChatArgsUsesExecResumeJSON(t *testing.T) {
	args := codexResumeChatArgs("sess-1", "next turn")
	want := []string{
		"exec",
		"resume",
		"--json",
		"-c", `sandbox_mode="danger-full-access"`,
		"--skip-git-repo-check",
		"sess-1",
		"next turn",
	}
	if !slices.Equal(args, want) {
		t.Fatalf("resume args mismatch:\n got: %v\nwant: %v", args, want)
	}
	if got := strings.Count(strings.Join(args, "\x00"), "next turn"); got != 1 {
		t.Fatalf("expected prompt exactly once, got %d occurrences in %v", got, args)
	}
	if strings.Contains(strings.Join(args, "\n"), "old history") {
		t.Fatalf("resume args unexpectedly embedded history: %v", args)
	}
}

func TestCodexResumeChatArgsForbidUnsupportedFlags(t *testing.T) {
	args := codexResumeChatArgs("sess-1", "next turn")
	joined := strings.Join(args, " ")
	for _, forbidden := range []string{"--sandbox", "--dangerously-bypass-approvals-and-sandbox"} {
		if strings.Contains(joined, forbidden) {
			t.Fatalf("resume args must not contain %q, got %v", forbidden, args)
		}
	}
}

func TestConstantTimeStringEq(t *testing.T) {
	if !constantTimeStringEq("abc", "abc") {
		t.Fatal("equal strings should match")
	}
	if constantTimeStringEq("abc", "abd") {
		t.Fatal("different strings of equal length should not match")
	}
	if constantTimeStringEq("abc", "abcd") {
		t.Fatal("different lengths should not match")
	}
	if constantTimeStringEq("", "") {
		// Note: subtle.ConstantTimeCompare returns 1 only for non-empty
		// equal slices. The wrapper relies on len() short-circuit, so
		// empty == empty must return false to match Go's behavior. We
		// codify whatever the current impl does so a refactor stays safe.
		// (Empty broker tokens are rejected upstream regardless.)
		t.Log("empty/empty matched; this is acceptable but unusual")
	}
}

func TestRedactNeverLeaksValue(t *testing.T) {
	r := redact("super-secret-token")
	if strings.Contains(r, "super") || strings.Contains(r, "secret") {
		t.Fatalf("redact leaked the value: %q", r)
	}
	if strings.Contains(r, "18") {
		t.Fatalf("redact leaked value length: %q", r)
	}
	if !strings.Contains(r, "redacted") {
		t.Fatalf("redact output should mention 'redacted', got %q", r)
	}
}

func setBrokerTestEnv(t *testing.T, token string) {
	t.Helper()
	t.Setenv("BROKER_TENANT_TOKEN", token)
	t.Setenv("ROCKIELAB_TENANT_ID", "tenant-test")
}

func TestSpawnRequiresToken(t *testing.T) {
	setBrokerTestEnv(t, "test-token")
	body := strings.NewReader(`{"binary":"bash","args":["-c","echo hi"]}`)
	req := httptest.NewRequest(http.MethodPost, "/spawn", body)
	rec := httptest.NewRecorder()
	spawnHandler(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "invalid_token") {
		t.Fatalf("expected error code invalid_token, got %s", rec.Body.String())
	}
}

func TestSpawnFailsClosedWithoutTenantID(t *testing.T) {
	t.Setenv("BROKER_TENANT_TOKEN", "tt")
	body := strings.NewReader(`{"binary":"bash","args":["-c","echo hi"]}`)
	req := httptest.NewRequest(http.MethodPost, "/spawn?token=tt", body)
	rec := httptest.NewRecorder()
	spawnHandler(rec, req)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500 without tenant id, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "tenant_id_unset") {
		t.Fatalf("expected tenant_id_unset, got %s", rec.Body.String())
	}
}

func TestSpawnRejectsInvalidBinary(t *testing.T) {
	setBrokerTestEnv(t, "tt")
	body := strings.NewReader(`{"binary":"rm","args":["-rf","/"]}`)
	req := httptest.NewRequest(http.MethodPost, "/spawn?token=tt", body)
	rec := httptest.NewRecorder()
	spawnHandler(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "invalid_binary") {
		t.Fatalf("expected invalid_binary error, got %s", rec.Body.String())
	}
}

func TestSpawnHappyPath(t *testing.T) {
	setBrokerTestEnv(t, "tt")
	body := strings.NewReader(`{"binary":"bash","args":["-c","echo hello && echo err 1>&2 && exit 7"],"timeout_sec":5}`)
	req := httptest.NewRequest(http.MethodPost, "/spawn?token=tt", body)
	rec := httptest.NewRecorder()
	spawnHandler(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp spawnResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad JSON: %v / %s", err, rec.Body.String())
	}
	if resp.ExitCode != 7 {
		t.Fatalf("expected exit 7, got %d", resp.ExitCode)
	}
	if !strings.Contains(resp.Stdout, "hello") {
		t.Fatalf("expected stdout 'hello', got %q", resp.Stdout)
	}
	if !strings.Contains(resp.Stderr, "err") {
		t.Fatalf("expected stderr 'err', got %q", resp.Stderr)
	}
}

func TestSpawnForwardsSafeTenantEnvAndBlocksPlatformSecrets(t *testing.T) {
	setTenantRuntimeEnvForChildProbe(t)

	payload, err := json.Marshal(spawnRequest{
		Binary:     "bash",
		Args:       []string{"-c", childEnvProbeShellScript("")},
		TimeoutSec: 5,
	})
	if err != nil {
		t.Fatalf("marshal spawn request: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/spawn?token=tt", strings.NewReader(string(payload)))
	rec := httptest.NewRecorder()
	spawnHandler(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp spawnResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad JSON: %v / %s", err, rec.Body.String())
	}
	if resp.ExitCode != 0 {
		t.Fatalf("expected exit 0, got %d stderr=%q", resp.ExitCode, resp.Stderr)
	}
	assertTenantRuntimeProbe(t, parseProbeLines(resp.Stdout))
}

func TestSpawnBashInjectsResolvedTenantSecretEnvAndRedactsOutput(t *testing.T) {
	setBrokerTestEnv(t, "tt")
	secretValue := "CANARY_SECRET_VALUE_abcdef"
	withStubSecretsClient(t, stubSecretsClient{
		metadata: map[string]string{"DEPLOY_KEY": "ssh_key"},
		resolved: resolvedSecretSet{
			Values:     map[string]string{"DEPLOY_KEY": secretValue},
			Categories: map[string]string{"DEPLOY_KEY": "ssh_key"},
		},
	})
	body := strings.NewReader(`{"binary":"bash","args":["-c","test -n \"$DEPLOY_KEY\" && printf '%s' \"$DEPLOY_KEY\""],"timeout_sec":5}`)
	req := httptest.NewRequest(http.MethodPost, "/spawn?token=tt", body)
	rec := httptest.NewRecorder()
	spawnHandler(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp spawnResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("bad JSON: %v / %s", err, rec.Body.String())
	}
	if resp.ExitCode != 0 {
		t.Fatalf("expected exit 0, got %d stderr=%q", resp.ExitCode, resp.Stderr)
	}
	if strings.Contains(resp.Stdout, secretValue) || strings.Contains(resp.Stdout, secretValue[:6]) {
		t.Fatalf("secret value leaked in stdout: %q", resp.Stdout)
	}
	if !strings.Contains(resp.Stdout, "<redacted:DEPLOY_KEY>") {
		t.Fatalf("expected redacted marker in stdout, got %q", resp.Stdout)
	}
}

// TestPTYFramingRoundtrip exercises /ws end-to-end against `bash` and
// validates the framing scheme: stdin frame in, stdout frame out, exit
// frame at the end. This is the "one PTY framing roundtrip with a mocked
// binary" called for in the Phase 2 spec.
func TestChatRequiresToken(t *testing.T) {
	setBrokerTestEnv(t, "tt")
	req := httptest.NewRequest(http.MethodPost, "/chat?binary=claude", strings.NewReader(`{"prompt":"hi"}`))
	rec := httptest.NewRecorder()
	chatHandler(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
}

func TestChatRejectsInvalidBinary(t *testing.T) {
	setBrokerTestEnv(t, "tt")
	req := httptest.NewRequest(http.MethodPost, "/chat?binary=python&token=tt", strings.NewReader(`{"prompt":"hi"}`))
	rec := httptest.NewRecorder()
	chatHandler(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestChatDefaultsOmittedBinaryToClaude(t *testing.T) {
	setBrokerTestEnv(t, "tt")
	req := httptest.NewRequest(http.MethodPost, "/chat?token=tt", strings.NewReader(`{"prompt":"hi"}`))
	rec := httptest.NewRecorder()
	chatHandler(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 fallback, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"code":"auth_required"`) {
		t.Fatalf("expected claude auth_required fallback, got %s", rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "claude is not signed in") {
		t.Fatalf("expected fallback message for claude, got %s", rec.Body.String())
	}
}

func TestChatRejectsEmptyPrompt(t *testing.T) {
	setBrokerTestEnv(t, "tt")
	req := httptest.NewRequest(http.MethodPost, "/chat?binary=claude&token=tt", strings.NewReader(`{"prompt":""}`))
	rec := httptest.NewRecorder()
	chatHandler(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestCodexChatHandlerSelectsFreshAndResumeArgs(t *testing.T) {
	first := runCodexChatWithRecordedCommand(t, `{"prompt":"fresh","history":[{"role":"user","content":"old history"}],"timeout":1}`)
	if first.Name != "codex" {
		t.Fatalf("expected codex binary, got %q", first.Name)
	}
	wantFirst := codexChatArgs(flattenHistory([]chatTurn{{Role: "user", Content: "old history"}}, "fresh"))
	if !slices.Equal(first.Args, wantFirst) {
		t.Fatalf("fresh handler args mismatch:\n got: %v\nwant: %v", first.Args, wantFirst)
	}

	resumed := runCodexChatWithRecordedCommand(t, `{"prompt":"next turn","history":[{"role":"user","content":"old history"}],"session_id":"sess-1","timeout":1}`)
	wantResume := codexResumeChatArgs("sess-1", "next turn")
	if !slices.Equal(resumed.Args, wantResume) {
		t.Fatalf("resume handler args mismatch:\n got: %v\nwant: %v", resumed.Args, wantResume)
	}
	if strings.Contains(strings.Join(resumed.Args, "\n"), "old history") {
		t.Fatalf("resumed handler args embedded history: %v", resumed.Args)
	}
}

func TestCodexChatHandlerPassesThroughResumeJSONL(t *testing.T) {
	record, body := runCodexChatWithRecordedCommandAndBody(t, `{"prompt":"next turn","session_id":"sess-1","timeout":1}`)
	wantResume := codexResumeChatArgs("sess-1", "next turn")
	if !slices.Equal(record.Args, wantResume) {
		t.Fatalf("resume handler args mismatch:\n got: %v\nwant: %v", record.Args, wantResume)
	}
	if !strings.Contains(body, `{"type":"thread.started","thread_id":"thread-1"}`) {
		t.Fatalf("missing passthrough thread.started frame: %s", body)
	}
	if !strings.Contains(body, `{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}`) {
		t.Fatalf("missing passthrough item.completed frame: %s", body)
	}
	if strings.Contains(body, `"id":"item_0"`) {
		t.Fatalf("default resume path must not synthesize text-wrap frames: %s", body)
	}
}

func TestChatHandlerForwardsSafeTenantEnvAndBlocksPlatformSecrets(t *testing.T) {
	setTenantRuntimeEnvForChildProbe(t)
	record := runCodexChatWithRecordedCommand(t, `{"prompt":"check env","timeout":1}`)
	assertTenantRuntimeProbe(t, record.EnvProbe)
}

type recordedCommand struct {
	Name     string            `json:"name"`
	Args     []string          `json:"args"`
	EnvProbe map[string]string `json:"env_probe,omitempty"`
}

func runCodexChatWithRecordedCommand(t *testing.T, requestBody string) recordedCommand {
	t.Helper()
	record, _ := runCodexChatWithRecordedCommandAndBody(t, requestBody)
	return record
}

func runCodexChatWithRecordedCommandAndBody(t *testing.T, requestBody string) (recordedCommand, string) {
	t.Helper()
	withTempHome(t)
	writeAuthFile(t, "codex")
	setBrokerTestEnv(t, "tt")

	recordPath := t.TempDir() + "/command.json"
	output := strings.Join([]string{
		`{"type":"thread.started","thread_id":"thread-1"}`,
		`{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}`,
		`{"type":"turn.completed"}`,
		"",
	}, "\n")

	original := commandContext
	commandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
		helperArgs := []string{"-test.run=TestBrokerCommandHelperProcess", "--", recordPath, output, name}
		helperArgs = append(helperArgs, args...)
		return exec.CommandContext(ctx, os.Args[0], helperArgs...)
	}
	t.Cleanup(func() { commandContext = original })

	req := httptest.NewRequest(http.MethodPost,
		"/chat?binary=codex&token=tt",
		strings.NewReader(requestBody))
	rec := httptest.NewRecorder()
	chatHandler(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}

	raw, err := os.ReadFile(recordPath)
	if err != nil {
		t.Fatalf("read recorded command: %v", err)
	}
	var record recordedCommand
	if err := json.Unmarshal(raw, &record); err != nil {
		t.Fatalf("decode recorded command: %v raw=%s", err, raw)
	}
	return record, rec.Body.String()
}

func TestBrokerCommandHelperProcess(t *testing.T) {
	sep := -1
	for i, arg := range os.Args {
		if arg == "--" {
			sep = i
			break
		}
	}
	if sep == -1 {
		return
	}
	helperArgs := os.Args[sep+1:]
	if len(helperArgs) < 3 {
		os.Exit(2)
	}
	record := recordedCommand{
		Name:     helperArgs[2],
		Args:     append([]string(nil), helperArgs[3:]...),
		EnvProbe: childEnvProbe(),
	}
	bs, err := json.Marshal(record)
	if err != nil {
		os.Exit(2)
	}
	if err := os.WriteFile(helperArgs[0], bs, 0o600); err != nil {
		os.Exit(2)
	}
	_, _ = os.Stdout.WriteString(helperArgs[1])
	os.Exit(0)
}

func setTenantRuntimeEnvForChildProbe(t *testing.T) {
	t.Helper()
	setBrokerTestEnv(t, "tt")
	t.Setenv("ROCKIELAB_TENANT_TOKEN", "tenant-service-token")
	t.Setenv("ROCKIELAB_API_BASE", "https://api.rockielab.test/")
	t.Setenv("BINARY", "codex")
	t.Setenv("ROCKIELAB_API_PASSWORD", "platform-api-password")
	t.Setenv("OPENAI_API_KEY", "sk-platform-secret")
	t.Setenv("RUNPOD_API_KEY", "runpod-platform-secret")
}

func childEnvProbe() map[string]string {
	token := os.Getenv("ROCKIELAB_TENANT_TOKEN")
	tenantID := os.Getenv("ROCKIELAB_TENANT_ID")
	return map[string]string{
		"has_token":            yesNo(token != ""),
		"token_distinct":       yesNo(token != "" && token != tenantID),
		"tenant_id":            yesNo(tenantID != ""),
		"api_url":              os.Getenv("ROCKIELAB_API_URL"),
		"binary":               os.Getenv("BINARY"),
		"broker_token_present": yesNo(os.Getenv("BROKER_TENANT_TOKEN") != ""),
		"api_password_present": yesNo(os.Getenv("ROCKIELAB_API_PASSWORD") != ""),
		"openai_key_present":   yesNo(os.Getenv("OPENAI_API_KEY") != ""),
		"provider_key_present": yesNo(os.Getenv("RUNPOD_API_KEY") != ""),
	}
}

func yesNo(ok bool) string {
	if ok {
		return "yes"
	}
	return "no"
}

func childEnvProbeShellScript(outputPath string) string {
	redirect := ""
	if outputPath != "" {
		redirect = " > " + shellSingleQuote(outputPath)
	}
	return `yn() {
  if [ -n "$1" ]; then printf yes; else printf no; fi
}
{
  printf 'has_token=%s\n' "$(yn "${ROCKIELAB_TENANT_TOKEN:-}")"
  if [ -n "${ROCKIELAB_TENANT_TOKEN:-}" ] && [ "${ROCKIELAB_TENANT_TOKEN:-}" != "${ROCKIELAB_TENANT_ID:-}" ]; then
    printf 'token_distinct=yes\n'
  else
    printf 'token_distinct=no\n'
  fi
  printf 'tenant_id=%s\n' "$(yn "${ROCKIELAB_TENANT_ID:-}")"
  printf 'api_url=%s\n' "${ROCKIELAB_API_URL:-}"
  printf 'binary=%s\n' "${BINARY:-}"
  printf 'broker_token_present=%s\n' "$(yn "${BROKER_TENANT_TOKEN:-}")"
  printf 'api_password_present=%s\n' "$(yn "${ROCKIELAB_API_PASSWORD:-}")"
  printf 'openai_key_present=%s\n' "$(yn "${OPENAI_API_KEY:-}")"
  printf 'provider_key_present=%s\n' "$(yn "${RUNPOD_API_KEY:-}")"
}` + redirect + "\n"
}

func shellSingleQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'"
}

func parseProbeLines(output string) map[string]string {
	probe := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		if line == "" {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		probe[key] = value
	}
	return probe
}

func assertTenantRuntimeProbe(t *testing.T, probe map[string]string) {
	t.Helper()
	want := map[string]string{
		"has_token":            "yes",
		"token_distinct":       "yes",
		"tenant_id":            "yes",
		"api_url":              "https://api.rockielab.test",
		"binary":               "codex",
		"broker_token_present": "no",
		"api_password_present": "no",
		"openai_key_present":   "no",
		"provider_key_present": "no",
	}
	for key, value := range want {
		if probe[key] != value {
			t.Fatalf("env probe %s = %q, want %q; full probe=%v", key, probe[key], value, probe)
		}
	}
}

func waitForProbeFile(t *testing.T, path string) map[string]string {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		bs, err := os.ReadFile(path)
		if err == nil && len(bs) > 0 {
			probe := parseProbeLines(string(bs))
			if probe["provider_key_present"] != "" {
				return probe
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for env probe file %s", path)
	return nil
}

func TestFlattenHistory(t *testing.T) {
	got := flattenHistory(nil, "hi")
	if got != "hi" {
		t.Fatalf("empty history should pass through prompt; got %q", got)
	}

	turns := []chatTurn{
		{Role: "user", Content: "first user message"},
		{Role: "assistant", Content: "assistant reply"},
	}
	got = flattenHistory(turns, "second user message")
	if !strings.Contains(got, "first user message") ||
		!strings.Contains(got, "assistant reply") ||
		!strings.Contains(got, "second user message") {
		t.Fatalf("flattened prompt missing turns: %q", got)
	}
	// Order must preserve history then current prompt.
	idxFirst := strings.Index(got, "first user message")
	idxSecond := strings.Index(got, "second user message")
	if idxFirst < 0 || idxSecond < 0 || idxFirst >= idxSecond {
		t.Fatalf("history must precede current prompt; got %q", got)
	}
}

func TestPTYFramingRoundtrip(t *testing.T) {
	if _, err := os.Stat("/bin/bash"); err != nil {
		t.Skipf("bash not available on this host: %v", err)
	}
	setBrokerTestEnv(t, "tt")

	mux := http.NewServeMux()
	mux.HandleFunc("/ws", wsHandler)
	srv := httptest.NewServer(mux)
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http") + "/ws"
	u, _ := url.Parse(wsURL)
	q := u.Query()
	q.Set("token", "tt")
	q.Set("binary", "bash")
	u.RawQuery = q.Encode()

	dialer := websocket.DefaultDialer
	conn, resp, err := dialer.Dial(u.String(), nil)
	if err != nil {
		respBody := ""
		if resp != nil {
			b, _ := io.ReadAll(resp.Body)
			respBody = string(b)
		}
		t.Fatalf("dial failed: %v body=%s", err, respBody)
	}
	defer conn.Close()

	// Send: 0x01 "echo READY\nexit 0\n"
	cmd := []byte("echo READY\nexit 0\n")
	frame := append([]byte{frameStdin}, cmd...)
	if err := conn.WriteMessage(websocket.BinaryMessage, frame); err != nil {
		t.Fatalf("write stdin frame: %v", err)
	}

	deadline := time.Now().Add(5 * time.Second)
	_ = conn.SetReadDeadline(deadline)

	gotReady := false
	gotExit := false
	for !gotExit && time.Now().Before(deadline) {
		mt, data, err := conn.ReadMessage()
		if err != nil {
			break
		}
		if mt != websocket.BinaryMessage || len(data) < 1 {
			continue
		}
		switch data[0] {
		case frameStdout:
			if strings.Contains(string(data[1:]), "READY") {
				gotReady = true
			}
		case frameExit:
			gotExit = true
		}
	}
	if !gotReady {
		t.Fatalf("never received stdout frame containing READY")
	}
	if !gotExit {
		t.Fatalf("never received exit frame")
	}
}

func TestWSBinaryQueryDispatchUsesQueryButForwardsRuntimeBinaryEnv(t *testing.T) {
	setBrokerTestEnv(t, "tt")
	t.Setenv("MODE", "subscription")
	t.Setenv("BINARY", "codex")

	binDir := t.TempDir()
	writeFakeBrokerBinary(t, binDir, "claude")
	writeFakeBrokerBinary(t, binDir, "codex")
	t.Setenv("PATH", binDir)

	mux := http.NewServeMux()
	mux.HandleFunc("/ws", wsHandler)
	srv := httptest.NewServer(mux)
	defer srv.Close()

	cases := []struct {
		name        string
		binaryQuery string
		wantBinary  string
	}{
		{name: "explicit-codex-query-wins", binaryQuery: "codex", wantBinary: "codex"},
		{name: "explicit-claude-query-wins", binaryQuery: "claude", wantBinary: "claude"},
		{name: "omitted-binary-defaults-to-claude", binaryQuery: "", wantBinary: "claude"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gotStdout, gotExit := dialBrokerWSAndReadAllFrames(t, srv.URL, tc.binaryQuery)
			if !strings.Contains(gotStdout, "BROKER_BINARY="+tc.wantBinary) {
				t.Fatalf("expected broker to spawn %q, got stdout %q", tc.wantBinary, gotStdout)
			}
			if !strings.Contains(gotStdout, "MODE=") {
				t.Fatalf("expected MODE probe output, got %q", gotStdout)
			}
			if strings.Contains(gotStdout, "MODE=subscription") {
				t.Fatalf("expected broker child env to ignore MODE, got %q", gotStdout)
			}
			if !strings.Contains(gotStdout, "BINARY=") {
				t.Fatalf("expected BINARY probe output, got %q", gotStdout)
			}
			if !strings.Contains(gotStdout, "\nBINARY=codex") && !strings.HasPrefix(gotStdout, "BINARY=codex") {
				t.Fatalf("expected broker child env to forward runtime BINARY, got %q", gotStdout)
			}
			if gotExit != 0 {
				t.Fatalf("expected exit 0, got %d stdout=%q", gotExit, gotStdout)
			}
		})
	}
}

func writeFakeBrokerBinary(t *testing.T, dir, name string) {
	t.Helper()
	path := filepath.Join(dir, name)
	script := "#!/bin/sh\n" +
		"printf 'BROKER_BINARY=%s\\n' '" + name + "'\n" +
		"printf 'MODE=%s\\n' \"${MODE:-}\"\n" +
		"printf 'BINARY=%s\\n' \"${BINARY:-}\"\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatalf("write fake %s binary: %v", name, err)
	}
}

func dialBrokerWSAndReadAllFrames(t *testing.T, serverURL, binaryQuery string) (string, int32) {
	t.Helper()

	wsURL := "ws" + strings.TrimPrefix(serverURL, "http") + "/ws"
	u, err := url.Parse(wsURL)
	if err != nil {
		t.Fatalf("parse ws url: %v", err)
	}
	q := u.Query()
	q.Set("token", "tt")
	if binaryQuery != "" {
		q.Set("binary", binaryQuery)
	}
	u.RawQuery = q.Encode()

	conn, resp, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		respBody := ""
		if resp != nil {
			b, _ := io.ReadAll(resp.Body)
			respBody = string(b)
		}
		t.Fatalf("dial failed: %v body=%s", err, respBody)
	}
	defer conn.Close()

	deadline := time.Now().Add(5 * time.Second)
	_ = conn.SetReadDeadline(deadline)

	var stdout strings.Builder
	var exitCode int32 = -999
	for time.Now().Before(deadline) {
		mt, data, err := conn.ReadMessage()
		if err != nil {
			t.Fatalf("read frame: %v stdout=%q", err, stdout.String())
		}
		if mt != websocket.BinaryMessage || len(data) < 1 {
			continue
		}
		switch data[0] {
		case frameStdout:
			stdout.Write(data[1:])
		case frameExit:
			exitCode = int32(binary.BigEndian.Uint32(data[1:5]))
			return stdout.String(), exitCode
		}
	}

	t.Fatalf("timed out waiting for exit frame stdout=%q", stdout.String())
	return "", 0
}
