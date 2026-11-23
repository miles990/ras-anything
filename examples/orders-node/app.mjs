// Evening order run: price, ship, charge and label a batch of orders, then reconcile with the ledger.
// Starts its own local shop API (upstream.mjs), so it works offline and prints the same thing every time.
import { charge, getItem, printLabel, shippingQuote } from "./orders.mjs";
import { start } from "./upstream.mjs";

const NAMES = ["A. Kaya", "B. Lin", "C. Moreau", "D. Novak"];
const SKUS = ["TEE-M", "MUG-01", "BAG-XL", "PEN-3"];
const COUNTRIES = ["DE", "FR", "TW", "US", "IS"];
const orders = Array.from({ length: 12 }, (_, i) => ({
  id: `ORD-${2001 + i}`, name: NAMES[i % NAMES.length], country: COUNTRIES[i % COUNTRIES.length], sku: SKUS[(i * 3) % SKUS.length], qty: 1 + (i % 3),
}));
const eur = (cents) => (cents / 100).toFixed(2).padStart(7);

const upstream = await start();
process.env.SHOP_API_URL = upstream.url;
console.log(`ORDER RUN  ${orders.length} orders`);
let paid = 0;
for (const order of orders) {
  const item = await getItem(order.sku);
  const goods = item.price_cents * order.qty;
  const quote = await shippingQuote(order.country, item.grams * order.qty);
  const total = goods + (quote?.cents ?? 0);
  const shipping = quote ? eur(quote.cents) : " pickup";
  const record = await charge(order, total);
  if (!record) {
    console.log(`  ${order.id}  ${eur(goods)} + ${shipping}  PENDING (payment failed, retry tomorrow)`);
    continue;
  }
  paid++;
  const label = await printLabel(order, record.id);
  console.log(`  ${order.id}  ${eur(goods)} + ${shipping}  paid ${record.id}  label ${label.bytes}B ${label.sha256.slice(0, 8)}`);
}
const ledger = await (await fetch(`${upstream.url}/v1/ledger`, { signal: AbortSignal.timeout(5000) })).json();
console.log(`${paid}/${orders.length} paid · ledger: ${ledger.charges} charges, ${ledger.duplicates} duplicate, total${eur(ledger.total_cents)} EUR`);
upstream.stop();
