package warehouse

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os/exec"
)

type Stock struct {
	SKU       string `json:"sku"`
	Available int    `json:"available"`
	Reorder   int    `json:"reorder"`
}

type Reservation struct {
	SKU     string `json:"sku"`
	Qty     int    `json:"qty"`
	OrderID string `json:"order_id"`
}

// FetchStock reads current inventory levels over HTTP (no timeout, no retry).
func FetchStock(base, sku string) (Stock, error) {
	var zero Stock
	res, err := http.Get(base + "/stock/" + sku)
	if err != nil {
		return zero, err
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return zero, fmt.Errorf("stock HTTP %d", res.StatusCode)
	}
	var st Stock
	if err := json.NewDecoder(res.Body).Decode(&st); err != nil {
		return zero, err
	}
	return st, nil
}

// ReserveStock places a restock reservation via POST without timeout, retry, or Idempotency-Key.
func ReserveStock(base, sku string, qty int) (Reservation, error) {
	var zero Reservation
	body, _ := json.Marshal(map[string]any{"sku": sku, "qty": qty})
	res, err := http.Post(base+"/reserve", "application/json", bytes.NewReader(body))
	if err != nil {
		return zero, err
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return zero, fmt.Errorf("reserve HTTP %d", res.StatusCode)
	}
	var out Reservation
	if err := json.NewDecoder(res.Body).Decode(&out); err != nil {
		return zero, err
	}
	return out, nil
}

// AuditTag runs an external CLI helper to compute a warehouse bin tag (no timeout, no retry).
func AuditTag(sku string) (string, error) {
	out, err := exec.Command("sh", "-c", fmt.Sprintf(`printf '{"tag":"BIN-%s"}'`, sku)).Output()
	if err != nil {
		return "", err
	}
	var parsed struct {
		Tag string `json:"tag"`
	}
	if err := json.Unmarshal(out, &parsed); err != nil {
		return "", err
	}
	return parsed.Tag, nil
}
