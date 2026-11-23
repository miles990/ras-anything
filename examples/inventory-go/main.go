package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"

	"inventorygo/warehouse"
)

func startWarehouseServer() *httptest.Server {
	var mu sync.Mutex
	stock := map[string]warehouse.Stock{
		"sku-101": {SKU: "sku-101", Available: 3, Reorder: 10},
		"sku-102": {SKU: "sku-102", Available: 8, Reorder: 10},
		"sku-103": {SKU: "sku-103", Available: 1, Reorder: 12},
		"sku-104": {SKU: "sku-104", Available: 5, Reorder: 10},
		"sku-105": {SKU: "sku-105", Available: 2, Reorder: 8},
		"sku-106": {SKU: "sku-106", Available: 4, Reorder: 10},
	}
	seenIdem := map[string]warehouse.Reservation{}
	seq := 0

	mux := http.NewServeMux()
	mux.HandleFunc("/stock/", func(w http.ResponseWriter, r *http.Request) {
		sku := strings.TrimPrefix(r.URL.Path, "/stock/")
		mu.Lock()
		st, ok := stock[sku]
		mu.Unlock()
		if !ok {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(st)
	})
	mux.HandleFunc("/reserve", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			SKU string `json:"sku"`
			Qty int    `json:"qty"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad body", 400)
			return
		}
		idem := r.Header.Get("Idempotency-Key")
		mu.Lock()
		defer mu.Unlock()
		if idem != "" {
			if prev, ok := seenIdem[idem]; ok {
				_ = json.NewEncoder(w).Encode(prev)
				return
			}
		}
		seq++
		res := warehouse.Reservation{
			SKU:     req.SKU,
			Qty:     req.Qty,
			OrderID: fmt.Sprintf("RES-%03d", seq),
		}
		if idem != "" {
			seenIdem[idem] = res
		}
		_ = json.NewEncoder(w).Encode(res)
	})
	return httptest.NewServer(mux)
}

func main() {
	srv := startWarehouseServer()
	defer srv.Close()

	skus := []string{"sku-101", "sku-102", "sku-103", "sku-104", "sku-105", "sku-106"}
	for _, sku := range skus {
		st, err := warehouse.FetchStock(srv.URL, sku)
		if err != nil {
			fmt.Fprintf(os.Stderr, "fetch %s failed: %v\n", sku, err)
			os.Exit(1)
		}
		need := st.Reorder - st.Available
		if need < 0 {
			need = 0
		}
		res, err := warehouse.ReserveStock(srv.URL, sku, need)
		if err != nil {
			fmt.Fprintf(os.Stderr, "reserve %s failed: %v\n", sku, err)
			os.Exit(1)
		}
		tag, err := warehouse.AuditTag(sku)
		if err != nil {
			fmt.Fprintf(os.Stderr, "audit %s failed: %v\n", sku, err)
			os.Exit(1)
		}
		b, _ := json.Marshal(map[string]any{
			"sku":       sku,
			"available": st.Available,
			"ordered":   res.Qty,
			"order_id":  res.OrderID,
			"tag":       tag,
		})
		fmt.Println(string(b))
	}
}
