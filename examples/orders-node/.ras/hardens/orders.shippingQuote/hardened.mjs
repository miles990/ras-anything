// Hardened orders.shippingQuote, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`.
// Paths are relative to _ras_hardened/ next to the module: that is where enable puts this file.

import http from "node:http";
import { isTransient, logEvent, raiseForStatus, RasError, retry } from "./_runtime.cjs";

// No fallback: null already means "no carrier goes there", and the caller then charges the order without
// shipping. Answering null for "carrier API down" would undercharge, so a failed quote throws a RasError.
export const RAS_FALLBACK = null;

// The first attempt survives a 3 s slow answer and still leaves room for a retry. The whole call, backoff and
// Retry-After included, stays under the 5 s limit. Five attempts outlast a short outage.
const RETRY = {
  attempts: 5,
  baseMs: 100,
  capMs: 1000,
  timeoutMs: 3400,
  budgetMs: 4700,
  retryOn: (e) => isTransient(e) || e instanceof SyntaxError, // a 200 whose body was cut off doesn't parse
};

// not exported by the module, so this file has its own copy
const api = () => process.env.SHOP_API_URL ?? "https://api.shop.example";

/** GETs a URL; resolves to the response and its body, rejects on any error, also one in the middle of the body. */
function get(url, signal) {
  return new Promise((resolve, reject) => {
    http.get(url, { signal }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => resolve({ res, body }));
      res.on("error", reject);
    }).on("error", reject);
  });
}

async function requestQuote(url, signal) {
  const { res, body } = await get(url, signal);
  if (res.statusCode === 404) return null; // a real answer: no carrier goes there
  if (res.statusCode !== 200) {
    // The body is already read, so hand raiseForStatus a Response. It only throws for >= 400.
    await raiseForStatus(new Response(body || null, { status: res.statusCode, headers: flat(res.headers) }));
    throw new RasError("unexpected status from the carrier API", { context: { status: res.statusCode } });
  }
  return JSON.parse(body);
}

// Node gives repeated headers as arrays; Headers wants strings.
const flat = (headers) => Object.entries(headers).map(([name, value]) => [name, String(value)]);

/** What shipping costs, or null when no carrier goes there. Throws a RasError when the carrier API doesn't answer. */
export async function shippingQuote(country, grams) {
  const url = `${api()}/v1/rates?country=${country}&grams=${grams}`;
  try {
    return await retry(({ signal }) => requestQuote(url, signal), RETRY);
  } catch (e) {
    const context = { country, grams, url };
    const problem = new RasError("shipping quote failed", { cause: e, dependency: "rates" }).problem;
    logEvent("shipping_quote.failed", { ...context, problem });
    throw new RasError("no shipping quote: the carrier API failed", { cause: e, context });
  }
}
