"""Fallback hierarchy: a fresh answer, else the last good one while it is young enough, else a declared degraded
value, else the error. Never silent: every step down is logged."""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict

from .. import runtime as rt
from . import ADOPTED, Layer, Pattern


class StaleCache:
    """The last good answer per key, for at most ttl_s; at most max_entries keys (the least recently stored go).
    Values are copied in and out; what can't be copied isn't kept."""

    def __init__(self, ttl_s: float = 60.0, max_entries: int = 1000):
        self.ttl_s, self.max_entries = ttl_s, max_entries
        self._lock = threading.Lock()
        self._items: OrderedDict = OrderedDict()
        rt.track(self)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()

    def put(self, key, value) -> None:
        try:
            value = copy.deepcopy(value)
        except Exception:
            return
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = (rt._now(), value)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def get(self, key):
        """(value, age in seconds), or None."""
        with self._lock:
            hit = self._items.get(key)
            if hit is None:
                return None
            age = rt._now() - hit[0]
            if age > self.ttl_s:
                del self._items[key]
                return None
        return copy.deepcopy(hit[1]), age


class FallbackLayer(Layer):
    """`degraded` is a value, or a function of the error; leave it None to end in the error. Only errors `when`
    accepts (default: transient ones and this runtime's own rejections) are stepped down for."""

    def __init__(self, dependency, params):
        super().__init__(dependency, params)
        self.cache = StaleCache(params["stale_ttl_s"], params["max_entries"])

    def run(self, call, inner):
        keyed = call.cache_key is not None and self.params["stale_ttl_s"] > 0
        try:
            result = yield from inner(call)
        except Exception as e:
            when = self.params["when"] or rt.is_transient
            if not (isinstance(e, rt.RejectedError) or when(e)):
                raise
            hit = self.cache.get(call.cache_key) if keyed else None
            if hit is not None:
                rt.log_event("fallback", level="stale", dependency=self.dependency, age_s=round(hit[1], 3), error=e)
                return hit[0]
            degraded = self.params["degraded"]
            if degraded is None:
                raise
            rt.log_event("fallback", level="degraded", dependency=self.dependency, error=e)
            return degraded(e) if callable(degraded) else degraded
        if keyed:
            self.cache.put(call.cache_key, result)
        return result


PATTERN = Pattern(
    name="fallback", title="Fallback hierarchy (stale cache, degraded value)", order=10, verdict=ADOPTED,
    hint="a read given a cache key is answered from the last good answer while that is young enough; set `degraded` for a declared value. Only where callers can live with that answer.",
    params={"stale_ttl_s": 60.0, "max_entries": 1000, "degraded": None, "when": None}, layer=FallbackLayer,
    exports=("StaleCache",),
    refs=("AWS REL05-BP01: graceful degradation", "Google SRE ch.22: degraded responses", "RFC 5861: stale-if-error",
          "Failsafe / Polly v8: Fallback"))
