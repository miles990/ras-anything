// Retry-After: when a 429 or 503 says when to come back, wait that long, or give up at once if that is too late.
"use strict";
const core = require("./core.cjs");

/** The Retry-After delay (seconds) an HTTP error or Response asked for, if any. */
function retryAfterS(err) {
  const h = err?.headers;
  const value = typeof h?.get === "function" ? h.get("retry-after") : h?.["retry-after"];
  if (value === undefined || value === null || value === "") return null;
  const n = Number(value);
  if (Number.isFinite(n)) return Math.max(0, n);
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : Math.max(0, (at - Date.now()) / 1000);
}

/** Makes the retry layer's wait at least what the server asked for (a wait past the deadline ends the call). Such a retry
 *  is the server's own plan, so it doesn't need a token of the retry budget. */
class RetryAfterLayer extends core.Layer {
  * run(call, inner) {
    call.delayRules.push((e, delay) => {
      const asked = retryAfterS(e);
      call.invited = asked !== null;
      return asked === null ? delay : Math.max(delay, asked * 1000);
    });
    return yield* inner(call);
  }
}

const PATTERN = { name: "retry_after", order: 60, verdict: core.ADOPTED, params: {}, Layer: RetryAfterLayer };
module.exports = { PATTERN, retryAfterS };
