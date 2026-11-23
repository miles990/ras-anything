// A local stand-in for a shop platform (catalog, payments, carrier), so the example runs offline.
// Started in-process by app.mjs on a free port. Like real payment providers it honours an Idempotency-Key
// header: a repeated key returns the original charge instead of charging again. Without one, every POST that
// arrives is a new charge. GET /v1/ledger reports them, so duplicate charges show up in the app's output.
import http from "node:http";

const CATALOG = {
  "TEE-M": { name: "T-shirt (M)", price_cents: 1900, grams: 180 },
  "MUG-01": { name: "Mug", price_cents: 1250, grams: 340 },
  "BAG-XL": { name: "Tote bag", price_cents: 2400, grams: 260 },
  "PEN-3": { name: "Pen set", price_cents: 800, grams: 90 },
};
const RATES = { DE: 490, FR: 520, TW: 1180, US: 990 }; // cents per 500 g; no carrier anywhere else

export function start() {
  const charges = [];
  const byKey = new Map();

  function route(req, body) {
    const url = new URL(req.url, "http://localhost");
    const parts = url.pathname.split("/").filter(Boolean); // v1, kind, id
    if (req.method === "GET" && parts[1] === "items") {
      const item = CATALOG[parts[2]];
      return item ? [200, { sku: parts[2], ...item }] : [404, { error: "no such item" }];
    }
    if (req.method === "GET" && parts[1] === "rates") {
      const rate = RATES[url.searchParams.get("country")];
      return rate ? [200, { carrier: "POST", cents: rate * Math.ceil(Number(url.searchParams.get("grams")) / 500) }] : [404, { error: "no carrier" }];
    }
    if (req.method === "POST" && parts[1] === "charges") {
      const doc = JSON.parse(body || "{}");
      if (!doc.order || !(doc.amount_cents > 0)) return [400, { error: "invalid charge" }];
      const key = req.headers["idempotency-key"];
      if (key && byKey.has(key)) return [200, byKey.get(key)];
      const charge = { id: `ch_${String(charges.length + 1).padStart(4, "0")}`, order: doc.order, amount_cents: doc.amount_cents, status: "succeeded" };
      charges.push(charge);
      if (key) byKey.set(key, charge);
      return [200, charge];
    }
    if (req.method === "GET" && parts[1] === "ledger") {
      const orders = new Set(charges.map((c) => c.order));
      return [200, { charges: charges.length, duplicates: charges.length - orders.size, total_cents: charges.reduce((n, c) => n + c.amount_cents, 0) }];
    }
    return [404, { error: "not found" }];
  }

  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        const [status, doc] = route(req, Buffer.concat(chunks).toString());
        const body = JSON.stringify(doc);
        res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
        res.end(body);
      });
    });
    server.listen(0, "127.0.0.1", () => resolve({
      url: `http://127.0.0.1:${server.address().port}`,
      stop() {
        server.close();
        server.closeAllConnections();
      },
    }));
  });
}
