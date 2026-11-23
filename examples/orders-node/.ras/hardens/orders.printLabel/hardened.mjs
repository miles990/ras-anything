// Hardened orders.printLabel, written for ras-anything (see TASK.md). Swapped in by `ras-anything enable`.
// Paths are relative to _ras_hardened/ next to the module: that is where enable puts this file.

import { isTransient, logEvent, RasError, retry, scope } from "./_runtime.cjs";

// module-level names the original uses (the module's own, live)
const { LABEL, run } = scope("orders.printLabel");

// No fallback: the caller reads label.bytes and label.sha256, and a made-up label would be wrong data.
export const RAS_FALLBACK = null;

const BUDGET_MS = 4700; // the whole call, backoff included; must stay under the 5 s contract
const ATTEMPT_MS = 3400; // lets a slow but working printer (about 3 s) finish; a hang still leaves time for a retry
const ATTEMPTS = 5; // refused attempts fail at once, so a short outage fits in the budget

// A label cut off mid-print is exit 0 with broken JSON: as transient as a timeout.
const retryable = (e) => isTransient(e) || e instanceof SyntaxError;

export async function printLabel(order, chargeId) {
  // Built outside the retry: a malformed order is a programming error, not a printer failure.
  const job = JSON.stringify({ id: order.id, to: order.name, country: order.country, charge: chargeId });
  let attempts = 0;
  try {
    // Printing again is safe: label.mjs only computes and prints, it stores and charges nothing.
    return await retry(
      async ({ attempt, signal }) => {
        attempts = attempt;
        const { stdout } = await run(process.execPath, [LABEL, job], { signal }); // the signal also kills the child
        return JSON.parse(stdout);
      },
      { attempts: ATTEMPTS, baseMs: 100, capMs: 500, timeoutMs: ATTEMPT_MS, budgetMs: BUDGET_MS, retryOn: retryable },
    );
  } catch (e) {
    const context = { order: order.id, charge: chargeId, attempts };
    // Not the message: execFile puts the whole command line in it, the customer's name included.
    const problem = new RasError("label printer unavailable", { cause: e, dependency: "label.mjs" }).problem;
    logEvent("print_label_failed", { ...context, transient: retryable(e), error: e?.name, code: e?.code ?? null, signal: e?.signal ?? null, problem });
    if (!retryable(e)) throw e; // a bad label job or a bug: the original error says it best
    throw new RasError("label printer unavailable", { cause: e, context });
  }
}
