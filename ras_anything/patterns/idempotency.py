"""Idempotency-Key: one key per logical operation, the same in every attempt, so a repeated write happens once."""

from __future__ import annotations

import hashlib
import json

from .. import runtime as rt
from . import ADOPTED, Layer, Pattern


def idempotency_key(operation: str, *parts) -> str:
    """A key for one logical operation, from what identifies it: idempotency_key("charge", invoice_id).

    Deterministic (the same in every attempt, process and language) and shaped like a UUID. Parts are strings,
    ints, bools or None. Use what makes the *operation* unique; a timestamp or a random number would make every
    attempt a new one, and leaving out the amount would let a changed request reuse the key (the server's 422)."""
    for p in (operation,) + parts:
        if not isinstance(p, (str, int, bool, type(None))):
            raise TypeError("idempotency_key takes strings, ints, bools and None, not %s" % type(p).__name__)
    text = json.dumps([operation, *parts], separators=(",", ":"), ensure_ascii=False)
    h = hashlib.sha256(text.encode()).hexdigest()
    return "%s-%s-8%s-%s%s-%s" % (h[:8], h[8:12], h[13:16], "89ab"[int(h[16], 16) % 4], h[17:20], h[20:32])


def is_idempotency_conflict(e: BaseException) -> bool:
    """409: the first request with this key is still being processed. Ask again a little later, same key."""
    return rt._http_status(e) == 409


def is_idempotency_mismatch(e: BaseException) -> bool:
    """422: the key was used before for a different request. Never retry: it is a bug in how the key is made."""
    return rt._http_status(e) == 422


class IdempotencyLayer(Layer):
    """Puts the call's idempotency_key on every attempt (attempt.headers), which is what allows the retry layer
    to repeat a write at all, and makes a 409 worth another attempt. The header is a structured-field string,
    "quoted", as the draft says; quoted=False sends the bare key."""

    def run(self, call, inner):
        if call.idempotency_key is not None:
            call.carry_key = call.idempotency_key
            call.headers["Idempotency-Key"] = '"%s"' % call.idempotency_key if self.params["quoted"] else call.idempotency_key
            call.retry_rules.append(is_idempotency_conflict)
        return (yield from inner(call))


PATTERN = Pattern(
    name="idempotency", title="Idempotency-Key per logical operation", order=40, verdict=ADOPTED,
    hint="a write is repeated only if the call has an idempotency key; every attempt sends it (send the attempt's headers). 409 is asked again, 422 never.",
    params={"quoted": True},
    layer=IdempotencyLayer, exports=("idempotency_key", "is_idempotency_conflict", "is_idempotency_mismatch"),
    refs=("IETF draft-ietf-httpapi-idempotency-key-header", "AWS Builders' Library: Making retries safe with idempotent APIs",
          "RFC 9110 section 9.2.2: idempotent methods"))
