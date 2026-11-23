// Loader hooks: swap a module's source for an instrumented one (kept in memory, the files stay as they are).
"use strict";
const fs = require("node:fs");
const module_ = require("node:module");
const { createHash } = require("node:crypto");
const { fileURLToPath, pathToFileURL } = require("node:url");

/** files: {"/abs/file": {sha, source}}; resolve(specifier, parentPath, next) may redirect a specifier. */
function install(files, resolve) {
  const byUrl = new Map(Object.entries(files).map(([f, x]) => [pathToFileURL(f).href, { file: f, ...x }]));
  module_.registerHooks({
    resolve(specifier, context, next) {
      if (!resolve || !context.parentURL?.startsWith("file:")) return next(specifier, context);
      const r = resolve(specifier, fileURLToPath(context.parentURL));
      return r ? next(r.specifier, r.parent ? { ...context, parentURL: pathToFileURL(r.parent).href } : context) : next(specifier, context);
    },
    load(url, context, next) {
      const r = next(url, context);
      const x = byUrl.get(url);
      if (!x || r.format === "json" || r.format === "wasm") return r;
      const raw = fs.readFileSync(x.file);
      if (createHash("sha256").update(raw).digest("hex") !== x.sha) return r; // it changed since it was analyzed
      return { ...r, source: x.source };
    },
  });
}

module.exports = { install };
