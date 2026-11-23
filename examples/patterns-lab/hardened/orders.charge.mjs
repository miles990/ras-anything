// orders.charge on a Policy, for the pattern lab: the marked line is replaced per variant.
import { HttpError, idempotencyKey, isTransient, Policy, raiseForStatus } from "./_runtime.cjs";

export const RAS_FALLBACK = () => null;
const retryable = (e) => isTransient(e) || e instanceof SyntaxError;
const BASE = { deadline: { timeoutMs: 4700 }, attemptTimeout: { timeoutMs: 3400 }, hedge: { delayMs: 500 }, retry: { attempts: 4, retryOn: retryable },
  fallback: { degraded: () => null, when: (e) => retryable(e) || e instanceof HttpError } };
const PATTERNS = {}; // ras-lab

const POLICY = Object.fromEntries(Object.entries(PATTERNS).map(([k, v]) => [k, v ? { ...BASE[k], ...(v === true ? {} : v) } : false]));
const payments = new Policy("payments-api", POLICY);
const api = () => process.env.SHOP_API_URL ?? "https://api.shop.example";

export const charge = async (order, amountCents) => {
  const body = JSON.stringify({ order: order.id, amount_cents: amountCents, currency: "eur" });
  return payments.call(async ({ signal, headers }) => (await raiseForStatus(await fetch(`${api()}/v1/charges`, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers }, body, signal }))).json(),
  { method: "POST", idempotencyKey: idempotencyKey("charge", order.id, amountCents) });
};
