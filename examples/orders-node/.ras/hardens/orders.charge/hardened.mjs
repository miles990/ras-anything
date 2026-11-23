// Hardened orders.charge, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`.
// Paths are relative to _ras_hardened/ next to the module: that is where enable puts this file.

import { HttpError, isTransient, logEvent, raiseForStatus, RasError, retry } from "./_runtime.cjs";

// What this function returns when it degrades on purpose (dependency down, budget spent).
// The app already handles null: it lists the order as pending and retries it later.
export const RAS_FALLBACK = () => null;

// One attempt gets 3.5 s, so a provider that is merely slow (3 s) still answers in time. The whole call,
// backoff and Retry-After waits included, stays under 5 s. Five attempts outlast a short outage.
const RETRY = { attempts: 5, baseMs: 100, capMs: 1000, timeoutMs: 3500, budgetMs: 4600 };

// not exported by the module, so this file has its own copy
const api = () => process.env.SHOP_API_URL ?? "https://api.shop.example";

// A 2xx whose body was cut off parses as a SyntaxError: the charge may exist, so ask again (same key).
const isRetryable = (err) => isTransient(err) || err instanceof SyntaxError;

/** Charges one order. Returns the provider's charge record, or null if it didn't go through. */
export const charge = async (order, amountCents) => {
  const url = `${api()}/v1/charges`;
  const body = JSON.stringify({ order: order.id, amount_cents: amountCents, currency: "eur" });
  // An order is charged once, so its id names the operation: retries here, and a rerun of a pending
  // order later, reuse the key, and the provider answers with the first charge instead of a second one.
  const idempotencyKey = `charge-${order.id}`;
  let attempts = 0;

  try {
    return await retry(async ({ signal }) => {
      attempts++;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
        body,
        signal,
      });
      await raiseForStatus(res);
      return await res.json();
    }, { ...RETRY, retryOn: isRetryable });
  } catch (err) {
    const context = { order: order.id, amountCents, idempotencyKey, attempts, status: err?.status, error: String(err) };
    // Provider down, too slow or refusing the charge: "didn't go through" is the null the caller handles.
    if (isRetryable(err) || err instanceof HttpError) {
      logEvent("charge_not_confirmed", context);
      return RAS_FALLBACK(order, amountCents);
    }
    // Anything else is a bug on our side; never disguise it as a failed payment.
    logEvent("charge_failed", context);
    throw new RasError(`charging order ${order.id} failed`, { cause: err, context });
  }
};
