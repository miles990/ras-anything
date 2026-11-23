// AI-dependency resilience helpers (V-14 / V-15). Node built-ins only; parity with patterns/ai.py.
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const core = require("./core.cjs");

const DEFAULT_PRICES = {
  small: [0.15, 0.60],
  backup: [0.30, 1.20],
  "gpt-mini": [0.15, 0.60],
  "gpt-fallback": [0.30, 1.20],
  "gpt-long": [0.50, 1.50],
  "gpt-4o-mini": [0.15, 0.60],
  "gpt-4o": [2.50, 10.00],
  "claude-haiku": [0.80, 4.00],
  "claude-sonnet": [3.00, 15.00],
  default: [0.25, 1.00],
};

class SchemaValidationError extends Error {
  constructor(msg) { super(msg); this.name = "SchemaValidationError"; }
}
class ToolNotAllowedError extends Error {
  constructor(msg) { super(msg); this.name = "ToolNotAllowedError"; }
}
class LoopGuardError extends Error {
  constructor(msg) { super(msg); this.name = "LoopGuardError"; }
}
class BudgetExceededError extends Error {
  constructor(msg) { super(msg); this.name = "BudgetExceededError"; }
}

function parseJsonResponse(raw) {
  if (raw !== null && typeof raw === "object") return raw;
  if (typeof raw !== "string") throw new SchemaValidationError(`expected JSON string or object, got ${typeof raw}`);
  const s = raw.trim();
  if (!s) throw new SchemaValidationError("empty completion content");
  try {
    return JSON.parse(s);
  } catch (e) {
    throw new SchemaValidationError(`malformed JSON completion: ${e.message}`);
  }
}

function checkSchema(val, schema, p) {
  if (!schema) return;
  const t = schema.type;
  if (t === "object") {
    if (val === null || typeof val !== "object" || Array.isArray(val)) throw new SchemaValidationError(`${p}: expected object`);
    for (const k of schema.required || []) {
      if (!(k in val)) throw new SchemaValidationError(`${p}: missing required field ${JSON.stringify(k)}`);
    }
    const props = schema.properties || {};
    if (schema.additionalProperties === false) {
      const extra = Object.keys(val).filter((k) => !(k in props));
      if (extra.length) throw new SchemaValidationError(`${p}: unexpected fields ${JSON.stringify(extra.sort())}`);
    }
    for (const [k, sub] of Object.entries(props)) {
      if (k in val) checkSchema(val[k], sub, `${p}.${k}`);
    }
  } else if (t === "array") {
    if (!Array.isArray(val)) throw new SchemaValidationError(`${p}: expected array`);
    if (schema.minItems != null && val.length < schema.minItems) throw new SchemaValidationError(`${p}: too few items`);
    if (schema.maxItems != null && val.length > schema.maxItems) throw new SchemaValidationError(`${p}: too many items`);
    if (schema.items && typeof schema.items === "object") {
      val.forEach((item, i) => checkSchema(item, schema.items, `${p}[${i}]`));
    }
  } else if (t === "string") {
    if (typeof val !== "string") throw new SchemaValidationError(`${p}: expected string`);
    if (schema.nonEmpty && !val.trim()) throw new SchemaValidationError(`${p}: empty string`);
    if (schema.minLength != null) {
      const checkStr = schema.nonEmpty !== false ? val.trim() : val;
      if (checkStr.length < schema.minLength) throw new SchemaValidationError(`${p}: string shorter than minLength`);
    }
    if (schema.maxLength != null && val.length > schema.maxLength) throw new SchemaValidationError(`${p}: string longer than maxLength`);
  } else if (t === "integer") {
    if (typeof val !== "number" || !Number.isInteger(val)) throw new SchemaValidationError(`${p}: expected integer`);
  } else if (t === "number") {
    if (typeof val !== "number" || Number.isNaN(val)) throw new SchemaValidationError(`${p}: expected number`);
  } else if (t === "boolean") {
    if (typeof val !== "boolean") throw new SchemaValidationError(`${p}: expected boolean`);
  }
  if (Array.isArray(schema.enum) && !schema.enum.includes(val)) {
    throw new SchemaValidationError(`${p}: value ${JSON.stringify(val)} not in enum`);
  }
}

function validateSchema(obj, schema) {
  const val = typeof obj === "string" && schema?.type !== "string" ? parseJsonResponse(obj) : obj;
  checkSchema(val, schema, "$");
  return val;
}

function frameUntrusted(text, label = "UNTRUSTED_DOCUMENT") {
  const safeLabel = String(label).replace(/[^A-Za-z0-9_-]/g, "") || "UNTRUSTED_DATA";
  let s = String(text ?? "");
  s = s.split(`</${safeLabel}>`).join(`&lt;/${safeLabel}&gt;`);
  s = s.split("<untrusted_data").join("&lt;untrusted_data").split("</untrusted_data>").join("&lt;/untrusted_data&gt;");
  return `<${safeLabel}>\n${s}\n</${safeLabel}>`;
}

function checkInjectionLeaks(text, canary) {
  return Boolean(canary && text && String(text).includes(canary));
}

class ToolAllowlist {
  constructor(allowed, forbidden = ["refund_all", "issue_refund"]) {
    if (Array.isArray(allowed) || allowed instanceof Set) {
      this.schemas = Object.fromEntries([...allowed].map((k) => [String(k), {}]));
    } else {
      this.schemas = { ...(allowed || {}) };
    }
    this.forbidden = new Set(forbidden);
  }

  check(name, args) {
    if (this.forbidden.has(name) || !(name in this.schemas)) {
      core.logEvent("ras.ai.tool_blocked", { "ras.tool": String(name), "ras.reason": "not_allowed" });
      throw new ToolNotAllowedError(`tool ${JSON.stringify(name)} is not allowed`);
    }
    const parsed = typeof args === "string" ? parseJsonResponse(args) : args;
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      core.logEvent("ras.ai.tool_blocked", { "ras.tool": String(name), "ras.reason": "invalid_args" });
      throw new ToolNotAllowedError(`tool ${JSON.stringify(name)} arguments must be an object`);
    }
    const schema = this.schemas[name];
    if (schema && Object.keys(schema).length) {
      try {
        validateSchema(parsed, schema);
      } catch (e) {
        core.logEvent("ras.ai.tool_blocked", { "ras.tool": String(name), "ras.reason": e.message });
        throw new ToolNotAllowedError(`tool ${JSON.stringify(name)} args rejected: ${e.message}`);
      }
    }
    return parsed;
  }
}

class LoopGuard {
  constructor({ maxSteps = 6, maxRepeat = 2 } = {}) {
    this.maxSteps = maxSteps;
    this.maxRepeat = maxRepeat;
    this.reset();
  }

  reset() {
    this.steps = 0;
    this._lastSig = null;
    this._repeatCount = 0;
  }

  record(toolName, args = {}) {
    this.steps += 1;
    if (this.steps > this.maxSteps) {
      core.logEvent("ras.ai.loop_guard", { "ras.tool": toolName, "ras.reason": "max_steps", steps: this.steps, max_steps: this.maxSteps });
      throw new LoopGuardError(`tool loop exceeded max_steps=${this.maxSteps}`);
    }
    const sorted = args && typeof args === "object" && !Array.isArray(args)
      ? Object.fromEntries(Object.keys(args).sort().map((k) => [k, args[k]]))
      : (args ?? {});
    const sig = `${toolName}:${JSON.stringify(sorted)}`;
    if (sig === this._lastSig) this._repeatCount += 1;
    else { this._lastSig = sig; this._repeatCount = 1; }
    if (this._repeatCount > this.maxRepeat) {
      core.logEvent("ras.ai.loop_guard", { "ras.tool": toolName, "ras.reason": "repeated_call", repeats: this._repeatCount });
      throw new LoopGuardError(`repeated identical tool call ${JSON.stringify(toolName)} ${this._repeatCount} times`);
    }
  }
}

class TokenBudget {
  constructor({ maxTokens = null, maxCostUsd = null, maxRequests = null, prices = null } = {}) {
    this.maxTokens = maxTokens;
    this.maxCostUsd = maxCostUsd;
    this.maxRequests = maxRequests;
    this.prices = { ...DEFAULT_PRICES, ...(prices || {}) };
    this.reset();
  }

  reset() {
    this.requests = 0;
    this.inputTokens = 0;
    this.outputTokens = 0;
    this.costUsd = 0;
  }

  get totalTokens() { return this.inputTokens + this.outputTokens; }

  checkBefore(estTokens = 0) {
    if (this.maxRequests != null && this.requests >= this.maxRequests) {
      throw new BudgetExceededError(`request budget exhausted (${this.requests}/${this.maxRequests})`);
    }
    if (this.maxTokens != null && this.totalTokens + Math.max(0, estTokens) > this.maxTokens) {
      throw new BudgetExceededError(`token budget exhausted (${this.totalTokens}/${this.maxTokens})`);
    }
    if (this.maxCostUsd != null && this.costUsd >= this.maxCostUsd) {
      throw new BudgetExceededError(`cost budget exhausted ($${this.costUsd.toFixed(6)}/$${this.maxCostUsd.toFixed(6)})`);
    }
  }

  charge(model, inputTokens, outputTokens) {
    this.requests += 1;
    this.inputTokens += Math.max(0, inputTokens | 0);
    this.outputTokens += Math.max(0, outputTokens | 0);
    const [pin, pout] = this.prices[model] || this.prices.default;
    const delta = (Math.max(0, inputTokens | 0) * pin + Math.max(0, outputTokens | 0) * pout) / 1_000_000;
    this.costUsd += delta;
    if (this.maxTokens != null && this.totalTokens > this.maxTokens) {
      throw new BudgetExceededError(`token budget exceeded (${this.totalTokens} > ${this.maxTokens})`);
    }
    if (this.maxCostUsd != null && this.costUsd > this.maxCostUsd) {
      throw new BudgetExceededError(`cost budget exceeded ($${this.costUsd.toFixed(6)} > $${this.maxCostUsd.toFixed(6)})`);
    }
    return delta;
  }
}

class ExactCache {
  constructor({ ttlMs = 300_000, maxEntries = 256 } = {}) {
    this.ttlMs = ttlMs;
    this.maxEntries = maxEntries;
    this._data = new Map();
    core.track(this);
  }

  reset() { this._data.clear(); }

  static keyFor(model, payload) {
    return crypto.createHash("sha256").update(JSON.stringify({ model, payload })).digest("hex");
  }

  get(key) {
    const hit = this._data.get(key);
    if (!hit) return undefined;
    if (Date.now() > hit.exp) { this._data.delete(key); return undefined; }
    return hit.val;
  }

  put(key, val) {
    if (this._data.size >= this.maxEntries) {
      const oldest = this._data.keys().next().value;
      if (oldest !== undefined) this._data.delete(oldest);
    }
    this._data.set(key, { exp: Date.now() + this.ttlMs, val });
  }
}

class StepJournal {
  constructor(directory) {
    this.dir = directory;
    fs.mkdirSync(this.dir, { recursive: true });
  }

  _path(runId) {
    const safe = String(runId).replace(/[^A-Za-z0-9_.-]/g, "_");
    return path.join(this.dir, `${safe}.json`);
  }

  _load(runId) {
    const p = this._path(runId);
    if (!fs.existsSync(p)) return {};
    try {
      const data = JSON.parse(fs.readFileSync(p, "utf8"));
      return data && typeof data === "object" && !Array.isArray(data) ? data : {};
    } catch { return {}; }
  }

  _save(runId, data) {
    const p = this._path(runId);
    const tmp = `${p}.tmp-${process.pid}-${Math.random().toString(16).slice(2)}`;
    fs.writeFileSync(tmp, JSON.stringify(data), "utf8");
    fs.renameSync(tmp, p);
  }

  async step(runId, name, fn) {
    const state = this._load(runId);
    if (Object.prototype.hasOwnProperty.call(state, name)) return state[name];
    const val = await fn();
    state[name] = val;
    this._save(runId, state);
    return val;
  }

  clear(runId) {
    try { fs.unlinkSync(this._path(runId)); } catch {}
  }
}

function classifyAiHttp(status, bodyText = "") {
  const low = String(bodyText || "").toLowerCase();
  if (status === 429) {
    if (low.includes("insufficient_quota") || low.includes("quota")) return ["ai_quota", false];
    return ["ai_rate_limit", true];
  }
  if (status === 400 && (low.includes("context_length") || low.includes("maximum context") || low.includes("too many tokens"))) {
    return ["ai_context_limit", false];
  }
  if (status === 404 && (low.includes("model_not_found") || low.includes("does not exist"))) {
    return ["ai_model_deprecated", false];
  }
  if (status === 529 || [500, 502, 503, 504, 408, 425].includes(status)) {
    return [status === 529 ? "ai_overloaded" : "http5xx", true];
  }
  return [`http_${status}`, false];
}

class AIPolicy {
  constructor({
    dependency = "llm",
    models = ["small", "backup"],
    contextWindowFallbacks = [],
    contentPolicyFallbacks = [],
    maxAttemptsPerModel = 2,
    perAttemptTimeoutMs = 1500,
    deadlineMs = 5000,
    maxTokens = 1024,
    maxCostUsd = null,
    maxRequests = null,
    schema = null,
    cache = null,
    provider = "openai",
  } = {}) {
    Object.assign(this, {
      dependency, models: [...models],
      contextWindowFallbacks: [...contextWindowFallbacks],
      contentPolicyFallbacks: [...contentPolicyFallbacks],
      maxAttemptsPerModel: Math.max(1, maxAttemptsPerModel),
      perAttemptTimeoutMs, deadlineMs, maxTokens, maxCostUsd, maxRequests,
      schema, cache, provider,
    });
  }

  async run(invokeModel, { cachePayload = null, schema = null } = {}) {
    const activeSchema = schema || this.schema;
    const budget = new TokenBudget({ maxTokens: this.maxTokens, maxCostUsd: this.maxCostUsd, maxRequests: this.maxRequests });
    const deadline = Date.now() + this.deadlineMs;
    let queue = [...this.models];
    const tried = new Set();
    let lastErr = null;

    while (queue.length) {
      const model = queue.shift();
      if (tried.has(model)) continue;
      tried.add(model);

      const ckey = this.cache && cachePayload !== null ? ExactCache.keyFor(model, cachePayload) : null;
      if (ckey) {
        const hit = this.cache.get(ckey);
        if (hit !== undefined) return hit;
      }

      for (let attempt = 0; attempt < this.maxAttemptsPerModel; attempt += 1) {
        const rem = deadline - Date.now();
        if (rem <= 0) { lastErr = new core.DeadlineExceeded(`ai deadline exceeded after ${this.deadlineMs}ms`); break; }
        budget.checkBefore();
        const tmo = Math.min(this.perAttemptTimeoutMs, rem);
        try {
          const raw = await invokeModel(model, tmo);
          const { val, meta } = AIPolicy._extractAndValidate(raw, model, activeSchema);
          budget.charge(meta.model, meta.inputTokens, meta.outputTokens);
          core.logEvent("gen_ai.client.operation", {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": this.provider,
            "gen_ai.request.model": model,
            "gen_ai.response.model": meta.model,
            "gen_ai.usage.input_tokens": meta.inputTokens,
            "gen_ai.usage.output_tokens": meta.outputTokens,
            "gen_ai.response.finish_reasons": meta.finishReason ? [meta.finishReason] : [],
            "ras.dependency": this.dependency,
          });
          if (ckey) this.cache.put(ckey, val);
          return val;
        } catch (e) {
          if (e instanceof BudgetExceededError) throw e;
          lastErr = e;
          const status = e?.status;
          if (typeof status === "number" && status >= 400) {
            const [fclass, retrySame] = classifyAiHttp(status, e.body || e.message);
            if (fclass === "ai_context_limit" && this.contextWindowFallbacks.length) {
              queue = [...this.contextWindowFallbacks.filter((m) => !tried.has(m)), ...queue];
              break;
            }
            if (!retrySame) break;
            const ra = e.headers?.["retry-after"];
            const delayMs = ra && /^\d+(\.\d+)?$/.test(String(ra)) ? Number(ra) * 1000 : Math.min(500, 50 * (2 ** attempt));
            if (Date.now() + delayMs < deadline) await core.sleep(delayMs);
            continue;
          }
          if (e instanceof SchemaValidationError) {
            const msg = e.message.toLowerCase();
            if ((msg.includes("content_filter") || msg.includes("refusal")) && this.contentPolicyFallbacks.length) {
              queue = [...this.contentPolicyFallbacks.filter((m) => !tried.has(m)), ...queue];
              break;
            }
            if (attempt + 1 >= this.maxAttemptsPerModel) break;
            continue;
          }
          if (!core.isTransient(e)) break;
          if (Date.now() + 50 < deadline) await core.sleep(Math.min(250, 50 * (2 ** attempt)));
        }
      }
    }

    core.logEvent("ras.dependency.gave_up", {
      "ras.dependency": this.dependency,
      "ras.fault": "ai_exhausted",
      "gen_ai.provider.name": this.provider,
    });
    const err = new Error(`${this.dependency} failed across fallback chain`);
    err.cause = lastErr;
    throw err;
  }

  static _extractAndValidate(raw, model, schema) {
    if (raw && typeof raw === "object" && Array.isArray(raw.choices)) {
      if (!raw.choices.length) throw new SchemaValidationError("empty choices");
      const c0 = raw.choices[0] || {};
      const finish = c0.finish_reason;
      const msg = c0.message || {};
      if (finish === "content_filter" || msg.refusal) throw new SchemaValidationError("content_filter refusal");
      if (finish === "length") throw new SchemaValidationError("finish_reason=length truncated output");
      const usage = raw.usage || {};
      const meta = {
        model: String(raw.model || model),
        inputTokens: Number(usage.prompt_tokens ?? usage.input_tokens ?? 0),
        outputTokens: Number(usage.completion_tokens ?? usage.output_tokens ?? 0),
        finishReason: finish || "stop",
      };
      const tcs = Array.isArray(msg.tool_calls) ? msg.tool_calls : [];
      if (tcs.length) return { val: { tool_calls: tcs, message: msg }, meta };
      const val = schema ? validateSchema(msg.content, schema) : msg.content;
      return { val, meta };
    }
    const meta = { model, inputTokens: 0, outputTokens: 0, finishReason: "stop" };
    const val = schema ? validateSchema(raw, schema) : raw;
    return { val, meta };
  }
}

module.exports = {
  DEFAULT_PRICES,
  SchemaValidationError,
  ToolNotAllowedError,
  LoopGuardError,
  BudgetExceededError,
  validateSchema,
  frameUntrusted,
  checkInjectionLeaks,
  ToolAllowlist,
  LoopGuard,
  TokenBudget,
  ExactCache,
  StepJournal,
  AIPolicy,
};
