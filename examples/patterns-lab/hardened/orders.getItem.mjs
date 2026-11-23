// orders.getItem on a Policy, for the pattern lab: the marked line is replaced per variant.
import { isTransient, Policy, raiseForStatus } from "./_runtime.cjs";

export const RAS_FALLBACK = null;
const BASE = { deadline: { timeoutMs: 4700 }, attemptTimeout: { timeoutMs: 3400 }, hedge: { delayMs: 500 },
  retry: { attempts: 4, retryOn: (e) => isTransient(e) || e instanceof SyntaxError } };
const PATTERNS = {}; // ras-lab

const POLICY = Object.fromEntries(Object.entries(PATTERNS).map(([k, v]) => [k, v ? { ...BASE[k], ...(v === true ? {} : v) } : false]));
const shop = new Policy("shop-api", { ...POLICY, ...(POLICY.fallback ? { fallback: { ...POLICY.fallback, when: BASE.retry.retryOn } } : {}) });
const api = () => process.env.SHOP_API_URL ?? "https://api.shop.example";

export async function getItem(sku) {
  return shop.call(async ({ signal }) => (await raiseForStatus(await fetch(`${api()}/v1/items/${encodeURIComponent(sku)}`, { signal }))).json(), { cacheKey: sku });
}
