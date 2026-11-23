// Errors an agent can act on: RFC 9457 problem details for any failure, and the human gate for irreversible operations.
// The members and the table are the same as in Python (patterns/problem.py); every language runtime must produce this shape.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const core = require("./core.cjs");

// cause -> title, status, retryable, next_action
const CAUSES = {
  timeout: ["The dependency did not answer in time", 504, true, "retry_later"],
  unavailable: ["The dependency is unavailable", 503, true, "retry_later"],
  rate_limited: ["The dependency asks for fewer requests", 429, true, "retry_later"],
  quota: ["A quota or a budget is used up", 429, false, "escalate"],
  invalid_request: ["The request was refused as invalid", 400, false, "change_params"],
  auth: ["Not authorized at the dependency", 403, false, "escalate"],
  conflict: ["The request conflicts with the current state", 409, false, "stop"],
  dependency_bug: ["The dependency answered in a way that can't be used", 502, false, "escalate"],
  approval_required: ["A person has to authorize this operation", 403, false, "escalate"],
  unknown: ["The operation failed", 500, false, "stop"],
};
const NEXT_ACTIONS = ["retry_later", "change_params", "use_fallback", "escalate", "stop"];
const FAULT_CAUSE = { timeout: "timeout", refused: "unavailable", unavailable: "unavailable", truncated: "unavailable", circuit_open: "unavailable",
  rejected: "unavailable", rate_limited: "rate_limited", quota: "quota", invalid_request: "invalid_request", auth: "auth", conflict: "conflict",
  unsupported: "dependency_bug", approval: "approval_required" };

/** What an agent needs to know about a failure, and nothing about our insides: RFC 9457 members (type, title, status,
 *  detail, instance) plus cause, dependency (a logical name), retryable, retry_after_s, next_action,
 *  needs_human_authorization, correlation_id. */
function problem(err) {
  const advice = err?.advice ?? {};
  let cause = advice.category ?? FAULT_CAUSE[core.faultClass(err)] ?? "unknown";
  if (!(cause in CAUSES)) cause = "unknown";
  const [title, usual, worth, usualAction] = CAUSES[cause];
  let root = err;
  let real = core.httpStatus(err);
  let wait = advice.retryAfterS ?? null;
  for (let i = 0; i < 6 && root?.cause !== null && typeof root?.cause === "object"; i++) {
    root = root.cause;
    real ??= core.httpStatus(root);
    wait ??= root.advice?.retryAfterS ?? retryAfter(root);
  }
  wait ??= retryAfter(err);
  const status = advice.httpStatus ?? (["invalid_request", "auth", "conflict"].includes(cause) && real ? real : usual);
  const ctx = core.context();
  const dep = advice.dependency ?? root?.advice?.dependency ?? (ctx ? ctx.lastDependency ?? (ctx.deps.length === 1 ? ctx.deps[0].name : null) : null);
  const tid = err?.traceId ?? core.traceId();
  const retryable = Boolean(advice.retryable ?? worth);
  const action = advice.nextAction ?? (retryable ? "retry_later" : usualAction === "retry_later" ? "stop" : usualAction);
  return {
    type: `urn:ras-anything:problem:${cause}`, title, status, detail: core.plain(err instanceof core.RasError ? err.message : title),
    instance: `urn:trace:${tid}`, cause, dependency: dep ? core.plain(dep, 60) : null, retryable,
    retry_after_s: wait === null || wait === undefined ? null : Math.round(Number(wait) * 1000) / 1000,
    next_action: NEXT_ACTIONS.includes(action) ? action : "stop",
    needs_human_authorization: Boolean(advice.needsHumanAuthorization ?? cause === "approval_required"), correlation_id: tid,
  };
}

function retryAfter(err) {
  try {
    return require("./retry_after.cjs").retryAfterS(err);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- human gate

/** An irreversible operation nobody with the authority has approved. */
class ApprovalRequired extends core.RasError {
  constructor(op, details = {}, message = null) {
    super(message ?? `a person has to approve ${op} first`, { context: { op, ...details }, category: "approval_required", retryable: false,
      nextAction: "escalate", needsHumanAuthorization: true });
    this.name = "ApprovalRequired";
    Object.assign(this, { op, needsHumanAuthorization: true, nextAction: "escalate" });
  }
}

let approver = null;

/** `fn(op, details)` returns (or resolves to) true or not, from now on; null: back to the environment and the file.
 *  Returns the one before. */
function setApprover(fn) {
  const prev = approver;
  approver = fn;
  return prev;
}

/** Await it before an operation that can't be undone (a charge, a delete, a message to a person). True if it may go on,
 *  else ApprovalRequired: needsHumanAuthorization, nextAction "escalate".
 *
 *  Who decides: the approver (setApprover); else $RAS_ANYTHING_APPROVE, a comma-separated list of operations or "*"; else
 *  the JSON file $RAS_ANYTHING_APPROVALS (default .ras/approvals.json): {"charge": true} or {"charge": {"max":
 *  {"amount_cents": 5000}}}. Nobody answering, an approver that fails, or a file that can't be read is a no. */
async function requireApproval(op, details = {}) {
  let ok = false;
  let by = "nobody";
  try {
    if (approver !== null) [ok, by] = [(await approver(op, { ...details })) === true, "approver"];
    else {
      const env = (process.env.RAS_ANYTHING_APPROVE ?? "").split(",").map((x) => x.trim()).filter(Boolean);
      [ok, by] = env.includes("*") || env.includes(op) ? [true, "environment"] : [fileSays(op, details), "file"];
    }
  } catch (e) {
    core.logEvent("ras.approval.error", { op, error: e });
    ok = false;
  }
  if (ok) {
    core.logEvent("ras.approval.granted", { op, by });
    return true;
  }
  core.logEvent("ras.approval.denied", { op, "ras.next_action": "escalate", "ras.needs_human_authorization": true });
  throw new ApprovalRequired(op, details);
}

function fileSays(op, details) {
  const file = process.env.RAS_ANYTHING_APPROVALS || path.join(".ras", "approvals.json");
  if (!fs.existsSync(file)) return false;
  const rule = JSON.parse(fs.readFileSync(file, "utf8"))[op];
  if (rule === true) return true;
  const limits = rule && typeof rule === "object" ? rule.max : null;
  if (!limits || typeof limits !== "object" || Object.keys(limits).length === 0) return false;
  return Object.entries(limits).every(([k, v]) => typeof details[k] === "number" && details[k] <= v);
}

module.exports = { CAUSES, NEXT_ACTIONS, problem, requireApproval, setApprover, ApprovalRequired };
