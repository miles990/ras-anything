// Package ras_hardened provides the standalone Go resilience, observability,
// RFC 9457 error, human-gate, and flight-recorder runtime vendored by `ras-anything enable`.
package ras_hardened

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	mrand "math/rand/v2"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Generated from ras_anything.contracts.SCHEMA_VERSION by write_schema_version(). Do not edit by hand.
const SchemaVersion = "1.1.0"

// --------------------------------------------------------------------------- masking & plain

var (
	reEmail   = regexp.MustCompile(`[\w.+-]+@[\w-]+(?:\.[\w-]+)+`)
	reAuth    = regexp.MustCompile(`(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}`)
	reJWT     = regexp.MustCompile(`\beyJ[\w-]{8,}\.[\w-]+\.[\w-]*`)
	rePrefix  = regexp.MustCompile(`\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|AKIA|ASIA|AIza)[-_A-Za-z0-9]{12,}`)
	reSecret  = regexp.MustCompile(`(?i)\b(api[_-]?key|access[_-]?key|token|secret|passw(?:or)?d|pwd|authorization)\s*([=:])\s*([^\s"'&,;}]+)`)
	reCard    = regexp.MustCompile(`\b(?:\d[ -]?){13,19}\b`)
	rePhone   = regexp.MustCompile(`(?:(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}|\+\d{1,3}[\s.-]\d{2,4}[\s.-]\d{3,4}[\s.-]\d{3,4})\b`)
	reURL     = regexp.MustCompile(`(?i)\b[a-z][a-z0-9+.-]*://[^\s"'<>)\]]+`)
	reIPv4    = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b`)
	reLocal   = regexp.MustCompile(`\blocalhost(?::\d+)?\b`)
	reAbsPath = regexp.MustCompile(`(^|[^\w.])((?:~|\.{1,2})?/(?:[\w.@+-]+/)+[\w.@+-]*)`)
	reSrcFile = regexp.MustCompile(`\b[\w-]+\.(?:py|mjs|cjs|js|ts|go|rb|java|php|cs)(?::\d+)?\b`)
)

func luhn(s string) bool {
	var digits []int
	for _, r := range s {
		if r >= '0' && r <= '9' {
			digits = append(digits, int(r-'0'))
		}
	}
	if len(digits) < 13 || len(digits) > 19 {
		return false
	}
	sum := 0
	alt := false
	for i := len(digits) - 1; i >= 0; i-- {
		d := digits[i]
		if alt {
			d *= 2
			if d > 9 {
				d -= 9
			}
		}
		sum += d
		alt = !alt
	}
	return sum%10 == 0
}

// Mask redacts emails, credentials, cards, and phone numbers.
func Mask(s string) string {
	s = reEmail.ReplaceAllString(s, "[email]")
	s = reAuth.ReplaceAllString(s, "$1 [token]")
	s = reJWT.ReplaceAllString(s, "[token]")
	s = rePrefix.ReplaceAllString(s, "[secret]")
	s = reSecret.ReplaceAllString(s, "$1$2[secret]")
	s = reCard.ReplaceAllStringFunc(s, func(m string) string {
		if luhn(m) {
			return "[card]"
		}
		return m
	})
	s = rePhone.ReplaceAllString(s, "[phone]")
	return s
}

// Plain runs Mask and removes internal URLs, hosts, and paths for external agent/user surfaces.
func Plain(s string, limit int) string {
	if limit <= 0 {
		limit = 200
	}
	s = Mask(s)
	s = reURL.ReplaceAllString(s, "[url]")
	s = reIPv4.ReplaceAllString(s, "[host]")
	s = reLocal.ReplaceAllString(s, "[host]")
	s = reAbsPath.ReplaceAllString(s, "[path]")
	s = reSrcFile.ReplaceAllString(s, "[path]")
	s = strings.Join(strings.Fields(s), " ")
	if len(s) > limit {
		return s[:limit-1] + "…"
	}
	return s
}

// --------------------------------------------------------------------------- trace & context

func randHex(nBytes int) string {
	b := make([]byte, nBytes)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func currentGID() string {
	var buf [64]byte
	n := runtime.Stack(buf[:], false)
	s := string(buf[:n])
	const prefix = "goroutine "
	if strings.HasPrefix(s, prefix) {
		s = s[len(prefix):]
		if idx := strings.IndexByte(s, ' '); idx > 0 {
			return s[:idx]
		}
	}
	return "0"
}

// DepInfo describes a recorded dependency of a hardened function.
type DepInfo struct {
	Name    string `json:"name"`
	Kind    string `json:"kind"`
	Method  string `json:"method"`
	Route   string `json:"route"`
	Program string `json:"program"`
}

// CallContext tracks an in-flight call for diagnostics and flight recording.
type CallContext struct {
	FID            string
	Deps           []DepInfo
	TraceID        string
	SpanID         string
	Parent         string
	Seed           uint64
	Started        time.Time
	Events         []map[string]any
	Dropped        int
	GaveUp         string
	LastDependency string
	Args           []any
	RNG            *mrand.Rand
}

func (c *CallContext) Note(event string, facts map[string]any) {
	if c == nil {
		return
	}
	if len(c.Events) >= 200 {
		c.Dropped++
		return
	}
	ev := map[string]any{
		"t":     math.Round(time.Since(c.Started).Seconds()*1000) / 1000,
		"event": event,
	}
	for k, v := range facts {
		if v != nil {
			ev[k] = v
		}
	}
	c.Events = append(c.Events, ev)
}

var (
	ctxMu     sync.Mutex
	ctxStack  = map[string][]*CallContext{}
	fnStatsMu sync.Mutex
	fnStats   = map[string]map[string]any{}
	breakers  = map[string]string{}
	hookMu    sync.RWMutex
	sleepFn   = time.Sleep
	nowFn     = time.Now
)

// SetClockHooks lets verify attach virtual sleep/time hooks without affecting production.
func SetClockHooks(sleep func(time.Duration), now func() time.Time) {
	hookMu.Lock()
	defer hookMu.Unlock()
	if sleep != nil {
		sleepFn = sleep
	} else {
		sleepFn = time.Sleep
	}
	if now != nil {
		nowFn = now
	} else {
		nowFn = time.Now
	}
}

// Sleep uses the configured clock hook.
func Sleep(d time.Duration) {
	if d <= 0 {
		return
	}
	hookMu.RLock()
	fn := sleepFn
	hookMu.RUnlock()
	fn(d)
}

func currentCtx() *CallContext {
	gid := currentGID()
	ctxMu.Lock()
	defer ctxMu.Unlock()
	s := ctxStack[gid]
	if len(s) == 0 {
		return nil
	}
	return s[len(s)-1]
}

// TraceID returns the active W3C trace id or a fresh 32-hex id.
func TraceID() string {
	if c := currentCtx(); c != nil && c.TraceID != "" {
		return c.TraceID
	}
	if tp := os.Getenv("TRACEPARENT"); tp != "" {
		parts := strings.Split(tp, "-")
		if len(parts) >= 3 && len(parts[1]) == 32 {
			return parts[1]
		}
	}
	return randHex(16)
}

// SpanID returns the active 16-hex span id or empty string.
func SpanID() string {
	if c := currentCtx(); c != nil {
		return c.SpanID
	}
	return ""
}

// Traceparent formats the active W3C traceparent header.
func Traceparent() string {
	sid := SpanID()
	if sid == "" {
		sid = randHex(8)
	}
	return "00-" + TraceID() + "-" + sid + "-01"
}

// Disabled returns true when RAS_ANYTHING_DISABLE=1.
func Disabled(fid string) bool {
	return os.Getenv("RAS_ANYTHING_DISABLE") == "1"
}

// --------------------------------------------------------------------------- OTel logging

var severityNum = map[string]int{"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}

func serviceName() string {
	if s := os.Getenv("OTEL_SERVICE_NAME"); s != "" {
		return s
	}
	if len(os.Args) > 0 && os.Args[0] != "" {
		return filepath.Base(os.Args[0])
	}
	return "go-app"
}

// Emit writes one OpenTelemetry Log Data Model JSON line to stderr.
func Emit(event, severity, body string, attrs map[string]any) {
	emitOpt(event, severity, body, attrs, false)
}

func emitOpt(event, severity, body string, attrs map[string]any, legacy bool) {
	lvlName := strings.ToLower(os.Getenv("RAS_ANYTHING_LOG"))
	lvl := 9
	switch lvlName {
	case "off":
		lvl = 99
	case "error":
		lvl = 17
	case "warn":
		lvl = 13
	case "debug":
		lvl = 5
	}
	sevNum := severityNum[severity]
	if sevNum == 0 {
		sevNum = 9
	}
	if sevNum < lvl && !legacy {
		return
	}
	tid := TraceID()
	if body == "" {
		body = event
	}
	hookMu.RLock()
	nf := nowFn
	hookMu.RUnlock()
	now := nf()
	rec := map[string]any{
		"Timestamp":      strconv.FormatInt(now.UnixNano(), 10),
		"SeverityText":   severity,
		"SeverityNumber": sevNum,
		"EventName":      event,
		"TraceId":        tid,
		"Body":           Mask(body),
		"Resource":       map[string]any{"service.name": serviceName()},
	}
	if legacy {
		rec["ts"] = float64(now.UnixMilli()) / 1000.0
		rec["event"] = event
		rec["trace_id"] = tid
	}
	if sid := SpanID(); sid != "" {
		rec["SpanId"] = sid
	}
	cleanAttrs := map[string]any{}
	for k, v := range attrs {
		if v == nil {
			continue
		}
		switch val := v.(type) {
		case string:
			cleanAttrs[k] = Mask(val)
		case error:
			cleanAttrs[k] = Plain(val.Error(), 200)
		default:
			cleanAttrs[k] = val
		}
	}
	if c := currentCtx(); c != nil {
		if _, ok := cleanAttrs["code.function"]; !ok {
			cleanAttrs["code.function"] = c.FID
		}
	}
	rec["Attributes"] = cleanAttrs
	b, err := json.Marshal(rec)
	if err == nil {
		fmt.Fprintln(os.Stderr, string(b))
	}
}

// LogEvent writes a structured decision log line to stderr (compatible with legacy trace_id checks).
func LogEvent(event string, fields map[string]any) {
	attrs := map[string]any{}
	for k, v := range fields {
		if k != "trace_id" {
			attrs[k] = v
		}
	}
	emitOpt(event, "INFO", event, attrs, true)
}

// --------------------------------------------------------------------------- errors & RFC 9457 Problem

// HTTPError represents a non-2xx HTTP response.
type HTTPError struct {
	StatusCode int
	URL        string
	Header     http.Header
	Body       string
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("HTTP %d", e.StatusCode)
}

var causeTable = map[string]struct {
	Title     string
	Status    int
	Retryable bool
	Action    string
}{
	"timeout":           {"The dependency did not answer in time", 504, true, "retry_later"},
	"unavailable":       {"The dependency is unavailable", 503, true, "retry_later"},
	"rate_limited":      {"The dependency asks for fewer requests", 429, true, "retry_later"},
	"quota":             {"A quota or a budget is used up", 429, false, "escalate"},
	"invalid_request":   {"The request was refused as invalid", 400, false, "change_params"},
	"auth":              {"Not authorized at the dependency", 403, false, "escalate"},
	"conflict":          {"The request conflicts with the current state", 409, false, "stop"},
	"dependency_bug":    {"The dependency answered in a way that can't be used", 502, false, "escalate"},
	"approval_required": {"A person has to authorize this operation", 403, false, "escalate"},
	"unknown":           {"The operation failed", 500, false, "stop"},
}

var faultCause = map[string]string{
	"timeout":         "timeout",
	"refused":         "unavailable",
	"unavailable":     "unavailable",
	"truncated":       "unavailable",
	"circuit_open":    "unavailable",
	"rejected":        "unavailable",
	"rate_limited":    "rate_limited",
	"quota":           "quota",
	"invalid_request": "invalid_request",
	"auth":            "auth",
	"conflict":        "conflict",
	"unsupported":     "dependency_bug",
	"approval":        "approval_required",
}

// FaultClass classifies a Go error into canonical ras-anything fault classes.
func FaultClass(err error) string {
	if err == nil {
		return ""
	}
	var appErr *ApprovalRequiredError
	if errors.As(err, &appErr) {
		return "approval"
	}
	var cbErr *CircuitOpenError
	if errors.As(err, &cbErr) {
		return "circuit_open"
	}
	var rasErr *RasError
	if errors.As(err, &rasErr) && rasErr.Category != "" {
		for k, v := range faultCause {
			if v == rasErr.Category {
				return k
			}
		}
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return "timeout"
	}
	var he *HTTPError
	if errors.As(err, &he) {
		switch {
		case he.StatusCode == 429:
			if strings.Contains(strings.ToLower(he.Body), "quota") {
				return "quota"
			}
			return "rate_limited"
		case he.StatusCode == 402:
			return "quota"
		case he.StatusCode == 401 || he.StatusCode == 403:
			return "auth"
		case he.StatusCode == 409:
			return "conflict"
		case he.StatusCode == 501 || he.StatusCode == 505:
			return "unsupported"
		case he.StatusCode >= 500:
			return "unavailable"
		case he.StatusCode >= 400:
			return "invalid_request"
		}
	}
	msg := strings.ToLower(err.Error())
	switch {
	case strings.Contains(msg, "timeout") || strings.Contains(msg, "deadline exceeded"):
		return "timeout"
	case strings.Contains(msg, "connection refused") || strings.Contains(msg, "econnrefused") || strings.Contains(msg, "resource temporarily unavailable") || strings.Contains(msg, "eagain"):
		return "refused"
	case strings.Contains(msg, "unexpected eof") || strings.Contains(msg, "eof") || strings.Contains(msg, "invalid character"):
		return "truncated"
	case strings.Contains(msg, "429"):
		return "rate_limited"
	case strings.Contains(msg, "503") || strings.Contains(msg, "502") || strings.Contains(msg, "504") || strings.Contains(msg, "exit status 75"):
		return "unavailable"
	}
	return "unknown"
}

// IsTransient reports whether retrying err may succeed.
func IsTransient(err error) bool {
	fc := FaultClass(err)
	return fc == "timeout" || fc == "refused" || fc == "unavailable" || fc == "rate_limited" || fc == "truncated"
}

// RasError is a structured error carrying trace id, dependency, and RFC 9457 Problem details.
type RasError struct {
	Message    string
	Cause      error
	Dependency string
	Category   string
	TraceIDVal string
	Context    map[string]any
}

func (e *RasError) Error() string { return e.Message }
func (e *RasError) Unwrap() error { return e.Cause }

// Problem returns the RFC 9457 problem+json map for agents.
func (e *RasError) Problem() map[string]any {
	return ProblemOf(e)
}

// ProblemOf builds an RFC 9457 map from any error.
func ProblemOf(err error) map[string]any {
	fc := FaultClass(err)
	cause := faultCause[fc]
	if cause == "" {
		cause = "unknown"
	}
	var re *RasError
	dep := ""
	tid := TraceID()
	detail := ""
	if errors.As(err, &re) {
		if re.Category != "" {
			cause = re.Category
		}
		dep = re.Dependency
		if re.TraceIDVal != "" {
			tid = re.TraceIDVal
		}
		detail = re.Message
	}
	meta, ok := causeTable[cause]
	if !ok {
		cause = "unknown"
		meta = causeTable["unknown"]
	}
	if detail == "" {
		detail = meta.Title
	}
	if dep == "" {
		if c := currentCtx(); c != nil {
			dep = c.LastDependency
			if dep == "" && len(c.Deps) == 1 {
				dep = c.Deps[0].Name
			}
		}
	}
	var wait any
	var he *HTTPError
	if errors.As(err, &he) && he.Header != nil {
		if ra := he.Header.Get("Retry-After"); ra != "" {
			if sec, perr := strconv.ParseFloat(strings.TrimSpace(ra), 64); perr == nil {
				wait = math.Round(sec*1000) / 1000
			}
		}
	}
	var depVal any
	if dep != "" {
		depVal = Plain(dep, 60)
	}
	return map[string]any{
		"type":                      "urn:ras-anything:problem:" + cause,
		"title":                     meta.Title,
		"status":                    meta.Status,
		"detail":                    Plain(detail, 200),
		"instance":                  "urn:trace:" + tid,
		"cause":                     cause,
		"dependency":                depVal,
		"retryable":                 meta.Retryable,
		"retry_after_s":             wait,
		"next_action":               meta.Action,
		"needs_human_authorization": cause == "approval_required",
		"correlation_id":            tid,
	}
}

// --------------------------------------------------------------------------- human gate

// ApprovalRequiredError is returned when an irreversible action lacks authorization.
type ApprovalRequiredError struct {
	Op      string
	Details map[string]any
}

func (e *ApprovalRequiredError) Error() string {
	return fmt.Sprintf("a person has to approve %s first", e.Op)
}

// RequireApproval checks RAS_ANYTHING_APPROVALS / .ras/approvals.json before irreversible operations.
func RequireApproval(op string, details map[string]any) error {
	if os.Getenv("RAS_ANYTHING_APPROVALS") == "*" {
		return nil
	}
	if raw := os.Getenv("RAS_ANYTHING_APPROVALS"); raw != "" {
		for _, tok := range strings.Split(raw, ",") {
			if strings.TrimSpace(tok) == op {
				return nil
			}
		}
	}
	if b, err := os.ReadFile(filepath.Join(".ras", "approvals.json")); err == nil {
		var doc map[string]any
		if json.Unmarshal(b, &doc) == nil {
			if v, ok := doc[op].(bool); ok && v {
				return nil
			}
		}
	}
	return &ApprovalRequiredError{Op: op, Details: details}
}

// --------------------------------------------------------------------------- Retry & CircuitBreaker

// CircuitOpenError is returned when an open circuit breaker short-circuits a call.
type CircuitOpenError struct{ Name string }

func (e *CircuitOpenError) Error() string { return "circuit open: " + e.Name }

// CircuitBreaker tracks consecutive dependency failures and fails fast when open.
type CircuitBreaker struct {
	mu          sync.Mutex
	Name        string
	Threshold   int
	Cooldown    time.Duration
	failures    int
	state       string // closed | open | half_open
	openedAt    time.Time
	probing     bool
}

var (
	cbRegMu sync.Mutex
	cbReg   []*CircuitBreaker
)

func NewCircuitBreaker(name string, threshold int, cooldown time.Duration) *CircuitBreaker {
	if threshold <= 0 {
		threshold = 5
	}
	if cooldown <= 0 {
		cooldown = 15 * time.Second
	}
	cb := &CircuitBreaker{Name: name, Threshold: threshold, Cooldown: cooldown, state: "closed"}
	cbRegMu.Lock()
	cbReg = append(cbReg, cb)
	cbRegMu.Unlock()
	return cb
}

// ResetAll resets all registered CircuitBreakers and runtime stats (used between isolated verify trials).
func ResetAll() {
	cbRegMu.Lock()
	for _, cb := range cbReg {
		cb.mu.Lock()
		cb.failures = 0
		cb.state = "closed"
		cb.openedAt = time.Time{}
		cb.probing = false
		cb.mu.Unlock()
	}
	cbRegMu.Unlock()
	fnStatsMu.Lock()
	fnStats = map[string]map[string]any{}
	breakers = map[string]string{}
	fnStatsMu.Unlock()
}

func (cb *CircuitBreaker) Allow() error {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	hookMu.RLock()
	nf := nowFn
	hookMu.RUnlock()
	now := nf()
	switch cb.state {
	case "open":
		if now.Sub(cb.openedAt) >= cb.Cooldown {
			cb.state = "half_open"
			cb.probing = true
			return nil
		}
		return &CircuitOpenError{Name: cb.Name}
	case "half_open":
		if cb.probing {
			return &CircuitOpenError{Name: cb.Name}
		}
		cb.probing = true
		return nil
	default:
		return nil
	}
}

func (cb *CircuitBreaker) Success() {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	cb.failures = 0
	cb.probing = false
	if cb.state != "closed" {
		prev := cb.state
		cb.state = "closed"
		Emit("ras.breaker.transition", "INFO", fmt.Sprintf("breaker %s: %s -> closed", cb.Name, prev),
			map[string]any{"ras.breaker": cb.Name, "ras.breaker.state": "closed", "ras.dependency": cb.Name})
		fnStatsMu.Lock()
		delete(breakers, cb.Name)
		fnStatsMu.Unlock()
	}
}

func (cb *CircuitBreaker) Failure() {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	hookMu.RLock()
	nf := nowFn
	hookMu.RUnlock()
	cb.failures++
	cb.probing = false
	if cb.state == "half_open" || cb.failures >= cb.Threshold {
		prev := cb.state
		cb.state = "open"
		cb.openedAt = nf()
		if prev != "open" {
			Emit("ras.breaker.transition", "WARN", fmt.Sprintf("breaker %s: %s -> open", cb.Name, prev),
				map[string]any{"ras.breaker": cb.Name, "ras.breaker.state": "open", "ras.dependency": cb.Name})
			fnStatsMu.Lock()
			breakers[cb.Name] = "open"
			fnStatsMu.Unlock()
		}
	}
}

// RetryOpts configures bounded jittered retries.
type RetryOpts struct {
	Attempts   int
	Base       time.Duration
	Cap        time.Duration
	Budget     time.Duration
	Dependency string
	RetryOn    func(error) bool
}

// Retry executes fn up to opts.Attempts with full-jitter exponential backoff and budget check.
func Retry(ctx context.Context, opts RetryOpts, fn func(context.Context) error) error {
	if opts.Attempts <= 0 {
		opts.Attempts = 3
	}
	if opts.Base <= 0 {
		opts.Base = 100 * time.Millisecond
	}
	if opts.Cap <= 0 {
		opts.Cap = 2 * time.Second
	}
	if opts.RetryOn == nil {
		opts.RetryOn = IsTransient
	}
	hookMu.RLock()
	nf := nowFn
	hookMu.RUnlock()
	start := nf()
	var lastErr error
	for n := 1; n <= opts.Attempts; n++ {
		if ctx.Err() != nil {
			lastErr = ctx.Err()
			break
		}
		if opts.Budget > 0 && nf().Sub(start) >= opts.Budget {
			if lastErr == nil {
				lastErr = context.DeadlineExceeded
			}
			break
		}
		t0 := nf()
		err := fn(ctx)
		if err == nil {
			return nil
		}
		lastErr = err
		took := nf().Sub(t0)
		fc := FaultClass(err)
		if c := currentCtx(); c != nil {
			dep := opts.Dependency
			if dep == "" {
				dep = c.LastDependency
			}
			c.Note("failure", map[string]any{"n": n, "dependency": dep, "fault": fc, "took_s": took.Seconds()})
			Emit("ras.dependency.failure", "WARN", fmt.Sprintf("%s failed: %s (attempt %d)", dep, fc, n),
				map[string]any{"ras.dependency": dep, "ras.fault": fc, "error.type": fc, "exception.message": Plain(err.Error(), 200), "http.request.resend_count": n - 1})
		}
		if n == opts.Attempts || !opts.RetryOn(err) {
			if c := currentCtx(); c != nil {
				c.GaveUp = fc
				c.Note("gave_up", map[string]any{"n": n, "why": "exhausted", "fault": fc, "dependency": opts.Dependency})
			}
			Emit("ras.dependency.gave_up", "ERROR", fmt.Sprintf("%s: gave up after %d attempt(s): %s", opts.Dependency, n, fc),
				map[string]any{"ras.dependency": opts.Dependency, "ras.fault": fc, "error.type": fc, "exception.message": Plain(err.Error(), 200)})
			return err
		}
		maxDelay := opts.Base << uint(n-1)
		if maxDelay > opts.Cap {
			maxDelay = opts.Cap
		}
		var he *HTTPError
		delay := time.Duration(mrand.Int64N(int64(maxDelay) + 1))
		if errors.As(err, &he) && he.Header != nil {
			if ra := he.Header.Get("Retry-After"); ra != "" {
				if sec, perr := strconv.ParseFloat(strings.TrimSpace(ra), 64); perr == nil {
					delay = time.Duration(sec * float64(time.Second))
				}
			}
		}
		if opts.Budget > 0 && nf().Sub(start)+delay >= opts.Budget {
			if c := currentCtx(); c != nil {
				c.GaveUp = fc
			}
			Emit("ras.dependency.gave_up", "ERROR", fmt.Sprintf("%s: gave up (budget): %s", opts.Dependency, fc),
				map[string]any{"ras.dependency": opts.Dependency, "ras.fault": fc, "error.type": fc, "exception.message": Plain(err.Error(), 200)})
			return err
		}
		Sleep(delay)
	}
	return lastErr
}

// IdempotencyKey builds a stable key for non-idempotent retries.
func IdempotencyKey(parts ...any) string {
	var strs []string
	for _, p := range parts {
		strs = append(strs, fmt.Sprint(p))
	}
	h := sha256.Sum256([]byte(strings.Join(strs, "|")))
	return "ras-" + hex.EncodeToString(h[:8])
}

// Health returns a snapshot of observed functions and open circuit breakers.
func Health() map[string]any {
	fnStatsMu.Lock()
	defer fnStatsMu.Unlock()
	status := "ok"
	if len(breakers) > 0 {
		status = "degraded"
	}
	snap := map[string]any{
		"status":    status,
		"updated":   time.Now().UTC().Format(time.RFC3339),
		"breakers":  breakers,
		"functions": fnStats,
	}
	return snap
}

func writeHealth() {
	if os.Getenv("RAS_ANYTHING_VERIFY") == "1" {
		return
	}
	target := os.Getenv("RAS_ANYTHING_HEALTH_FILE")
	if target == "" {
		if info, err := os.Stat(".ras"); err == nil && info.IsDir() {
			target = filepath.Join(".ras", "health.json")
		}
	}
	if target == "" {
		return
	}
	b, err := json.MarshalIndent(Health(), "", "  ")
	if err == nil {
		_ = os.WriteFile(target, b, 0o644)
	}
}

// ObserveCall wraps a hardened function call with trace context, stats, health snapshot, and incident dump.
func ObserveCall[T any](fid string, deps []DepInfo, args []any, fn func() (T, error)) (T, error) {
	ctx := &CallContext{
		FID:     fid,
		Deps:    deps,
		TraceID: TraceID(),
		SpanID:  randHex(8),
		Started: time.Now(),
		Args:    args,
		Seed:    mrand.Uint64(),
	}
	gid := currentGID()
	ctxMu.Lock()
	ctxStack[gid] = append(ctxStack[gid], ctx)
	ctxMu.Unlock()

	defer func() {
		ctxMu.Lock()
		s := ctxStack[gid]
		if len(s) > 0 {
			ctxStack[gid] = s[:len(s)-1]
		}
		ctxMu.Unlock()
	}()

	res, err := fn()
	fnStatsMu.Lock()
	st, ok := fnStats[fid]
	if !ok {
		st = map[string]any{"calls": 0, "raised": 0, "gave_up": 0}
		fnStats[fid] = st
	}
	st["calls"] = st["calls"].(int) + 1
	if err != nil {
		st["raised"] = st["raised"].(int) + 1
		st["last_fault"] = FaultClass(err)
	}
	if ctx.GaveUp != "" {
		st["gave_up"] = st["gave_up"].(int) + 1
		st["last_fault"] = ctx.GaveUp
	}
	fnStatsMu.Unlock()
	writeHealth()
	if err != nil || ctx.GaveUp != "" {
		dumpIncident(ctx, err)
	}
	return res, err
}

func dumpIncident(ctx *CallContext, err error) {
	if os.Getenv("RAS_ANYTHING_VERIFY") == "1" {
		return
	}
	at := os.Getenv("RAS_ANYTHING_INCIDENTS")
	if strings.EqualFold(at, "off") || at == "0" {
		return
	}
	if at == "" {
		if info, serr := os.Stat(".ras"); serr == nil && info.IsDir() {
			at = filepath.Join(".ras", "incidents")
		} else {
			return
		}
	}
	fault := ctx.GaveUp
	if fault == "" && err != nil {
		fault = FaultClass(err)
	}
	stamp := time.Now().UTC().Format("20060102T150405")
	safeFID := strings.Map(func(r rune) rune {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' || r == '.' || r == '-' {
			return r
		}
		return '_'
	}, ctx.FID)
	id := fmt.Sprintf("inc-%s-%s-%s", stamp, safeFID, ctx.SpanID[:8])
	doc := map[string]any{
		"schema_version": SchemaVersion,
		"kind":           "incident",
		"id":             id,
		"created":        time.Now().UTC().Format(time.RFC3339),
		"lang":           "go",
		"service":        serviceName(),
		"pid":            os.Getpid(),
		"function":       ctx.FID,
		"trigger":        "gave_up",
		"trace_id":       ctx.TraceID,
		"span_id":        ctx.SpanID,
		"seed":           ctx.Seed,
		"duration_s":     math.Round(time.Since(ctx.Started).Seconds()*1000) / 1000,
		"dependencies":   ctx.Deps,
		"dependency":     ctx.LastDependency,
		"fault":          fault,
		"events":         ctx.Events,
	}
	if err != nil {
		doc["outcome"] = map[string]any{
			"kind":       "raise",
			"error_type": fmt.Sprintf("%T", err),
			"message":    Plain(err.Error(), 300),
			"problem":    ProblemOf(err),
		}
	}
	_ = os.MkdirAll(at, 0o755)
	b, merr := json.MarshalIndent(doc, "", " ")
	if merr == nil {
		_ = os.WriteFile(filepath.Join(at, id+".json"), b, 0o644)
	}
}
