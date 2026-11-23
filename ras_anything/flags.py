"""Feature flags: an OpenFeature/flagd-compatible file, hot-reloaded, fail-safe.

The file is `.ras/flags.json` (or `$RAS_ANYTHING_FLAGS`). A boolean or number variant is enough
for this tool. `RAS_ANYTHING_DISABLE=1` is the master switch and is not stored here.

    { "flags": {
        "ras.pattern.retry": {"state": "ENABLED", "variants": {"on": true, "off": false}, "defaultVariant": "on"},
        "ras.enable.weather.fetch_forecast": {"state": "ENABLED", "variants": {"off": 0, "canary": 5, "on": 100}, "defaultVariant": "canary"}
    }}

Evaluation never raises to the caller: a broken file keeps the last good one, and a broken
evaluator returns the default. `is_on` (and Node's `hooks.isOn`) consult this on every call.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from .paths import UserError, find_project, save_json

SCHEMA = 1
PATTERN_PREFIX = "ras.pattern."
ENABLE_PREFIX = "ras.enable."
SHADOW_PREFIX = "ras.shadow."
ISOLATE_PREFIX = "ras.isolate."

_lock = threading.RLock()
_store: "Store | None" = None


def flags_path(project: Path | None = None) -> Path:
    env = os.environ.get("RAS_ANYTHING_FLAGS")
    if env:
        return Path(env)
    # Do not create .ras/ just to evaluate flags (scan/tests would grow a folder as a side effect).
    return (project or find_project()) / ".ras" / "flags.json"


def empty_doc() -> dict:
    return {"$schemaVersion": SCHEMA, "flags": {}}


def _flag_body(value, state="ENABLED") -> dict:
    """A flagd flag object for a bool, a number (canary %), or a string."""
    if isinstance(value, bool):
        variants = {"on": True, "off": False}
        default = "on" if value else "off"
    elif isinstance(value, (int, float)):
        variants = {"off": 0, "on": 100, "value": value}
        default = "value"
    else:
        variants = {"on": str(value)}
        default = "on"
    return {"state": state, "variants": variants, "defaultVariant": default}


class Store:
    """One flags file: load, atomic save, mtime poll, last-known-good."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.doc = empty_doc()
        self.version = 0
        self.mtime_ns = 0
        self.size = -1
        self.last_good = empty_doc()
        self.last_error: str | None = None
        self.source = "default"
        self._loaded = False

    def maybe_reload(self) -> bool:
        """Re-read the file when it has changed. Returns True if the in-memory doc changed."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            if not self._loaded:
                self._loaded = True
                self.source = "missing"
            elif self.source != "missing":
                self.doc = empty_doc()
                self.last_good = empty_doc()
                self.source = "missing"
                self.version += 1
                self.mtime_ns = 0
                return True
            return False
        if self._loaded and st.st_mtime_ns == self.mtime_ns and getattr(self, "size", -1) == st.st_size:
            return False
        try:
            raw = self.path.read_text()
            doc = json.loads(raw)
            if not isinstance(doc, dict) or not isinstance(doc.get("flags", {}), dict):
                raise ValueError("flags.json must be an object with a 'flags' object")
            doc.setdefault("flags", {})
            self.doc = doc
            self.last_good = doc
            self.last_error = None
            self.source = "file"
        except (OSError, ValueError, json.JSONDecodeError) as e:
            self.last_error = str(e)
            self.doc = self.last_good
            self.source = "last-known-good"
            # still record mtime so we don't spin; the next *change* will retry
        self.mtime_ns = st.st_mtime_ns
        self.size = st.st_size
        self._loaded = True
        self.version += 1
        return True

    def save(self, doc: dict | None = None) -> None:
        if doc is not None:
            self.doc = doc
        self.doc.setdefault("$schemaVersion", SCHEMA)
        self.doc.setdefault("flags", {})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.path, self.doc)
        self.last_good = self.doc
        self.last_error = None
        self.source = "file"
        try:
            st = self.path.stat()
            self.mtime_ns = st.st_mtime_ns
            self.size = st.st_size
        except OSError:
            pass
        self.version += 1
        self._loaded = True
        from . import runtime
        runtime.reset_flags()
        runtime.flag_store(self.path)

    def flag(self, key: str) -> dict | None:
        self.maybe_reload()
        f = self.doc.get("flags", {}).get(key)
        return f if isinstance(f, dict) else None

    def evaluate(self, key: str, default):
        """The variant value of `key`, or `default`. Never raises."""
        try:
            f = self.flag(key)
            if not f:
                return default
            if str(f.get("state", "ENABLED")).upper() == "DISABLED":
                if isinstance(default, bool):
                    return False
                if isinstance(default, (int, float)):
                    return 0
                return default
            variants = f.get("variants") or {}
            name = f.get("defaultVariant")
            if name in variants:
                return variants[name]
            if variants:
                return next(iter(variants.values()))
            return default
        except Exception:
            return default

    def set_flag(self, key: str, value, state="ENABLED") -> dict:
        self.maybe_reload()
        flags = dict(self.doc.get("flags") or {})
        flags[key] = _flag_body(value, state=state)
        self.save({**self.doc, "flags": flags})
        return flags[key]

    def delete(self, key: str) -> None:
        self.maybe_reload()
        flags = dict(self.doc.get("flags") or {})
        flags.pop(key, None)
        self.save({**self.doc, "flags": flags})

    def snapshot(self) -> dict:
        self.maybe_reload()
        return {
            "path": str(self.path),
            "source": self.source,
            "version": self.version,
            "error": self.last_error,
            "flags": {k: {"state": (v or {}).get("state"), "defaultVariant": (v or {}).get("defaultVariant"),
                          "value": self.evaluate(k, None)} for k, v in (self.doc.get("flags") or {}).items()},
        }


def store(project: Path | None = None) -> Store:
    global _store
    path = flags_path(project)
    with _lock:
        if _store is None or _store.path.resolve() != path.resolve():
            _store = Store(path)
        _store.maybe_reload()
        return _store


def reset() -> None:
    """Drop the process-wide store (tests)."""
    global _store
    with _lock:
        _store = None
    from . import runtime
    runtime.reset_flags()


def disabled() -> bool:
    return os.environ.get("RAS_ANYTHING_DISABLE") == "1"


def pattern_key(pattern: str, dependency: str | None = None) -> str:
    return PATTERN_PREFIX + pattern + (("@" + dependency) if dependency else "")


def enable_key(fid: str) -> str:
    return ENABLE_PREFIX + fid


def shadow_key(fid: str) -> str:
    return SHADOW_PREFIX + fid


def isolate_key(name: str) -> str:
    return ISOLATE_PREFIX + name


def is_pattern_on(dependency: str, pattern: str, configured: bool) -> bool:
    """Hook for Policy: a more specific flag (per dependency) wins; else the pattern flag; else `configured`."""
    store()  # bind RAS_ANYTHING_FLAGS so a CLI set is visible
    from . import runtime
    runtime.flag_store(flags_path())
    return runtime.is_pattern_on(dependency, pattern, configured)


def enable_percent(fid: str) -> int:
    """0 = original only, 100 = hardened always, in between = canary. Fail-safe: 100 if unset (enable already swapped)."""
    from . import runtime
    runtime.flag_store(flags_path())
    return runtime.enable_percent(fid)


def shadow_on(fid: str) -> bool:
    from . import runtime
    runtime.flag_store(flags_path())
    return runtime.shadow_on(fid)


def isolation_scope(name: str, default: str = "dependency") -> str:
    """'dependency' | 'key' | 'function'."""
    v = store().evaluate(isolate_key(name), default)
    return v if v in ("dependency", "key", "function") else default


def sticky_bucket(key: str, buckets: int = 100) -> int:
    """Stable 0..buckets-1 from a targeting key (same in Python and Node: SHA-256, big-endian)."""
    h = hashlib.sha256(str(key).encode()).digest()
    return int.from_bytes(h[:8], "big") % buckets


def in_canary(fid: str, targeting_key: str | None) -> bool:
    pct = enable_percent(fid)
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    return sticky_bucket(targeting_key or fid) < pct


def targeting_key(*args, **kwargs) -> str | None:
    for k in ("idempotency_key", "targeting_key", "key", "order_id", "id"):
        if kwargs.get(k) not in (None, ""):
            return str(kwargs[k])
    if args:
        a = args[0]
        if isinstance(a, (str, int)):
            return str(a)
        if isinstance(a, dict):
            for k in ("id", "key", "order_id"):
                if a.get(k) not in (None, ""):
                    return str(a[k])
    return None


# --------------------------------------------------------------------------- CLI


def _print_list(snap: dict) -> None:
    from . import ui

    ui.headline("flag", snap["path"] if snap["flags"] else "no flags file yet")
    print("  source=%s  version=%s%s" % (snap["source"], snap["version"],
                                         ("  (%s)" % snap["error"]) if snap["error"] else ""))
    if not snap["flags"]:
        print("  (none)  set one with: ras-anything flag set ras.pattern.retry off")
        return
    rows = [[k, str(v["state"] or ""), "" if v["value"] is None else json.dumps(v["value"]), v["defaultVariant"] or ""]
            for k, v in snap["flags"].items()]
    print(ui.table(["flag", "state", "value", "variant"], rows, "llll"))


def run(argv: list[str]) -> int:
    """`ras-anything flag list|get|set|delete`."""

    if not argv or argv[0] in ("list", "ls"):
        _print_list(store().snapshot())
        return 0
    cmd = argv[0]
    if cmd == "get":
        if len(argv) < 2:
            raise UserError("flag get needs a name", "", "flag list")
        s = store()
        key = argv[1]
        print(json.dumps({"key": key, "value": s.evaluate(key, None), "flag": s.flag(key),
                          "source": s.source, "version": s.version}, indent=2, default=str))
        return 0
    if cmd == "delete":
        if len(argv) < 2:
            raise UserError("flag delete needs a name", "", "flag list")
        store().delete(argv[1])
        _print_list(store().snapshot())
        return 0
    if cmd == "set":
        return _set(argv[1:])
    raise UserError("unknown flag command %s" % cmd, "use list, get, set or delete", "flag list")


def _set(argv: list[str]) -> int:
    if not argv:
        raise UserError("flag set needs a name", "examples: flag set ras.pattern.retry off · flag set weather.fetch_forecast --canary 5",
                        "flag list")
    key, rest = argv[0], argv[1:]
    state = "ENABLED"
    value: object = True
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("on", "true", "True"):
            value = True
        elif a in ("off", "false", "False"):
            value = False
        elif a == "--disabled":
            state = "DISABLED"
            value = False
        elif a in ("--canary", "--percent") and i + 1 < len(rest):
            i += 1
            value = int(rest[i])
            key = key if key.startswith(ENABLE_PREFIX) else enable_key(key)
        elif a == "--shadow":
            value = True
            key = key if key.startswith(SHADOW_PREFIX) else shadow_key(key)
        elif a == "--isolate" and i + 1 < len(rest):
            i += 1
            value = rest[i]
            key = key if key.startswith(ISOLATE_PREFIX) else isolate_key(key)
        elif a in ("--on",):
            value = True
        elif a in ("--off",):
            value = False
        else:
            try:
                value = json.loads(a)
            except json.JSONDecodeError:
                value = a
        i += 1
    store().set_flag(key, value, state=state)
    _print_list(store().snapshot())
    return 0


# bind the process-wide hook the first time this module is imported from is_on — done in patterns/__init__.py
