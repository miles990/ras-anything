// Hardened orders.getItem, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`.
// Paths are relative to _ras_hardened/ next to the module: that is where enable puts this file.

import { HttpError, isTransient, logEvent, raiseForStatus, RasError, retry } from "./_runtime.cjs";

// No fallback: callers price and charge an order from this item, so a made-up one would be charged to a customer.
// null means no fallback: throw a RasError instead.
export const RAS_FALLBACK = null;

// Four attempts ride out a short outage. One attempt may take 3.5 s (a slow catalog still answers);
// everything, waits included, stays under the 5 s limit.
const RETRY = { attempts: 4, baseMs: 100, capMs: 1000, timeoutMs: 3500, budgetMs: 4700 };

// A 2xx body that doesn't parse was cut off on the way: worth another attempt.
const retryOn = (e) => isTransient(e) || e instanceof SyntaxError;

// not exported by the module, so this file has its own copy
const api = () => process.env.SHOP_API_URL ?? "https://api.shop.example";

export async function getItem(sku) {
  const url = `${api()}/v1/items/${encodeURIComponent(sku)}`;
  let attempts = 0;
  try {
    return await retry(async ({ attempt, signal }) => {
      attempts = attempt;
      const res = await raiseForStatus(await fetch(url, { signal }));
      return res.json();
    }, { ...RETRY, retryOn });
  } catch (e) {
    if (!retryOn(e) && !(e instanceof HttpError)) throw e; // a bug, not the catalog
    const context = { sku, url, attempts, status: e.status ?? null };
    const error = new RasError("catalog item unavailable", { cause: e, context });
    logEvent("orders.getItem.give_up", { ...context, error: e, trace_id: error.traceId });
    throw error;
  }
}
