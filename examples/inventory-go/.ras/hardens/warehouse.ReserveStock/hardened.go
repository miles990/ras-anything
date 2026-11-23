package warehouse

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"time"
	_ras_rt "inventorygo/warehouse/_ras_hardened"
)

var reserveBreaker = _ras_rt.NewCircuitBreaker("reserve-api", 5, 15*time.Second)

func ReserveStock(base, sku string, qty int) (Reservation, error) {
	var zero Reservation
	if err := reserveBreaker.Allow(); err != nil {
		re := &_ras_rt.RasError{Message: "reserve circuit open", Category: "circuit_open", Dependency: "reserve-api", Cause: err}
		_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
			"ras.dependency": "reserve-api",
			"ras.fault":      "circuit_open",
			"problem":        re.Problem(),
		})
		return zero, re
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4700*time.Millisecond)
	defer cancel()
	client := &http.Client{}
	body, _ := json.Marshal(map[string]any{"sku": sku, "qty": qty})
	idem := _ras_rt.IdempotencyKey("reserve", sku, qty)
	var out Reservation
	err := _ras_rt.Retry(ctx, _ras_rt.RetryOpts{
		Attempts: 4, Base: 50 * time.Millisecond, Cap: 1500 * time.Millisecond,
		Budget: 4700 * time.Millisecond, Dependency: "reserve-api",
		RetryOn: func(e error) bool { return _ras_rt.IsTransient(e) },
	}, func(actx context.Context) error {
		actx, acancel := context.WithTimeout(actx, 3400*time.Millisecond)
		defer acancel()
		req, rerr := http.NewRequestWithContext(actx, "POST", base+"/reserve", bytes.NewReader(body))
		if rerr != nil {
			return rerr
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Idempotency-Key", idem)
		res, rerr := client.Do(req)
		if rerr != nil {
			return rerr
		}
		defer res.Body.Close()
		if res.StatusCode >= 400 {
			return &_ras_rt.HTTPError{StatusCode: res.StatusCode, Header: res.Header, URL: req.URL.String()}
		}
		return json.NewDecoder(res.Body).Decode(&out)
	})
	if err != nil {
		reserveBreaker.Failure()
		re := &_ras_rt.RasError{Message: "reserve request failed", Dependency: "reserve-api", Cause: err}
		_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
			"ras.dependency": "reserve-api",
			"ras.fault":      _ras_rt.FaultClass(err),
			"problem":        re.Problem(),
		})
		return zero, re
	}
	reserveBreaker.Success()
	return out, nil
}
