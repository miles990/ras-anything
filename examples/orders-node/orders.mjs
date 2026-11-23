// The calls an order makes to the outside world: catalog, carrier, payments, label printer.
import { execFile } from "node:child_process";
import http from "node:http";
import { promisify } from "node:util";

const run = promisify(execFile);
const LABEL = new URL("./label.mjs", import.meta.url).pathname;
const api = () => process.env.SHOP_API_URL ?? "https://api.shop.example";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Catalog entry of one article. */
export async function getItem(sku) {
  const res = await fetch(`${api()}/v1/items/${encodeURIComponent(sku)}`);
  return res.json();
}

/** What shipping costs, or null when no carrier goes there. */
export function shippingQuote(country, grams) {
  return new Promise((resolve, reject) => {
    http.get(`${api()}/v1/rates?country=${country}&grams=${grams}`, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => resolve(res.statusCode === 200 ? JSON.parse(body) : null));
    }).on("error", reject);
  });
}

/** Charges one order. Returns the provider's charge record, or null if it didn't go through. */
export const charge = async (order, amountCents) => {
  for (let attempt = 0; attempt < 3; attempt++) { // the provider is flaky, so try a few times
    try {
      const res = await fetch(`${api()}/v1/charges`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order: order.id, amount_cents: amountCents, currency: "eur" }),
        signal: AbortSignal.timeout(10_000),
      });
      if (res.ok) return await res.json();
    } catch {}
    await sleep(300);
  }
  return null;
};

/** Prints the parcel label in a separate process; returns its size and checksum. */
export async function printLabel(order, chargeId) {
  const { stdout } = await run(process.execPath, [LABEL, JSON.stringify({ id: order.id, to: order.name, country: order.country, charge: chargeId })]);
  return JSON.parse(stdout);
}
