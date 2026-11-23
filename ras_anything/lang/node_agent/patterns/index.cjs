// The registry of resilience patterns: one module each, outermost first (docs/PATTERNS.md). A pattern module
// imports ./core.cjs only. With one switched off or missing, the others still work.
"use strict";

const NAMES = require("./names.json"); // generated from ras_anything.patterns.NAMES (patterns.write_node_names)
const camel = (s) => s.replace(/_(\w)/g, (_, c) => c.toUpperCase());
const snake = (s) => s.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);

/** A pattern's description, by its name in either spelling. */
function get(name) {
  const n = snake(name);
  if (!NAMES.includes(n)) throw new RangeError(`no pattern ${name} (known: ${NAMES.join(", ")})`);
  return require(`./${n}.cjs`).PATTERN;
}

/** Every pattern whose module is there, outermost first. */
const all = () => NAMES.flatMap((name) => {
  try {
    return [get(name)];
  } catch (e) {
    if (e.code !== "MODULE_NOT_FOUND") throw e;
    return [];
  }
}).sort((a, b) => a.order - b.order);

/** Is a pattern on for this call? Asked on every call of every policy. Flags (`.ras/flags.json`) hook in here. */
const hooks = { isOn: (dependency, pattern, configured) => {
  try { return require("./flags.cjs").isOn(dependency, pattern, configured); } catch { return configured; }
} };

module.exports = { NAMES, get, all, hooks, camel, snake };
