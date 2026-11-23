// Package rasagent runs inside instrumented Go programs for scan, chaos, record, bench, and verify.
package rasagent

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// --------------------------------------------------------------------------- config & state

type Config struct {
	Protocol   int              `json:"protocol"`
	Mode       string           `json:"mode"` // scan | chaos | record | bench | verify
	Root       string           `json:"root"`
	Project    string           `json:"project"`
	Cwd        string           `json:"cwd"`
	Out        string           `json:"out"`
	Seed       *int64           `json:"seed"`
	Rate       float64          `json:"rate"`
	OutageRate float64          `json:"outage_rate"`
	Scale      float64          `json:"scale"`
	HangS      float64          `json:"hang_s"`
	Target     *TargetCfg       `json:"target"`
	CallsPath  string           `json:"calls_path"`
	MaxCalls   int              `json:"max_calls"`
	Targets    []BenchTargetCfg `json:"targets"`
}

type TargetCfg struct {
	ID       string `json:"id"`
	Module   string `json:"module"`
	Qualname string `json:"qualname"`
	File     string `json:"file"`
}

type BenchTargetCfg struct {
	ID       string `json:"id"`
	Module   string `json:"module"`
	Qualname string `json:"qualname"`
	File     string `json:"file"`
}

type Plan struct {
	Type       string   `json:"type"`
	Fault      string   `json:"fault"`
	Key        string   `json:"key"`
	Seed       int64    `json:"seed"`
	Rate       float64  `json:"rate"`
	OutageRate float64  `json:"outage_rate"`
	Salt       string   `json:"salt"`
	Faults     []string `json:"faults"`
	Repeat     *bool    `json:"repeat"`
}

type FnStat struct {
	File     string         `json:"file"`
	Line     int            `json:"line"`
	Name     string         `json:"name"`
	Module   string         `json:"module"`
	Calls    int            `json:"calls"`
	Boundary int            `json:"boundary"`
	Kinds    map[string]int `json:"kinds"`
	ArgOK    bool           `json:"arg_ok"`
	RetOK    bool           `json:"ret_ok"`
	Faults   map[string]int `json:"faults"`
}

type AttemptRec struct {
	Key           string  `json:"key"`
	N             int     `json:"n"`
	Method        string  `json:"method"`
	Nonidempotent bool    `json:"nonidempotent"`
	Idem          *string `json:"idem"`
	Fault         *string `json:"fault"`
	Reached       bool    `json:"reached"`
	Failed        bool    `json:"failed"`
	Outage        bool    `json:"outage"`
}

type BenchStat struct {
	Calls          int            `json:"calls"`
	Ok             int            `json:"ok"`
	Fallback       int            `json:"fallback"`
	Raised         map[string]int `json:"raised"`
	Hardened       bool           `json:"hardened"`
	P50            *float64       `json:"p50"`
	P99            *float64       `json:"p99"`
	Attempts       int            `json:"attempts"`
	OutageCalls    int            `json:"outage_calls"`
	OutageAttempts int            `json:"outage_attempts"`
	durs           []float64
}

type IOEntry struct {
	Req map[string]any `json:"req"`
	Out map[string]any `json:"out"`
	Dur float64        `json:"dur"`
}

type RecordedCall struct {
	Args   []any     `json:"args"`
	Kwargs map[string]any `json:"kwargs"`
	Result any       `json:"result,omitempty"`
	Exc    *ExcRec   `json:"exc,omitempty"`
	Dur    float64   `json:"dur"`
	IO     []IOEntry `json:"io"`
}

type ExcRec struct {
	Type   string `json:"type"`
	Module string `json:"module"`
	Msg    string `json:"msg"`
}

var (
	mu            sync.Mutex
	cfg           *Config
	activePlan    = Plan{Type: "none"}
	baseTransport = http.DefaultTransport
	fnTable       = map[string]*FnStat{}
	attempts      []AttemptRec
	counts        = map[string]int{}
	boundaryTotal int
	hangs         int
	uncaught      map[string]any
	benchStats    = map[string]*BenchStat{}
	benchTargets  = map[string]bool{}
	stacks        = map[string][]string{}
	recIOStacks   = map[string][]*[]IOEntry{}
	benchScopes   = map[string][]*[]AttemptRec{}
	recordedCalls []RecordedCall
	seenRec       int
	skippedRec    int
	finished      bool

	// verify state
	verifyMode    bool
	vClock        time.Time
	vBase         = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	vIO           []IOEntry
	vIOUsed       []bool
	vSlowS        = 3.0
	vHangCapS     = 60.0
	vOutageK      = 3
	vRetryAfter   = "1"
)

func init() {
	raw := os.Getenv("RAS_ANYTHING_CFG")
	if raw == "" {
		return
	}
	_ = os.Unsetenv("RAS_ANYTHING_CFG")
	var c Config
	if err := json.Unmarshal([]byte(raw), &c); err != nil {
		fmt.Fprintf(os.Stderr, "ras-anything go agent: bad config: %v\n", err)
		return
	}
	cfg = &c
	if c.Scale <= 0 {
		c.Scale = 0.05
	}
	if c.HangS <= 0 {
		c.HangS = 2.0
	}
	if c.MaxCalls <= 0 {
		c.MaxCalls = 500
	}
	for _, t := range c.Targets {
		benchTargets[t.ID] = true
	}
	if (c.Mode == "chaos" || c.Mode == "bench") && c.Seed != nil {
		activePlan = Plan{Type: "seeded", Seed: *c.Seed, Rate: c.Rate, OutageRate: c.OutageRate, Salt: ""}
	} else {
		activePlan = Plan{Type: "none"}
	}
	if c.Mode != "verify" {
		http.DefaultTransport = NewTransport(baseTransport)
	}
}

// --------------------------------------------------------------------------- goroutine helpers & function registration

func gid() string {
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

// RegisterFn registers static metadata for a function in the project.
func RegisterFn(fid, file string, line int, name, module string, argOK, retOK bool) {
	mu.Lock()
	defer mu.Unlock()
	if _, ok := fnTable[fid]; !ok {
		fnTable[fid] = &FnStat{
			File: file, Line: line, Name: name, Module: module,
			Kinds: map[string]int{}, ArgOK: argOK, RetOK: retOK, Faults: map[string]int{},
		}
	}
}

// Enter pushes fid onto the current goroutine stack.
func Enter(fid string) string {
	if cfg == nil {
		return ""
	}
	g := gid()
	mu.Lock()
	defer mu.Unlock()
	stacks[g] = append(stacks[g], fid)
	if st, ok := fnTable[fid]; ok {
		st.Calls++
	}
	return fid
}

// Exit pops fid from the current goroutine stack.
func Exit(fid string) {
	if cfg == nil || fid == "" {
		return
	}
	g := gid()
	mu.Lock()
	defer mu.Unlock()
	s := stacks[g]
	if len(s) > 0 {
		stacks[g] = s[:len(s)-1]
	}
}

func innermostLocked() string {
	s := stacks[gid()]
	if len(s) == 0 {
		return ""
	}
	return s[len(s)-1]
}

// TrackBench wraps a bench-targeted function execution to track stats and attempts.
func TrackBench(fid string, isFallback bool, fn func() error) error {
	if cfg == nil || cfg.Mode != "bench" {
		return fn()
	}
	g := gid()
	var scoped []AttemptRec
	mu.Lock()
	benchScopes[g] = append(benchScopes[g], &scoped)
	mu.Unlock()
	t0 := time.Now()
	err := fn()
	dur := time.Since(t0).Seconds()

	mu.Lock()
	defer mu.Unlock()
	bs := benchScopes[g]
	if len(bs) > 0 {
		benchScopes[g] = bs[:len(bs)-1]
	}
	st, ok := benchStats[fid]
	if !ok {
		st = &BenchStat{Raised: map[string]int{}, Hardened: os.Getenv("RAS_ANYTHING_DISABLE") != "1"}
		benchStats[fid] = st
	}
	st.Calls++
	st.durs = append(st.durs, dur)
	st.Attempts += len(scoped)
	metOutage := false
	for _, a := range scoped {
		if a.Outage {
			metOutage = true
			st.OutageAttempts++
		}
	}
	if metOutage {
		st.OutageCalls++
	}
	if err != nil {
		ename := fmt.Sprintf("%T", err)
		st.Raised[ename]++
	} else if isFallback {
		st.Fallback++
	} else {
		st.Ok++
	}
	return err
}

// RecordCall captures a single call's arguments, result/err, and boundary io into calls.json.
func RecordCall(fid string, args []any, fn func() (any, error)) (any, error) {
	if cfg == nil || cfg.Mode != "record" || cfg.Target == nil || cfg.Target.ID != fid {
		return fn()
	}
	g := gid()
	var ioList []IOEntry
	mu.Lock()
	seenRec++
	over := len(recordedCalls) >= cfg.MaxCalls
	if !over {
		recIOStacks[g] = append(recIOStacks[g], &ioList)
	}
	mu.Unlock()

	if over {
		mu.Lock()
		skippedRec++
		mu.Unlock()
		return fn()
	}

	t0 := time.Now()
	res, err := fn()
	dur := time.Since(t0).Seconds()

	mu.Lock()
	s := recIOStacks[g]
	if len(s) > 0 {
		recIOStacks[g] = s[:len(s)-1]
	}
	rc := RecordedCall{
		Args:   args,
		Kwargs: map[string]any{},
		Dur:    math.Round(dur*1e6) / 1e6,
		IO:     ioList,
	}
	if rc.IO == nil {
		rc.IO = []IOEntry{}
	}
	if err != nil {
		rc.Exc = &ExcRec{Type: fmt.Sprintf("%T", err), Module: "go", Msg: err.Error()}
	} else {
		rc.Result = res
	}
	recordedCalls = append(recordedCalls, rc)
	mu.Unlock()
	return res, err
}

// --------------------------------------------------------------------------- deterministic fault engine (matches faults.py)

var faultKinds = []string{"timeout", "refused", "http5xx", "http429", "slow", "truncated"}

func uHash(parts ...string) float64 {
	h := sha256.Sum256([]byte(strings.Join(parts, "|")))
	v := binary.BigEndian.Uint64(h[:8])
	return float64(v) / 18446744073709551616.0
}

func faultOf(p Plan, key string, n int) string {
	switch p.Type {
	case "none", "":
		return ""
	case "down":
		return "refused"
	case "first":
		if p.Key != "" && p.Key != key {
			return ""
		}
		if p.Fault == "outage" {
			if n < vOutageK {
				return "refused"
			}
			return ""
		}
		if n == 0 {
			return p.Fault
		}
		return ""
	case "schedule":
		if p.Key != "" && p.Key != key {
			return ""
		}
		if len(p.Faults) == 0 {
			return ""
		}
		if n < len(p.Faults) {
			return p.Faults[n]
		}
		if p.Repeat == nil || *p.Repeat {
			return p.Faults[len(p.Faults)-1]
		}
		return ""
	case "seeded":
		seedStr := strconv.FormatInt(p.Seed, 10)
		if n < vOutageK && uHash(seedStr, p.Salt, key, "outage") < p.OutageRate {
			return "refused"
		}
		if uHash(seedStr, p.Salt, key, strconv.Itoa(n), "p") < p.Rate {
			idx := int(math.Floor(uHash(seedStr, p.Salt, key, strconv.Itoa(n), "k") * float64(len(faultKinds))))
			if idx >= len(faultKinds) {
				idx = len(faultKinds) - 1
			}
			return faultKinds[idx]
		}
		return ""
	default:
		return ""
	}
}

func isOutage(p Plan, key string, n int) bool {
	if p.Type == "down" {
		return true
	}
	if p.Type == "first" && p.Fault == "outage" && (p.Key == "" || p.Key == key) {
		return n < vOutageK
	}
	if p.Type == "seeded" {
		seedStr := strconv.FormatInt(p.Seed, 10)
		return n < vOutageK && uHash(seedStr, p.Salt, key, "outage") < p.OutageRate
	}
	return false
}

// --------------------------------------------------------------------------- virtual clock helpers (for verify)

type vDeadlineKeyType struct{}

var vDeadlineKey = vDeadlineKeyType{}

// Now returns virtual time in verify, real time otherwise.
func Now() time.Time {
	if verifyMode {
		mu.Lock()
		t := vClock
		mu.Unlock()
		return t
	}
	return time.Now()
}

// Sleep advances virtual clock in verify, or sleeps scaled in live mode.
func Sleep(d time.Duration) {
	if d <= 0 {
		return
	}
	if verifyMode {
		mu.Lock()
		vClock = vClock.Add(d)
		mu.Unlock()
		return
	}
	time.Sleep(d)
}

// After behaves like time.After on the virtual clock.
func After(d time.Duration) <-chan time.Time {
	if verifyMode {
		Sleep(d)
		ch := make(chan time.Time, 1)
		ch <- Now()
		return ch
	}
	return time.After(d)
}

// Timer mimics time.Timer on the virtual clock.
type Timer struct {
	C    <-chan time.Time
	real *time.Timer
	ch   chan time.Time
}

// NewTimer creates a timer backed by the virtual clock during verify.
func NewTimer(d time.Duration) *Timer {
	if verifyMode {
		Sleep(d)
		ch := make(chan time.Time, 1)
		ch <- Now()
		return &Timer{C: ch, ch: ch}
	}
	rt := time.NewTimer(d)
	return &Timer{C: rt.C, real: rt}
}

func (t *Timer) Stop() bool {
	if t.real != nil {
		return t.real.Stop()
	}
	return true
}

// WithTimeout attaches a virtual deadline during verify (or delegates to context.WithTimeout).
func WithTimeout(parent context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	if verifyMode {
		dl := Now().Add(d)
		return WithDeadline(parent, dl)
	}
	return context.WithTimeout(parent, d)
}

// WithDeadline attaches a virtual deadline during verify.
func WithDeadline(parent context.Context, dl time.Time) (context.Context, context.CancelFunc) {
	if verifyMode {
		if prev, ok := parent.Value(vDeadlineKey).(time.Time); ok && prev.Before(dl) {
			dl = prev
		}
		ctx, cancel := context.WithCancel(parent)
		ctx = context.WithValue(ctx, vDeadlineKey, dl)
		return ctx, cancel
	}
	return context.WithDeadline(parent, dl)
}

func deadlineRemaining(ctx context.Context) (time.Duration, bool) {
	if verifyMode {
		if dl, ok := ctx.Value(vDeadlineKey).(time.Time); ok {
			return dl.Sub(Now()), true
		}
		return 0, false
	}
	if dl, ok := ctx.Deadline(); ok {
		return time.Until(dl), true
	}
	return 0, false
}

// --------------------------------------------------------------------------- ChaosHang / ReplayMiss

type ChaosHang struct{ Where string }

func (e *ChaosHang) Error() string { return "chaos hang at " + e.Where }

type ReplayMiss struct{ Msg string }

func (e *ReplayMiss) Error() string { return e.Msg }

func triggerLiveHang(where string) {
	mu.Lock()
	hangs++
	uncaught = map[string]any{"type": "ChaosHang", "msg": "boundary call with no timeout hung (chaos)", "hang": true, "where": where}
	mu.Unlock()
	Finish()
	os.Exit(70)
}

// --------------------------------------------------------------------------- HTTP Transport

type Transport struct {
	Base http.RoundTripper
}

func NewTransport(base http.RoundTripper) *Transport {
	if base == nil {
		base = http.DefaultTransport
	}
	return &Transport{Base: base}
}

func canonicalKey(method, rawURL string, body []byte) string {
	u, err := url.Parse(rawURL)
	if err != nil {
		return method + " " + rawURL
	}
	u.Host = u.Hostname()
	k := strings.ToUpper(method) + " " + u.String()
	if len(body) > 0 {
		sum := sha256.Sum256(body)
		k += " #" + hex.EncodeToString(sum[:])[:12]
	}
	return k
}

func isNonidempotent(method string) bool {
	switch strings.ToUpper(method) {
	case "POST", "PUT", "PATCH", "DELETE":
		return true
	}
	return false
}

func noteAttempt(rec AttemptRec, fid string, kind string) {
	mu.Lock()
	defer mu.Unlock()
	boundaryTotal++
	attempts = append(attempts, rec)
	if fid != "" {
		if st, ok := fnTable[fid]; ok {
			st.Boundary++
			st.Kinds[kind]++
			if rec.Fault != nil {
				st.Faults[*rec.Fault]++
			}
		}
	}
	if bs := benchScopes[gid()]; len(bs) > 0 {
		*bs[len(bs)-1] = append(*bs[len(bs)-1], rec)
	}
}

func (t *Transport) RoundTrip(req *http.Request) (*http.Response, error) {
	if cfg == nil && !verifyMode {
		return t.Base.RoundTrip(req)
	}
	var bodyBytes []byte
	if req.Body != nil {
		bodyBytes, _ = io.ReadAll(req.Body)
		_ = req.Body.Close()
		req.Body = io.NopCloser(bytes.NewReader(bodyBytes))
	}
	method := strings.ToUpper(req.Method)
	if method == "" {
		method = "GET"
	}
	key := canonicalKey(method, req.URL.String(), bodyBytes)
	nonidem := isNonidempotent(method)
	var idemPtr *string
	if ik := req.Header.Get("Idempotency-Key"); ik != "" {
		idemPtr = &ik
	}

	mu.Lock()
	fid := innermostLocked()
	n := counts[key]
	counts[key] = n + 1
	plan := activePlan
	mu.Unlock()

	if verifyMode {
		return t.roundTripVerify(req, key, method, nonidem, idemPtr, n, plan)
	}

	if cfg.Mode == "record" {
		t0 := time.Now()
		res, err := t.Base.RoundTrip(req)
		dur := time.Since(t0).Seconds()
		mu.Lock()
		recStacks := recIOStacks[gid()]
		var targetList *[]IOEntry
		if len(recStacks) > 0 {
			targetList = recStacks[len(recStacks)-1]
		}
		mu.Unlock()
		if targetList != nil {
			reqDict := map[string]any{
				"kind": "http", "method": method, "url": req.URL.String(),
				"idempotency_key": idemPtr, "key": key,
			}
			var outDict map[string]any
			if err != nil {
				outDict = map[string]any{"exc": map[string]any{"type": fmt.Sprintf("%T", err), "module": "net/http", "msg": err.Error()}}
			} else {
				rb, _ := io.ReadAll(res.Body)
				_ = res.Body.Close()
				res.Body = io.NopCloser(bytes.NewReader(rb))
				var hdrs [][]string
				for hk, hvs := range res.Header {
					for _, hv := range hvs {
						hdrs = append(hdrs, []string{hk, hv})
					}
				}
				sort.Slice(hdrs, func(i, j int) bool { return hdrs[i][0] < hdrs[j][0] })
				outDict = map[string]any{
					"status": res.StatusCode, "reason": res.Status, "headers": hdrs,
					"body": encodeBytes(rb), "url": req.URL.String(),
				}
			}
			mu.Lock()
			*targetList = append(*targetList, IOEntry{Req: reqDict, Out: outDict, Dur: dur})
			mu.Unlock()
		}
		return res, err
	}

	// scan / chaos / bench
	fault := faultOf(plan, key, n)
	outage := isOutage(plan, key, n)
	var faultPtr *string
	if fault != "" {
		faultPtr = &fault
	}
	rec := AttemptRec{Key: key, N: n, Method: method, Nonidempotent: nonidem, Idem: idemPtr, Fault: faultPtr, Outage: outage}

	switch fault {
	case "":
		res, err := t.Base.RoundTrip(req)
		rec.Reached = true
		rec.Failed = err != nil || res.StatusCode >= 500 || res.StatusCode == 429
		noteAttempt(rec, fid, "http")
		return res, err
	case "slow":
		time.Sleep(time.Duration(vSlowS * cfg.Scale * float64(time.Second)))
		res, err := t.Base.RoundTrip(req)
		rec.Reached = true
		rec.Failed = err != nil || res.StatusCode >= 500 || res.StatusCode == 429
		noteAttempt(rec, fid, "http")
		return res, err
	case "truncated":
		res, err := t.Base.RoundTrip(req)
		rec.Reached = true
		if err == nil && res != nil && res.Body != nil {
			rb, _ := io.ReadAll(res.Body)
			_ = res.Body.Close()
			if len(rb) > 1 {
				rb = rb[:len(rb)/2]
				rec.Failed = true
			}
			res.Body = io.NopCloser(bytes.NewReader(rb))
			res.ContentLength = int64(len(rb))
		} else {
			rec.Failed = true
		}
		noteAttempt(rec, fid, "http")
		return res, err
	case "timeout":
		if nonidem {
			res, err := t.Base.RoundTrip(req)
			if res != nil && res.Body != nil {
				_ = res.Body.Close()
			}
			_ = err
			rec.Reached = true
		}
		rem, hasDL := deadlineRemaining(req.Context())
		if !hasDL {
			rec.Failed = true
			noteAttempt(rec, fid, "http")
			time.Sleep(time.Duration(cfg.HangS * float64(time.Second)))
			triggerLiveHang(fid)
		}
		if rem > 0 && rem < 200*time.Millisecond {
			time.Sleep(rem)
		}
		rec.Failed = true
		noteAttempt(rec, fid, "http")
		return nil, &url.Error{Op: method, URL: req.URL.String(), Err: context.DeadlineExceeded}
	case "refused":
		rec.Failed = true
		noteAttempt(rec, fid, "http")
		return nil, &url.Error{Op: method, URL: req.URL.String(), Err: &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}}
	case "http5xx":
		rec.Failed = true
		noteAttempt(rec, fid, "http")
		return &http.Response{
			StatusCode: 503, Status: "503 Service Unavailable",
			Header: http.Header{"Content-Type": []string{"text/plain"}},
			Body:   io.NopCloser(strings.NewReader("Service Unavailable")),
			Request: req,
		}, nil
	case "http429":
		rec.Failed = true
		noteAttempt(rec, fid, "http")
		return &http.Response{
			StatusCode: 429, Status: "429 Too Many Requests",
			Header: http.Header{"Retry-After": []string{vRetryAfter}, "Content-Type": []string{"text/plain"}},
			Body:   io.NopCloser(strings.NewReader("Too Many Requests")),
			Request: req,
		}, nil
	}
	return t.Base.RoundTrip(req)
}

func (t *Transport) roundTripVerify(req *http.Request, key, method string, nonidem bool, idemPtr *string, n int, plan Plan) (*http.Response, error) {
	mu.Lock()
	var matched *IOEntry
	lastIdx := -1
	for i := range vIO {
		if k, _ := vIO[i].Req["key"].(string); k == key {
			lastIdx = i
			if !vIOUsed[i] {
				vIOUsed[i] = true
				matched = &vIO[i]
				break
			}
		}
	}
	if matched == nil && lastIdx >= 0 {
		matched = &vIO[lastIdx]
	}
	mu.Unlock()

	if matched == nil {
		panic(&ReplayMiss{Msg: "request the original never made: " + key})
	}

	fault := faultOf(plan, key, n)
	outage := isOutage(plan, key, n)
	var faultPtr *string
	if fault != "" {
		faultPtr = &fault
	}
	rec := AttemptRec{Key: key, N: n, Method: method, Nonidempotent: nonidem, Idem: idemPtr, Fault: faultPtr, Outage: outage}

	serveRecorded := func(trunc bool) (*http.Response, error) {
		Sleep(time.Duration(matched.Dur * float64(time.Second)))
		if exc, ok := matched.Out["exc"].(map[string]any); ok {
			rec.Failed = true
			noteAttempt(rec, "", "http")
			msg, _ := exc["msg"].(string)
			return nil, &url.Error{Op: method, URL: req.URL.String(), Err: errors.New(msg)}
		}
		statusF, _ := matched.Out["status"].(float64)
		status := int(statusF)
		if status == 0 {
			status = 200
		}
		hdr := http.Header{}
		if rawH, ok := matched.Out["headers"].([]any); ok {
			for _, item := range rawH {
				if pair, ok := item.([]any); ok && len(pair) == 2 {
					k, _ := pair[0].(string)
					v, _ := pair[1].(string)
					hdr.Add(k, v)
				}
			}
		}
		rb := decodeBytes(matched.Out["body"])
		if trunc && len(rb) > 1 {
			rb = rb[:len(rb)/2]
			rec.Failed = true
		} else {
			rec.Failed = status >= 500 || status == 429
		}
		rec.Reached = true
		noteAttempt(rec, "", "http")
		return &http.Response{
			StatusCode: status, Status: fmt.Sprintf("%d", status),
			Header: hdr, Body: io.NopCloser(bytes.NewReader(rb)), ContentLength: int64(len(rb)), Request: req,
		}, nil
	}

	if rem, hasDL := deadlineRemaining(req.Context()); hasDL && rem <= 0 {
		rec.Failed = true
		noteAttempt(rec, "", "http")
		return nil, &url.Error{Op: method, URL: req.URL.String(), Err: context.DeadlineExceeded}
	}
	switch fault {
	case "":
		return serveRecorded(false)
	case "slow":
		d := time.Duration(vSlowS * float64(time.Second))
		if rem, hasDL := deadlineRemaining(req.Context()); hasDL && rem < d {
			if rem > 0 {
				Sleep(rem)
			}
			rec.Failed = true
			noteAttempt(rec, "", "http")
			return nil, &url.Error{Op: method, URL: req.URL.String(), Err: context.DeadlineExceeded}
		}
		Sleep(d)
		return serveRecorded(false)
	case "truncated":
		return serveRecorded(true)
	case "timeout":
		if nonidem {
			rec.Reached = true
		}
		rem, hasDL := deadlineRemaining(req.Context())
		if !hasDL {
			rec.Failed = true
			noteAttempt(rec, "", "http")
			panic(&ChaosHang{Where: key})
		}
		if rem > 0 {
			Sleep(rem)
		}
		rec.Failed = true
		noteAttempt(rec, "", "http")
		return nil, &url.Error{Op: method, URL: req.URL.String(), Err: context.DeadlineExceeded}
	case "refused":
		rec.Failed = true
		noteAttempt(rec, "", "http")
		return nil, &url.Error{Op: method, URL: req.URL.String(), Err: &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}}
	case "http5xx":
		rec.Failed = true
		noteAttempt(rec, "", "http")
		return &http.Response{
			StatusCode: 503, Status: "503 Service Unavailable",
			Header: http.Header{"Content-Type": []string{"text/plain"}},
			Body: io.NopCloser(strings.NewReader("Service Unavailable")), Request: req,
		}, nil
	case "http429":
		rec.Failed = true
		noteAttempt(rec, "", "http")
		return &http.Response{
			StatusCode: 429, Status: "429 Too Many Requests",
			Header: http.Header{"Retry-After": []string{vRetryAfter}, "Content-Type": []string{"text/plain"}},
			Body: io.NopCloser(strings.NewReader("Too Many Requests")), Request: req,
		}, nil
	}
	return serveRecorded(false)
}

// --------------------------------------------------------------------------- Subprocess wrapper (os/exec)

type Cmd struct {
	ctx  context.Context
	name string
	args []string
	Dir  string
	Env  []string
}

func ExecCommand(name string, args ...string) *Cmd {
	return &Cmd{ctx: context.Background(), name: name, args: args}
}

func ExecCommandContext(ctx context.Context, name string, args ...string) *Cmd {
	return &Cmd{ctx: ctx, name: name, args: args}
}

func subKey(name string, args []string) string {
	parts := []string{"run", filepath.Base(name)}
	rootAbs := ""
	if cfg != nil && cfg.Root != "" {
		rootAbs, _ = filepath.Abs(cfg.Root)
	}
	for _, a := range args {
		if rootAbs != "" && filepath.IsAbs(a) {
			if rel, err := filepath.Rel(rootAbs, a); err == nil && !strings.HasPrefix(rel, "..") {
				a = rel
			}
		}
		parts = append(parts, a)
	}
	return strings.Join(parts, " ")
}

func (c *Cmd) CombinedOutput() ([]byte, error) {
	return c.Output()
}

func (c *Cmd) Output() ([]byte, error) {
	if cfg == nil && !verifyMode {
		cmd := exec.CommandContext(c.ctx, c.name, c.args...)
		cmd.Dir, cmd.Env = c.Dir, c.Env
		return cmd.Output()
	}
	key := subKey(c.name, c.args)
	mu.Lock()
	fid := innermostLocked()
	n := counts[key]
	counts[key] = n + 1
	plan := activePlan
	mu.Unlock()

	if verifyMode {
		return c.outputVerify(key, n, plan)
	}
	if cfg.Mode == "record" {
		cmd := exec.CommandContext(c.ctx, c.name, c.args...)
		cmd.Dir, cmd.Env = c.Dir, c.Env
		t0 := time.Now()
		var se bytes.Buffer
		cmd.Stderr = &se
		so, err := cmd.Output()
		dur := time.Since(t0).Seconds()
		mu.Lock()
		recStacks := recIOStacks[gid()]
		if len(recStacks) > 0 {
			tl := recStacks[len(recStacks)-1]
			argv := []string{filepath.Base(c.name)}
			for _, a := range c.args {
				if cfg.Root != "" && filepath.IsAbs(a) {
					if rel, rerr := filepath.Rel(cfg.Root, a); rerr == nil && !strings.HasPrefix(rel, "..") {
						a = rel
					}
				}
				argv = append(argv, a)
			}
			rc := 0
			if err != nil {
				var ee *exec.ExitError
				if errors.As(err, &ee) {
					rc = ee.ExitCode()
				} else {
					rc = 1
				}
			}
			*tl = append(*tl, IOEntry{
				Req: map[string]any{"kind": "subprocess", "method": "RUN", "url": "", "argv": argv, "key": key},
				Out: map[string]any{"returncode": rc, "stdout": encodeBytes(so), "stderr": encodeBytes(se.Bytes())},
				Dur: dur,
			})
		}
		mu.Unlock()
		return so, err
	}

	fault := faultOf(plan, key, n)
	outage := isOutage(plan, key, n)
	var fp *string
	if fault != "" {
		fp = &fault
	}
	rec := AttemptRec{Key: key, N: n, Method: "RUN", Nonidempotent: false, Fault: fp, Outage: outage}

	runLive := func() ([]byte, error) {
		cmd := exec.CommandContext(c.ctx, c.name, c.args...)
		cmd.Dir, cmd.Env = c.Dir, c.Env
		so, err := cmd.Output()
		rec.Reached = true
		rec.Failed = err != nil
		noteAttempt(rec, fid, "subprocess")
		return so, err
	}

	switch fault {
	case "":
		return runLive()
	case "slow":
		time.Sleep(time.Duration(vSlowS * cfg.Scale * float64(time.Second)))
		return runLive()
	case "truncated":
		so, err := runLive()
		if len(so) > 1 {
			so = so[:len(so)/2]
		}
		return so, err
	case "timeout":
		rem, hasDL := deadlineRemaining(c.ctx)
		if !hasDL {
			rec.Failed = true
			noteAttempt(rec, fid, "subprocess")
			time.Sleep(time.Duration(cfg.HangS * float64(time.Second)))
			triggerLiveHang(fid)
		}
		if rem > 0 && rem < 200*time.Millisecond {
			time.Sleep(rem)
		}
		rec.Failed = true
		noteAttempt(rec, fid, "subprocess")
		return nil, context.DeadlineExceeded
	case "refused":
		rec.Failed = true
		noteAttempt(rec, fid, "subprocess")
		return nil, syscall.EAGAIN
	case "http5xx", "http429":
		rec.Failed = true
		noteAttempt(rec, fid, "subprocess")
		return nil, fmt.Errorf("exit status 75")
	}
	return runLive()
}

func (c *Cmd) Run() error {
	_, err := c.Output()
	return err
}

func (c *Cmd) outputVerify(key string, n int, plan Plan) ([]byte, error) {
	mu.Lock()
	var matched *IOEntry
	lastIdx := -1
	for i := range vIO {
		if k, _ := vIO[i].Req["key"].(string); k == key {
			lastIdx = i
			if !vIOUsed[i] {
				vIOUsed[i] = true
				matched = &vIO[i]
				break
			}
		}
	}
	if matched == nil && lastIdx >= 0 {
		matched = &vIO[lastIdx]
	}
	mu.Unlock()
	if matched == nil {
		panic(&ReplayMiss{Msg: "subprocess the original never ran: " + key})
	}
	fault := faultOf(plan, key, n)
	outage := isOutage(plan, key, n)
	var fp *string
	if fault != "" {
		fp = &fault
	}
	rec := AttemptRec{Key: key, N: n, Method: "RUN", Fault: fp, Outage: outage}

	serve := func(trunc bool) ([]byte, error) {
		Sleep(time.Duration(matched.Dur * float64(time.Second)))
		rcF, _ := matched.Out["returncode"].(float64)
		rc := int(rcF)
		so := decodeBytes(matched.Out["stdout"])
		if trunc && len(so) > 1 {
			so = so[:len(so)/2]
			rec.Failed = true
		} else {
			rec.Failed = rc != 0
		}
		rec.Reached = true
		noteAttempt(rec, "", "subprocess")
		if rc != 0 {
			return so, fmt.Errorf("exit status %d", rc)
		}
		return so, nil
	}
	if rem, hasDL := deadlineRemaining(c.ctx); hasDL && rem <= 0 {
		rec.Failed = true
		noteAttempt(rec, "", "subprocess")
		return nil, context.DeadlineExceeded
	}
	switch fault {
	case "":
		return serve(false)
	case "slow":
		d := time.Duration(vSlowS * float64(time.Second))
		if rem, hasDL := deadlineRemaining(c.ctx); hasDL && rem < d {
			if rem > 0 {
				Sleep(rem)
			}
			rec.Failed = true
			noteAttempt(rec, "", "subprocess")
			return nil, context.DeadlineExceeded
		}
		Sleep(d)
		return serve(false)
	case "truncated":
		return serve(true)
	case "timeout":
		rem, hasDL := deadlineRemaining(c.ctx)
		if !hasDL {
			rec.Failed = true
			noteAttempt(rec, "", "subprocess")
			panic(&ChaosHang{Where: key})
		}
		if rem > 0 {
			Sleep(rem)
		}
		rec.Failed = true
		noteAttempt(rec, "", "subprocess")
		return nil, context.DeadlineExceeded
	case "refused":
		rec.Failed = true
		noteAttempt(rec, "", "subprocess")
		return nil, syscall.EAGAIN
	case "http5xx", "http429":
		rec.Failed = true
		noteAttempt(rec, "", "subprocess")
		return nil, fmt.Errorf("exit status 75")
	}
	return serve(false)
}

// --------------------------------------------------------------------------- cassette byte codec

func encodeBytes(b []byte) any {
	if b == nil {
		b = []byte{}
	}
	return map[string]any{"$bytes": base64.StdEncoding.EncodeToString(b)}
}

func decodeBytes(v any) []byte {
	if m, ok := v.(map[string]any); ok {
		if b64, ok := m["$bytes"].(string); ok {
			b, _ := base64.StdEncoding.DecodeString(b64)
			return b
		}
		if b64, ok := m["$str"].(string); ok {
			b, _ := base64.StdEncoding.DecodeString(b64)
			return b
		}
	}
	if s, ok := v.(string); ok {
		return []byte(s)
	}
	return []byte{}
}

// --------------------------------------------------------------------------- Exit / Finish

func ExitWith(code int) {
	Finish()
	os.Exit(code)
}

func LogFatal(v ...any) {
	log.Print(v...)
	ExitWith(1)
}

func LogFatalf(format string, v ...any) {
	log.Printf(format, v...)
	ExitWith(1)
}

func LogFatalln(v ...any) {
	log.Println(v...)
	ExitWith(1)
}

func Finish() {
	if cfg == nil {
		return
	}
	mu.Lock()
	if finished {
		mu.Unlock()
		return
	}
	finished = true

	if cfg.Mode == "record" && cfg.CallsPath != "" {
		cas := map[string]any{
			"format":  "ras-anything-cassette",
			"version": 1,
			"lang":    "go",
			"calls":   recordedCalls,
		}
		if cas["calls"] == nil {
			cas["calls"] = []RecordedCall{}
		}
		if b, err := json.MarshalIndent(cas, "", " "); err == nil {
			_ = os.MkdirAll(filepath.Dir(cfg.CallsPath), 0o755)
			_ = os.WriteFile(cfg.CallsPath, b, 0o644)
		}
	}

	var fns []*FnStat
	for _, f := range fnTable {
		fns = append(fns, f)
	}
	sort.Slice(fns, func(i, j int) bool { return fns[i].File < fns[j].File || (fns[i].File == fns[j].File && fns[i].Line < fns[j].Line) })

	for _, st := range benchStats {
		sort.Float64s(st.durs)
		if len(st.durs) > 0 {
			p50 := st.durs[len(st.durs)/2]
			p99 := st.durs[(len(st.durs)*99)/100]
			st.P50 = &p50
			st.P99 = &p99
		}
	}

	exe, _ := os.Executable()
	out := map[string]any{
		"protocol":       1,
		"mode":           cfg.Mode,
		"runtime":        map[string]any{"name": "Go", "version": strings.TrimPrefix(runtime.Version(), "go")},
		"executable":     exe,
		"uncaught":       uncaught,
		"boundary_total": boundaryTotal,
		"functions":      fns,
		"attempts":       attempts,
		"hangs":          hangs,
		"stats":          benchStats,
		"recorded":       len(recordedCalls),
		"seen":           seenRec,
		"skipped":        skippedRec,
	}
	if attempts == nil {
		out["attempts"] = []AttemptRec{}
	}
	if fns == nil {
		out["functions"] = []*FnStat{}
	}
	mu.Unlock()

	if cfg.Out != "" {
		if b, err := json.MarshalIndent(out, "", " "); err == nil {
			_ = os.WriteFile(cfg.Out, b, 0o644)
		}
	}
}

// --------------------------------------------------------------------------- Verify Replay Runner

type VerifyJob struct {
	Protocol  int            `json:"protocol"`
	Cassette  string         `json:"cassette"`
	Constants map[string]any `json:"constants"`
	Trials    []VerifyTrial  `json:"trials"`
	Out       string         `json:"out"`
}

func constFloat(m map[string]any, k string, def float64) float64 {
	if v, ok := m[k].(float64); ok && v > 0 {
		return v
	}
	return def
}

type VerifyTrial struct {
	Call int     `json:"call"`
	Side string  `json:"side"`
	Plan Plan    `json:"plan"`
	Seed int64   `json:"seed"`
	GapS float64 `json:"gap_s"`
}

type CassetteFile struct {
	Calls []struct {
		Args   []json.RawMessage `json:"args"`
		Result json.RawMessage   `json:"result"`
		Exc    *ExcRec           `json:"exc"`
		IO     []IOEntry         `json:"io"`
	} `json:"calls"`
}

// RunVerify drives verify trials on the virtual clock with stderr capture and writes job.Out.
func RunVerify(jobPath string, fallbackRepr any, resetFn func(), invoke func(side string, callIdx int, args []json.RawMessage) (val any, err error, isFallback bool)) {
	verifyMode = true
	_ = os.Setenv("RAS_ANYTHING_VERIFY", "1")
	http.DefaultTransport = NewTransport(baseTransport)
	jb, err := os.ReadFile(jobPath)
	if err != nil {
		panic(err)
	}
	var job VerifyJob
	if err := json.Unmarshal(jb, &job); err != nil {
		panic(err)
	}
	vSlowS = constFloat(job.Constants, "slow_s", 3.0)
	vHangCapS = constFloat(job.Constants, "hang_cap_s", 60.0)
	vOutageK = int(constFloat(job.Constants, "outage_k", 3))
	if ra, ok := job.Constants["retry_after"].(string); ok && ra != "" {
		vRetryAfter = ra
	}

	cb, err := os.ReadFile(job.Cassette)
	if err != nil {
		panic(err)
	}
	var cas CassetteFile
	if err := json.Unmarshal(cb, &cas); err != nil {
		panic(err)
	}

	results := make([]map[string]any, 0, len(job.Trials))
	vClock = vBase
	for _, tr := range job.Trials {
		if tr.GapS > 0 {
			vClock = vClock.Add(time.Duration(tr.GapS * float64(time.Second)))
		} else if resetFn != nil {
			resetFn()
		}
		c := cas.Calls[tr.Call]

		mu.Lock()
		vIO = c.IO
		vIOUsed = make([]bool, len(c.IO))
		activePlan = tr.Plan
		attempts = nil
		counts = map[string]int{}
		mu.Unlock()

		// Capture stderr during trial
		oldErr := os.Stderr
		rPipe, wPipe, _ := os.Pipe()
		os.Stderr = wPipe
		var errBuf bytes.Buffer
		doneCopy := make(chan struct{})
		go func() {
			_, _ = io.Copy(&errBuf, rPipe)
			close(doneCopy)
		}()

		tStart := Now()
		var outcome string
		var retVal any
		var retErr error
		var isFB bool
		var missMsg *string

		func() {
			defer func() {
				if r := recover(); r != nil {
					switch x := r.(type) {
					case *ChaosHang:
						outcome = "hang"
					case *ReplayMiss:
						outcome = "miss"
						m := x.Msg
						missMsg = &m
					default:
						outcome = "raise"
						retErr = fmt.Errorf("panic: %v", r)
					}
				}
			}()
			retVal, retErr, isFB = invoke(tr.Side, tr.Call, c.Args)
			if retErr != nil {
				outcome = "raise"
			} else {
				outcome = "return"
			}
		}()

		_ = wPipe.Close()
		os.Stderr = oldErr
		<-doneCopy
		_ = rPipe.Close()

		stderrTxt := errBuf.String()
		if len(stderrTxt) > 8000 {
			stderrTxt = stderrTxt[:8000]
		}
		logCount := 0
		for _, ln := range strings.Split(stderrTxt, "\n") {
			if strings.Contains(ln, `"trace_id"`) || strings.Contains(ln, `"TraceId"`) {
				logCount++
			}
		}

		lat := math.Round(Now().Sub(tStart).Seconds()*10000) / 10000
		if lat > vHangCapS && outcome == "return" {
			outcome = "hang"
		}

		matches := false
		if outcome == "return" && c.Exc == nil {
			bGot, _ := json.Marshal(retVal)
			var normGot, normExp any
			_ = json.Unmarshal(bGot, &normGot)
			_ = json.Unmarshal(c.Result, &normExp)
			matches = reflect.DeepEqual(normGot, normExp)
		} else if outcome == "raise" && c.Exc != nil {
			matches = true
		}

		mu.Lock()
		att := attempts
		if att == nil {
			att = []AttemptRec{}
		}
		mu.Unlock()

		structured := false
		if retErr != nil {
			et := fmt.Sprintf("%T", retErr)
			if strings.Contains(et, "RasError") {
				structured = true
			}
		}
		row := map[string]any{
			"outcome":     outcome,
			"exc":         nil,
			"exc_type":    nil,
			"latency":     lat,
			"matches":     matches,
			"is_fallback": isFB,
			"logs":        logCount,
			"structured":  structured,
			"attempts":    att,
			"miss":        missMsg,
			"stderr":      stderrTxt,
		}
		if retErr != nil {
			et := fmt.Sprintf("%T", retErr)
			es := retErr.Error()
			if strings.Contains(et, "RasError") {
				et = "RasError"
			}
			row["exc_type"] = et
			row["exc"] = es
		} else if outcome == "return" {
			row["exc"] = nil
			row["exc_type"] = nil
			bGot, _ := json.Marshal(retVal)
			sGot := string(bGot)
			if len(sGot) > 120 {
				sGot = sGot[:120]
			}
			row["result"] = sGot
		}
		results = append(results, row)
	}

	ans := map[string]any{
		"protocol": 1,
		"fallback": fallbackRepr,
		"results":  results,
	}
	b, _ := json.MarshalIndent(ans, "", " ")
	_ = os.WriteFile(job.Out, b, 0o644)
}
