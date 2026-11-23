package warehouse

import (
	"context"
	"encoding/json"
	"net/http"
	"time"
	_ras_rt "inventorygo/warehouse/_ras_hardened"
)

var fetchBreaker = _ras_rt.NewCircuitBreaker("stock-api", 5, 15*time.Second)

func FetchStock(base, sku string) (Stock, error) {
	var zero Stock
	if err := fetchBreaker.Allow(); err != nil {
		re := &_ras_rt.RasError{Message: "stock circuit open", Category: "circuit_open", Dependency: "stock-api", Cause: err}
		_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
			"ras.dependency": "stock-api",
			"ras.fault":      "circuit_open",
			"problem":        re.Problem(),
		})
		return zero, re
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4700*time.Millisecond)
	defer cancel()
	client := &http.Client{}
	var st Stock
	err := _ras_rt.Retry(ctx, _ras_rt.RetryOpts{
		Attempts: 4, Base: 50 * time.Millisecond, Cap: 1500 * time.Millisecond,
		Budget: 4700 * time.Millisecond, Dependency: "stock-api",
		RetryOn: func(e error) bool { return _ras_rt.IsTransient(e) },
	}, func(actx context.Context) error {
		actx, acancel := context.WithTimeout(actx, 3400*time.Millisecond)
		defer acancel()
		req, rerr := http.NewRequestWithContext(actx, "GET", base+"/stock/"+sku, nil)
		if rerr != nil {
			return rerr
		}
		res, rerr := client.Do(req)
		if rerr != nil {
			return rerr
		}
		defer res.Body.Close()
		if res.StatusCode >= 400 {
			return &_ras_rt.HTTPError{StatusCode: res.StatusCode, Header: res.Header, URL: req.URL.String()}
		}
		return json.NewDecoder(res.Body).Decode(&st)
	})
	if err != nil {
		fetchBreaker.Failure()
		re := &_ras_rt.RasError{Message: "stock lookup failed", Dependency: "stock-api", Cause: err}
		_ras_rt.LogEvent("ras.dependency.gave_up", map[string]any{
			"ras.dependency": "stock-api",
			"ras.fault":      _ras_rt.FaultClass(err),
			"problem":        re.Problem(),
		})
		return zero, re
	}
	fetchBreaker.Success()
	return st, nil
}
