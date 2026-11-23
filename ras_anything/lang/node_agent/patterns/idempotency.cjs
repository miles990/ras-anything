// Idempotency-Key: one key per logical operation, the same in every attempt, so a repeated write happens once.
"use strict";
const { createHash } = require("node:crypto");
const core = require("./core.cjs");

/** A key for one logical operation, from what identifies it: idempotencyKey("charge", invoiceId).
 *
 *  Deterministic (the same in every attempt, process and language) and shaped like a UUID. Parts are strings, integers,
 *  booleans or null. Use what makes the *operation* unique; a timestamp or a random number would make every attempt a
 *  new one, and leaving out the amount would let a changed request reuse the key (the server's 422). */
function idempotencyKey(operation, ...parts) {
  for (const p of [operation, ...parts]) {
    if (!(p === null || typeof p === "string" || typeof p === "boolean" || Number.isSafeInteger(p))) {
      throw new TypeError(`idempotencyKey takes strings, integers, booleans and null, not ${typeof p}`);
    }
  }
  const h = createHash("sha256").update(JSON.stringify([operation, ...parts])).digest("hex");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-8${h.slice(13, 16)}-${"89ab"[parseInt(h[16], 16) % 4]}${h.slice(17, 20)}-${h.slice(20, 32)}`;
}

/** 409: the first request with this key is still being processed. Ask again a little later, same key. */
const isIdempotencyConflict = (e) => core.httpStatus(e) === 409;

/** 422: the key was used before for a different request. Never retry: it is a bug in how the key is made. */
const isIdempotencyMismatch = (e) => core.httpStatus(e) === 422;

/** Puts the call's idempotencyKey on every attempt (its `headers`), which is what allows the retry layer to repeat a
 *  write at all, and makes a 409 worth another attempt. The header is a structured-field string, "quoted", as the IETF
 *  draft says; quoted: false sends the bare key. */
class IdempotencyLayer extends core.Layer {
  * run(call, inner) {
    if (call.idempotencyKey !== null && call.idempotencyKey !== undefined) {
      call.carryKey = call.idempotencyKey;
      call.headers["Idempotency-Key"] = this.params.quoted ? `"${call.idempotencyKey}"` : call.idempotencyKey;
      call.retryRules.push(isIdempotencyConflict);
    }
    return yield* inner(call);
  }
}

const PATTERN = { name: "idempotency", order: 40, verdict: core.ADOPTED, params: { quoted: true }, Layer: IdempotencyLayer };
module.exports = { PATTERN, idempotencyKey, isIdempotencyConflict, isIdempotencyMismatch };
