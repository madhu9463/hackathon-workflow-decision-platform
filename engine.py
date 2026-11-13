"""Workflow engine: intake, stage loop, retries, manual review, explanation.

The engine contains NO business rules. Everything it decides comes from the
workflow config the request is pinned to.
"""
import hashlib
import json
import time
import uuid

from .conditions import MISSING, evaluate, lookup
from .errors import (BadRequest, DependencyError, IdempotencyConflict, InvalidState,
                     RequestNotFound, TransientDependencyError)
from .schema import validate_payload


def canonical_hash(obj):
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def audit_row(stage, kind, result, description=None, rule_id=None, detail=None):
    return {"stage": stage, "kind": kind, "rule_id": rule_id, "description": description,
            "result": result, "detail": detail or {}}


def _outcome(status, stage, reason, audit):
    return {"status": status, "stage": stage, "reason": reason, "audit": audit}


class Engine:
    def __init__(self, store, registry, dependencies, sleep=time.sleep, max_steps=50):
        self.store = store
        self.registry = registry
        self.deps = dependencies
        self.sleep = sleep
        self.max_steps = max_steps

    # ------------------------------------------------------------------ intake
    def submit(self, workflow_id, payload, idempotency_key=None):
        """Returns (request, created). created=False means an idempotent replay."""
        if not isinstance(workflow_id, str) or not workflow_id:
            raise BadRequest("'workflow' is required")
        version, _ = self.registry.resolve(workflow_id)      # WorkflowNotFound if unknown
        payload_hash = canonical_hash({"workflow": workflow_id, "payload": payload})
        key = idempotency_key or payload_hash
        existing = self.store.get_by_idem_key(key)
        if existing:
            self._check_same(existing, workflow_id, payload_hash)
            return existing, False
        wf = self.registry.get(workflow_id, version)
        request, created = self.store.create_request(
            uuid.uuid4().hex, key, workflow_id, version, payload_hash, wf["start"],
            {"input": payload, "deps": {}, "attempts": {}, "flags": []})
        if not created:                                      # lost the race to a twin
            self._check_same(request, workflow_id, payload_hash)
            return request, False
        return self._execute(request["id"]), True

    @staticmethod
    def _check_same(request, workflow_id, payload_hash):
        if request["workflow_id"] != workflow_id or request["payload_hash"] != payload_hash:
            raise IdempotencyConflict(
                "idempotency key already used with a different payload or workflow")

    # --------------------------------------------------------------- operations
    def resume(self, request_id):
        """Resume a PENDING_RETRY request, or one interrupted mid-run."""
        request = self._require(request_id)
        if request["status"] not in ("PENDING_RETRY", "IN_PROGRESS", "RECEIVED"):
            raise InvalidState("request is %s; nothing to resume" % request["status"])
        return self._execute(request_id)

    def resolve_review(self, request_id, decision, reviewer, note=""):
        request = self._require(request_id)
        if decision not in ("APPROVED", "REJECTED"):
            raise BadRequest("decision must be APPROVED or REJECTED")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise BadRequest("reviewer is required")
        if request["status"] != "MANUAL_REVIEW":
            raise InvalidState("request is %s, not MANUAL_REVIEW" % request["status"])
        reason = "manual review by %s%s" % (reviewer, ": " + note if note else "")
        row = audit_row(request["current_stage"], "manual_review", decision, reason,
                        detail={"reviewer": reviewer, "note": note})
        return self.store.commit_stage(request["id"], request["version"], decision,
                                       request["current_stage"], request["context"],
                                       reason, [row])

    def get(self, request_id):
        return self._require(request_id)

    def history(self, request_id):
        self._require(request_id)
        return self.store.list_history(request_id)

    def audit(self, request_id):
        self._require(request_id)
        return self.store.list_audit(request_id)

    def _require(self, request_id):
        request = self.store.get_request(request_id)
        if request is None:
            raise RequestNotFound("unknown request %r" % (request_id,))
        return request

    # ---------------------------------------------------------------- execution
    def _commit(self, request, out, context=None):
        return self.store.commit_stage(
            request["id"], request["version"], out["status"], out["stage"],
            request["context"] if context is None else context, out["reason"], out["audit"])

    def _execute(self, request_id):
        request = self._require(request_id)
        wf = self.registry.get(request["workflow_id"], request["workflow_version"])
        status = request["status"]
        if status == "RECEIVED":
            errors = validate_payload(wf.get("schema", {}), request["context"].get("input"))
            if errors:
                request["context"]["errors"] = errors
                return self._commit(request, _outcome(
                    "INVALID", request["current_stage"], "Schema validation failed",
                    [audit_row("intake", "schema", "FAIL", "schema validation",
                               detail={"errors": errors})]))
            request = self._commit(request, _outcome(
                "IN_PROGRESS", request["current_stage"], "run started",
                [audit_row("intake", "schema", "PASS", "schema validation")]))
        elif status == "PENDING_RETRY":
            request = self._commit(request, _outcome(
                "IN_PROGRESS", request["current_stage"], "retry started", []))
        elif status != "IN_PROGRESS":
            raise InvalidState("request is %s; cannot execute" % status)

        steps = 0
        while request["status"] == "IN_PROGRESS":
            steps += 1
            if steps > self.max_steps:                       # last-resort safety net
                request = self._commit(request, _outcome(
                    "MANUAL_REVIEW", request["current_stage"],
                    "step limit exceeded; escalated", []))
                break
            request = self._step(request, wf)
        return request

    def _step(self, request, wf):
        name = request["current_stage"]
        stage = wf["stages"][name]
        handler = {"rules": self._rules, "dependency": self._dependency,
                   "branch": self._branch, "terminal": self._terminal}[stage["type"]]
        out = handler(request, name, stage, request["context"])
        return self._commit(request, out)

    # ----------------------------------------------------------------- stages
    def _rules(self, request, name, stage, ctx):
        audit = []
        for rule in stage["rules"]:
            rid, desc = rule["id"], rule.get("description", "")
            if "when" in rule:
                gate = evaluate(rule["when"], ctx)
                # Not applicable only when we KNOW it is false; unknown => enforce the rule.
                if not gate.ok and not gate.indeterminate:
                    audit.append(audit_row(name, "rule", "SKIPPED", desc, rid,
                                           {"reason": "not applicable", "trace": gate.trace}))
                    continue
            res = evaluate(rule["check"], ctx)
            if res.ok:
                audit.append(audit_row(name, "rule", "PASS", desc, rid, {"trace": res.trace}))
                continue
            action = rule.get("on_fail", "reject")
            if action == "flag":
                audit.append(audit_row(name, "rule", "FLAGGED", desc, rid, {"trace": res.trace}))
                ctx.setdefault("flags", []).append({"rule_id": rid, "stage": name,
                                                    "description": desc})
                continue
            audit.append(audit_row(name, "rule", "FAIL", desc, rid, {"trace": res.trace}))
            if action == "reject":
                return _outcome("REJECTED", name, "Rule %s failed: %s" % (rid, desc), audit)
            return _outcome("MANUAL_REVIEW", name,
                            "Rule %s needs manual review: %s" % (rid, desc), audit)
        return _outcome("IN_PROGRESS", stage["next"], "stage '%s' passed" % name, audit)

    def _dependency(self, request, name, stage, ctx):
        retries = stage.get("retries", {})
        per_run = retries.get("max_attempts", 3)
        cap = retries.get("max_total_attempts", per_run * 2)
        base = retries.get("backoff_base_seconds", 0.0)
        factor = retries.get("backoff_factor", 2.0)
        service = stage["service"]
        params = {}
        for key, path in stage.get("params", {}).items():
            value = lookup(ctx, path)
            params[key] = None if value is MISSING else value
        idem = "%s:%s" % (request["id"], name)               # same key for every attempt
        attempts = ctx.setdefault("attempts", {})
        audit = []
        for i in range(per_run):
            attempts[name] = attempts.get(name, 0) + 1
            n = attempts[name]
            try:
                response = self.deps.call(service, params, idem)
            except TransientDependencyError as exc:
                audit.append(audit_row(name, "dependency", "ERROR", service, None,
                                       {"service": service, "attempt": n, "error": str(exc)}))
                if i < per_run - 1:
                    self.sleep(base * (factor ** i))         # exponential backoff
                continue
            except DependencyError as exc:
                audit.append(audit_row(name, "dependency", "ERROR", service, None,
                                       {"service": service, "attempt": n,
                                        "error": str(exc), "permanent": True}))
                return _outcome("MANUAL_REVIEW", name,
                                "dependency '%s' failed: %s" % (service, exc), audit)
            ctx.setdefault("deps", {})[stage.get("store_as", name)] = response
            audit.append(audit_row(name, "dependency", "OK", service, None,
                                   {"service": service, "attempt": n, "response": response}))
            return _outcome("IN_PROGRESS", stage["next"],
                            "dependency '%s' succeeded" % service, audit)
        if attempts[name] >= cap:                            # total attempt cap reached
            status = "REJECTED" if stage.get("on_exhausted") == "reject" else "MANUAL_REVIEW"
            return _outcome(status, name, "dependency '%s' unavailable after %d attempts"
                            % (service, attempts[name]), audit)
        return _outcome("PENDING_RETRY", name,
                        "dependency '%s' unavailable; awaiting retry" % service, audit)

    def _branch(self, request, name, stage, ctx):
        target, matched, trace, desc = stage["default"], "default", [], "default branch"
        for i, b in enumerate(stage.get("branches", [])):
            res = evaluate(b["when"], ctx)
            if res.ok and not res.indeterminate:
                target, matched, trace = b["next"], i, res.trace
                desc = b.get("description", "branch #%d" % i)
                break
        row = audit_row(name, "branch", target, desc, None, {"matched": matched, "trace": trace})
        return _outcome("IN_PROGRESS", target, "branch '%s' -> %s" % (name, target), [row])

    def _terminal(self, request, name, stage, ctx):
        decision = stage["decision"]
        reason = stage.get("reason", decision)
        return _outcome(decision, name, reason,
                        [audit_row(name, "decision", decision, reason)])

    # -------------------------------------------------------------- explanation
    @staticmethod
    def _fmt_entry(t):
        actual = "<missing>" if t["missing"] and t["actual"] is None else repr(t["actual"])
        return "%s=%s %s %r -> %s" % (t["field"], actual, t["op"], t["expected"], t["result"])

    def explain(self, request_id):
        request = self._require(request_id)
        history = self.store.list_history(request_id)
        audit = self.store.list_audit(request_id)
        path = []
        for h in history:
            if h["to_stage"] and (not path or path[-1] != h["to_stage"]):
                path.append(h["to_stage"])
        lines = ["%s@v%s status=%s reason=%s" % (request["workflow_id"],
                                                  request["workflow_version"],
                                                  request["status"], request["reason"]),
                 "path: " + " -> ".join(path)]
        for a in audit:
            st, kind, res, d = a["stage"], a["kind"], a["result"], a["detail"]
            if kind == "schema":
                if res == "PASS":
                    lines.append("[intake] schema validation PASS")
                else:
                    errs = "; ".join("%s: %s" % (e["field"], e["error"]) for e in d["errors"])
                    lines.append("[intake] schema validation FAIL: " + errs)
            elif kind == "rule":
                if res == "SKIPPED":
                    lines.append("[%s] %s skipped (not applicable)" % (st, a["rule_id"]))
                else:
                    trace = "; ".join(self._fmt_entry(t) for t in d.get("trace", []))
                    lines.append("[%s] %s (%s) %s: %s" % (st, a["rule_id"], a["description"],
                                                         res, trace))
            elif kind == "dependency":
                if res == "OK":
                    lines.append("[%s] call %s attempt %s: OK -> %s"
                                 % (st, d["service"], d["attempt"], d["response"]))
                else:
                    lines.append("[%s] call %s attempt %s: ERROR (%s)"
                                 % (st, d["service"], d["attempt"], d["error"]))
            elif kind == "branch":
                lines.append("[%s] branch -> %s" % (st, res))
            elif kind == "decision":
                lines.append("[%s] decision -> %s: %s" % (st, res, a["description"]))
            elif kind == "manual_review":
                note = " (%s)" % d["note"] if d.get("note") else ""
                lines.append("[%s] manual review by %s: %s%s" % (st, d["reviewer"], res, note))
            else:
                lines.append("[%s] %s %s" % (st, kind, res))
        return "\n".join(lines)
