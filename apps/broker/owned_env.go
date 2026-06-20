package main

import (
	"os"
	"regexp"
	"strings"
)

var blockedOwnedChildEnvName = regexp.MustCompile(`(?i)(TOKEN|PASSWORD|PASSWD|SECRET|PRIVATE[_-]?KEY|CREDENTIAL|API[_-]?KEY|BROKER[_-]?TENANT[_-]?TOKEN)`)

// forwardedConnectionEnv is the exact-name allowlist of user-connected
// credentials the broker forwards from the container PID-1 env into the
// spawned agent (codex / claude / bash) env.
//
// fleet-task #573: the broker spawns every agent child process with a
// scrubbed, allowlisted env (see ownedChildEnv) — NOT an inherited one.
// That is the correct posture: the broker must not hand the agent its
// own auth token (BROKER_TENANT_TOKEN) or the platform API password.
// Tenant-scoped runtime clients still need ROCKIELAB_TENANT_TOKEN
// because rockie-gpu / mcp-rockie use it as X-Tenant-Token while
// ROCKIELAB_TENANT_ID remains the tenant scope header.
// But it also means a user who connects GitHub / HuggingFace at
// /settings/connections — which lands GH_TOKEN / HF_TOKEN in the
// container env as Fly app secrets — never sees those tokens reach
// `gh` / `hf`, because the block regex below scrubs anything matching
// TOKEN.
//
// These names are user-connected credentials the agent is explicitly
// meant to use (the whole point of the Connections feature). They are
// forwarded here. Membership is EXACT-NAME set lookup, never a regex —
// so this carve-out can never accidentally widen to forward a
// platform-owned secret. BROKER_TENANT_TOKEN, ROCKIELAB_API_PASSWORD,
// CLAUDE_CODE_OAUTH_TOKEN, and every other secret-shaped env var stay
// scrubbed because they are not in this set.
//
// To add a new connection credential: add the connection in
// platform-context (routers/connections.py) AND add its exact env-var
// name here. Both halves are required — layer 1 (Fly app secret) lands
// it in the container env; layer 2 (this allowlist) forwards it to the
// agent.
var forwardedConnectionEnv = map[string]struct{}{
	"GH_TOKEN": {},
	"HF_TOKEN": {},
}

// byokProviderEnv is the exact-name allowlist of BYOK provider env vars
// the broker forwards from the container PID-1 env into the spawned
// nugget (Goose) child. These are the NON-secret coordinates only —
// GOOSE_PROVIDER / GOOSE_MODEL select the provider, OPENAI_BASE_URL
// points Goose at the OpenAI-compatible endpoint. None of them matches
// the block regex, and none is ever set outside nugget BYOK mode (the
// entrypoint exports them only when MODE=nugget_byok), so forwarding
// them unconditionally is a no-op for the subscription / OpenClaw-BYOK /
// open-weights modes.
//
// nugget BYOK (MODE=nugget_byok): the runtime entrypoint translates the
// platform's BYOK contract (BYOK_PROVIDER / BYOK_MODEL_ID + the standard
// provider key) into Goose's provider env and exports it into the
// broker's PID-1 env. The broker spawns nugget with ownedChildEnv() — a
// scrubbed allowlist — so these names must be forwarded explicitly or
// Goose never sees them.
var byokProviderEnv = map[string]struct{}{
	"GOOSE_PROVIDER":  {},
	"GOOSE_MODEL":     {},
	"OPENAI_BASE_URL": {},
}

// byokProviderKeyEnv is the exact-name allowlist of BYOK provider-CREDENTIAL
// env vars. OPENAI_API_KEY / ANTHROPIC_API_KEY match the block regex
// (API_KEY), so they need an exact-name carve-out in
// isEnvNameAllowedForChild — but ONLY for the nugget BYOK path, where the
// spawned nugget (Goose) child must read the tenant's key directly.
//
// This carve-out is gated on MODE=nugget_byok (see isNuggetByokMode) so the
// existing modes stay byte-identical: in subscription / OpenClaw-BYOK /
// open-weights the tenant's ANTHROPIC_API_KEY / OPENAI_API_KEY (a Fly app
// secret set by the wizard) continues to be SCRUBBED from every spawned
// claude / codex / bash PTY exactly as before — those modes never hand the
// raw key to a spawned child (subscription uses OAuth; OpenClaw BYOK reaches
// the gateway over HTTP). Membership is EXACT-NAME set lookup, never a
// regex, so this carve-out can never widen to forward a platform-owned
// secret. This is the generic any-provider mechanism: no provider-specific
// values live here.
var byokProviderKeyEnv = map[string]struct{}{
	"OPENAI_API_KEY":    {},
	"ANTHROPIC_API_KEY": {},
}

const (
	defaultRockielabAPIBase = "https://api.rockielab.com"
	defaultRuntimeBinary    = "codex"
	nuggetByokMode          = "nugget_byok"
)

// isNuggetByokMode reports whether this container is the nugget BYOK
// runtime — the only mode in which the broker forwards the tenant's raw
// provider key to the spawned nugget (Goose) child.
func isNuggetByokMode() bool {
	return strings.TrimSpace(os.Getenv("MODE")) == nuggetByokMode
}

func tenantID() string {
	return strings.TrimSpace(os.Getenv("ROCKIELAB_TENANT_ID"))
}

func ownedChildEnv() []string {
	env := map[string]string{}
	allowed := []string{
		"PATH",
		"HOME",
		"USER",
		"LOGNAME",
		"SHELL",
		"TERM",
		"COLORTERM",
		"LANG",
		"LC_ALL",
		"LC_CTYPE",
		"TZ",
		"TMPDIR",
		"TEMP",
		"TMP",
		"XDG_CONFIG_HOME",
		"XDG_CACHE_HOME",
		"XDG_DATA_HOME",
		"ROCKIELAB_API_BASE",
		"ROCKIELAB_API_URL",
		"ROCKIELAB_TENANT_ID",
		"ROCKIELAB_TENANT_TOKEN",
		"BINARY",
		"BROKER_PORT",
	}
	// User-connected credentials (GH_TOKEN / HF_TOKEN) are copied from
	// the container PID-1 env the same way as the static allowlist
	// above; they are exempted from the block regex below by exact-name
	// lookup against forwardedConnectionEnv.
	for name := range forwardedConnectionEnv {
		allowed = append(allowed, name)
	}
	// BYOK provider coordinates (GOOSE_PROVIDER / GOOSE_MODEL /
	// OPENAI_BASE_URL): non-secret, only ever present in nugget BYOK mode,
	// so always allowed (a no-op for the other modes where they are unset).
	for name := range byokProviderEnv {
		allowed = append(allowed, name)
	}
	// BYOK provider credential (OPENAI_API_KEY / ANTHROPIC_API_KEY): only
	// forwarded to the spawned child in nugget BYOK mode. In every other
	// mode the tenant's raw key stays scrubbed (existing behavior).
	if isNuggetByokMode() {
		for name := range byokProviderKeyEnv {
			allowed = append(allowed, name)
		}
	}
	copyAllowedEnv(env, allowed)
	if env["PATH"] == "" {
		env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
	}
	if env["ROCKIELAB_API_BASE"] == "" {
		env["ROCKIELAB_API_BASE"] = defaultRockielabAPIBase
	}
	if env["ROCKIELAB_API_URL"] == "" {
		env["ROCKIELAB_API_URL"] = env["ROCKIELAB_API_BASE"]
	}
	if env["BINARY"] == "" {
		env["BINARY"] = defaultRuntimeBinary
	}
	if tid := tenantID(); tid != "" {
		env["ROCKIELAB_TENANT_ID"] = tid
	}
	out := make([]string, 0, len(env))
	for key, value := range env {
		if key == "" || value == "" {
			continue
		}
		if !isEnvNameAllowedForChild(key) {
			continue
		}
		out = append(out, key+"="+value)
	}
	return out
}

// isEnvNameAllowedForChild decides whether an env-var NAME may be passed
// to a spawned agent process. A name is allowed unless the block regex
// matches it — EXCEPT for two explicit, exact-name carve-outs:
//
//   - ROCKIELAB_TENANT_TOKEN: a tenant-scoped service token explicitly
//     staged for tenant runtime API calls. It is not the broker token or
//     platform API password.
//   - any name in forwardedConnectionEnv: user-connected credentials
//     the agent is explicitly meant to use.
//
// Both carve-outs are exact-string set membership — never a regex — so
// they cannot widen to cover a platform-owned secret.
func isEnvNameAllowedForChild(key string) bool {
	if key == "ROCKIELAB_TENANT_TOKEN" {
		return true
	}
	if _, ok := forwardedConnectionEnv[key]; ok {
		return true
	}
	// BYOK provider coordinates (GOOSE_*/OPENAI_BASE_URL) do not match the
	// block regex, so they fall through to the default-allow below. Only the
	// BYOK provider KEY names (OPENAI_API_KEY / ANTHROPIC_API_KEY) match the
	// regex and need a carve-out — gated on nugget BYOK mode so existing
	// modes keep scrubbing the tenant's raw key from spawned children.
	if isNuggetByokMode() {
		if _, ok := byokProviderKeyEnv[key]; ok {
			return true
		}
	}
	return !blockedOwnedChildEnvName.MatchString(key)
}

func copyAllowedEnv(out map[string]string, keys []string) {
	for _, key := range keys {
		if value := os.Getenv(key); value != "" {
			out[key] = value
		}
	}
}

func envContainsName(env []string, name string) bool {
	prefix := name + "="
	for _, kv := range env {
		if strings.HasPrefix(kv, prefix) {
			return true
		}
	}
	return false
}
