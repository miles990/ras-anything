package warehouse

import (
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"time"
	_ras_rt "inventorygo/warehouse/_ras_hardened"
)

var RAS_FALLBACK = "BIN-UNAVAILABLE"
var auditBreaker = _ras_rt.NewCircuitBreaker("audit-cli", 5, 15*time.Second)

func AuditTag(sku string) (string, error) {
	if err := auditBreaker.Allow(); err != nil {
		re := &_ras_rt.RasError{Message: "audit circuit open", Category: "circuit_open", Dependency: "audit-cli", Cause: err}
		_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
			"ras.dependency": "audit-cli",
			"ras.fault":      "circuit_open",
			"problem":        re.Problem(),
		})
		return RAS_FALLBACK, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4700*time.Millisecond)
	defer cancel()
	var tag string
	err := _ras_rt.Retry(ctx, _ras_rt.RetryOpts{
		Attempts: 4, Base: 50 * time.Millisecond, Cap: 1500 * time.Millisecond,
		Budget: 4700 * time.Millisecond, Dependency: "audit-cli",
		RetryOn: func(e error) bool { return _ras_rt.IsTransient(e) },
	}, func(actx context.Context) error {
		actx, acancel := context.WithTimeout(actx, 3400*time.Millisecond)
		defer acancel()
		out, rerr := exec.CommandContext(actx, "sh", "-c", fmt.Sprintf(`printf '{"tag":"BIN-%s"}'`, sku)).Output()
		if rerr != nil {
			return rerr
		}
		var parsed struct {
			Tag string `json:"tag"`
		}
		if jerr := json.Unmarshal(out, &parsed); jerr != nil {
			return jerr
		}
		tag = parsed.Tag
		return nil
	})
	if err != nil {
		auditBreaker.Failure()
		re := &_ras_rt.RasError{Message: "audit helper failed", Dependency: "audit-cli", Cause: err}
		_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
			"ras.dependency": "audit-cli",
			"ras.fault":      _ras_rt.FaultClass(err),
			"problem":        re.Problem(),
		})
		return RAS_FALLBACK, nil
	}
	auditBreaker.Success()
	return tag, nil
}
