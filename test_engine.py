import threading
import unittest

from helpers import loan, make_app, vendor
from wfdp.errors import (BadRequest, ConcurrencyError, IdempotencyConflict, IllegalTransition,
                         InvalidState, WorkflowNotFound)


def results(audit, kind="rule"):
    return {a["rule_id"]: a["result"] for a in audit if a["kind"] == kind}


class HappyPathTests(unittest.TestCase):
    def test_approval_with_full_history_and_audit(self):
        app = make_app(self)
        req, created = app.engine.submit("loan_approval", loan())
        self.assertTrue(created)
        self.assertEqual((req["status"], req["current_stage"], req["reason"], req["version"]),
                         ("APPROVED", "approve", "All checks passed", 7))
        history = app.engine.history(req["id"])
        self.assertEqual(len(history), 7)
        self.assertEqual((history[0]["from_status"], history[0]["to_status"]), (None, "RECEIVED"))
        self.assertEqual(history[-1]["to_status"], "APPROVED")
        audit = app.engine.audit(req["id"])
        self.assertEqual(len(audit), 9)
        self.assertEqual(results(audit), {"R1_ADULT": "PASS", "R2_EMPLOYED": "PASS",
                                          "R3_MIN_SCORE": "PASS", "R4_AFFORDABILITY": "PASS",
                                          "R5_SELF_EMPLOYED_SCORE": "SKIPPED"})

    def test_threshold_rejection_records_exact_values(self):
        app = make_app(self)
        req, _ = app.engine.submit("loan_approval", loan(applicant_id="cust004"))
        self.assertEqual(req["status"], "REJECTED")
        self.assertEqual(req["current_stage"], "risk_rules")
        self.assertEqual(req["reason"], "Rule R3_MIN_SCORE failed: Credit score must be at least 550")
        r3 = [a for a in app.engine.audit(req["id"]) if a["rule_id"] == "R3_MIN_SCORE"][0]
        self.assertEqual(r3["result"], "FAIL")
        self.assertEqual((r3["detail"]["trace"][0]["actual"], r3["detail"]["trace"][0]["expected"]),
                         (407, 550))

    def test_manual_review_branching_and_resolution(self):
        app = make_app(self)
        review, _ = app.engine.submit("vendor_approval",
                                      vendor(vendor_id="VND-200", annual_revenue=1000000))
        self.assertEqual(review["status"], "MANUAL_REVIEW")
        self.assertIn("V4_REVENUE_COVERS_CONTRACT", review["reason"])
        self.assertEqual(review["context"]["flags"], [])      # years_in_business=8, no flag
        done = app.engine.resolve_review(review["id"], "APPROVED", "priya", "income docs verified")
        self.assertEqual(done["status"], "APPROVED")
        self.assertEqual(done["reason"], "manual review by priya: income docs verified")
        last = app.engine.audit(review["id"])[-1]
        self.assertEqual((last["kind"], last["detail"]["reviewer"]), ("manual_review", "priya"))
        with self.assertRaises(InvalidState):                  # no longer in review
            app.engine.resolve_review(review["id"], "REJECTED", "priya")
        with self.assertRaises(BadRequest):
            app.engine.resolve_review(review["id"], "MAYBE", "priya")
        # branching: a large loan is routed to a MANUAL_REVIEW terminal stage
        big, _ = app.engine.submit("loan_approval", loan(income=500000, loan_amount=450000))
        self.assertEqual((big["status"], big["current_stage"]), ("MANUAL_REVIEW", "senior_review"))
        branch = [a for a in app.engine.audit(big["id"]) if a["kind"] == "branch"][0]
        self.assertEqual(branch["result"], "senior_review")
        # flag action: young vendor passes but is flagged
        flagged, _ = app.engine.submit("vendor_approval", vendor(vendor_id="VND-300",
                                                                 years_in_business=2))
        self.assertEqual(flagged["status"], "APPROVED")
        self.assertEqual(flagged["context"]["flags"][0]["rule_id"], "V5_NEW_VENDOR_WATCH")
        # sanctions hit is a hard reject
        hit, _ = app.engine.submit("vendor_approval", vendor(vendor_id="SAN-9"))
        self.assertEqual((hit["status"], hit["current_stage"]), ("REJECTED", "compliance_rules"))

    def test_conditional_rule_applies_only_when_condition_holds(self):
        app = make_app(self)
        weak, _ = app.engine.submit("loan_approval", loan(applicant_id="cust020",
                                                          employment="self_employed"))
        self.assertEqual(weak["status"], "MANUAL_REVIEW")      # score 580 < 620
        self.assertIn("R5_SELF_EMPLOYED_SCORE", weak["reason"])
        strong, _ = app.engine.submit("loan_approval", loan(applicant_id="cust021",
                                                            employment="self_employed"))
        self.assertEqual(strong["status"], "APPROVED")
        self.assertEqual(results(app.engine.audit(strong["id"]))["R5_SELF_EMPLOYED_SCORE"], "PASS")


class InputAndDuplicateTests(unittest.TestCase):
    def test_invalid_input_stored_as_invalid_and_unknown_workflow(self):
        app = make_app(self)
        req, created = app.engine.submit("loan_approval",
                                         {"applicant_id": "x1", "age": "abc", "employment": "pirate"})
        self.assertTrue(created)
        self.assertEqual(req["status"], "INVALID")
        self.assertEqual(len(req["context"]["errors"]), 5)
        self.assertEqual(app.deps.call_log, [])                # never executed
        audit = app.engine.audit(req["id"])
        self.assertEqual((len(audit), audit[0]["kind"], audit[0]["result"]), (1, "schema", "FAIL"))
        with self.assertRaises(InvalidState):
            app.engine.resume(req["id"])
        with self.assertRaises(WorkflowNotFound):
            app.engine.submit("no_such_workflow", {})

    def test_duplicate_replay_has_no_side_effects_and_key_conflict(self):
        app = make_app(self)
        first, c1 = app.engine.submit("loan_approval", loan(), idempotency_key="order-1001")
        calls = len(app.deps.call_log)
        audit_n = len(app.engine.audit(first["id"]))
        again, c2 = app.engine.submit("loan_approval", loan(), idempotency_key="order-1001")
        self.assertEqual((c1, c2), (True, False))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["version"], first["version"])
        self.assertEqual(len(app.deps.call_log), calls)
        self.assertEqual(len(app.engine.audit(first["id"])), audit_n)
        with self.assertRaises(IdempotencyConflict):           # same key, different payload
            app.engine.submit("loan_approval", loan(income=1), idempotency_key="order-1001")
        with self.assertRaises(IdempotencyConflict):           # same key, different workflow
            app.engine.submit("vendor_approval", vendor(), idempotency_key="order-1001")
        a, ca = app.engine.submit("loan_approval", loan(applicant_id="cust009"))
        b, cb = app.engine.submit("loan_approval", loan(applicant_id="cust009"))
        self.assertEqual((ca, cb, a["id"] == b["id"]), (True, False, True))   # hash is the key

    def test_concurrent_duplicates_resolve_to_one_request(self):
        app = make_app(self)
        barrier = threading.Barrier(8)
        out, errors = [], []

        def worker():
            try:
                barrier.wait()
                out.append(app.engine.submit("loan_approval", loan(), idempotency_key="race-1"))
            except Exception as exc:                            # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(len(out), 8)
        self.assertEqual(len({req["id"] for req, _ in out}), 1)
        self.assertEqual(sum(1 for _, created in out if created), 1)
        self.assertEqual(app.store.query("SELECT COUNT(*) AS n FROM requests")[0]["n"], 1)
        rid = out[0][0]["id"]
        self.assertEqual(len(app.engine.audit(rid)), 9)         # executed exactly once
        self.assertEqual(app.deps.executions["credit_bureau"], 1)


class ResilienceTests(unittest.TestCase):
    def test_inline_retry_with_exponential_backoff(self):
        sleeps = []
        app = make_app(self, sleeps)
        app.deps.fail_next("credit_bureau", 2)
        req, _ = app.engine.submit("loan_approval", loan())
        self.assertEqual(req["status"], "APPROVED")
        deps = [a for a in app.engine.audit(req["id"]) if a["kind"] == "dependency"]
        self.assertEqual([a["result"] for a in deps], ["ERROR", "ERROR", "OK"])
        self.assertEqual(sleeps, [0.2, 0.4])

    def test_pending_retry_then_success(self):
        app = make_app(self)
        app.deps.fail_next("credit_bureau", 3)
        req, _ = app.engine.submit("loan_approval", loan())
        self.assertEqual((req["status"], req["current_stage"]), ("PENDING_RETRY", "credit_check"))
        self.assertEqual(req["reason"], "dependency 'credit_bureau' unavailable; awaiting retry")
        done = app.engine.resume(req["id"])
        self.assertEqual(done["status"], "APPROVED")
        deps = [a["detail"]["attempt"] for a in app.engine.audit(req["id"])
                if a["kind"] == "dependency"]
        self.assertEqual(deps, [1, 2, 3, 4])
        self.assertEqual({k for _, k in app.deps.call_log}, {"%s:credit_check" % req["id"]})
        with self.assertRaises(InvalidState):                  # final states cannot resume
            app.engine.resume(req["id"])

    def test_retry_exhaustion_escalates_to_manual_review(self):
        app = make_app(self)
        app.deps.fail_next("credit_bureau", 100)
        req, _ = app.engine.submit("loan_approval", loan())
        self.assertEqual(req["status"], "PENDING_RETRY")
        final = app.engine.resume(req["id"])
        self.assertEqual(final["status"], "MANUAL_REVIEW")
        self.assertIn("6 attempts", final["reason"])
        errors = [a for a in app.engine.audit(req["id"]) if a["result"] == "ERROR"]
        self.assertEqual(len(errors), 6)                       # every attempt is audited

    def test_partial_save_failure_rolls_back_and_recovers(self):
        app = make_app(self)
        commits = []

        def explode_on_third_commit(point):
            commits.append(point)
            if len(commits) == 3:
                raise RuntimeError("simulated disk failure")

        app.store.fault_hook = explode_on_third_commit
        with self.assertRaises(RuntimeError):
            app.engine.submit("loan_approval", loan())
        app.store.fault_hook = None
        row = app.store.query("SELECT * FROM requests")[0]
        rid = row["id"]
        self.assertEqual((row["status"], row["current_stage"], row["version"]),
                         ("IN_PROGRESS", "credit_check", 3))   # failed commit left no trace
        self.assertEqual(len(app.engine.history(rid)), 3)
        self.assertEqual(len(app.engine.audit(rid)), 3)
        done = app.engine.resume(rid)                          # recoverable
        self.assertEqual(done["status"], "APPROVED")
        self.assertEqual(len(app.engine.history(rid)), 7)      # no duplicate rows
        self.assertEqual(len(app.engine.audit(rid)), 9)
        self.assertEqual(app.deps.executions["credit_bureau"], 1)   # idempotent dependency call

    def test_optimistic_lock_and_illegal_transitions(self):
        app = make_app(self)
        req, _ = app.engine.submit("loan_approval", loan())
        with self.assertRaises(ConcurrencyError):              # stale version
            app.store.commit_stage(req["id"], req["version"] - 1, "APPROVED", "approve",
                                   req["context"], "stale")
        with self.assertRaises(IllegalTransition):             # final states stay final
            app.store.commit_stage(req["id"], req["version"], "IN_PROGRESS", "approve",
                                   req["context"], "reopen")
        self.assertEqual(app.store.get_request(req["id"])["version"], req["version"])
        import sqlite3
        conn = sqlite3.connect(app.store.path)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.DatabaseError):         # audit is append-only
            conn.execute("DELETE FROM audit")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE history SET reason='rewritten'")


class ExplanationTests(unittest.TestCase):
    def test_explanation_content(self):
        app = make_app(self)
        ok, _ = app.engine.submit("loan_approval", loan())
        text = app.engine.explain(ok["id"])
        for expected in (
                "loan_approval@v1 status=APPROVED reason=All checks passed",
                "path: mandatory_checks -> credit_check -> risk_rules -> amount_branch -> approve",
                "[intake] schema validation PASS",
                "[credit_check] call credit_bureau attempt 1: OK -> {'score': 679",
                "R3_MIN_SCORE (Credit score must be at least 550) PASS: "
                "deps.credit.score=679 gte 550 -> True",
                "input.loan_amount=150000 lte 450000 -> True",
                "[risk_rules] R5_SELF_EMPLOYED_SCORE skipped (not applicable)",
                "[amount_branch] branch -> approve",
                "[approve] decision -> APPROVED: All checks passed"):
            self.assertIn(expected, text)
        rejected, _ = app.engine.submit("loan_approval", loan(applicant_id="cust004"))
        self.assertIn("FAIL: deps.credit.score=407 gte 550 -> False",
                      app.engine.explain(rejected["id"]))
        bad, _ = app.engine.submit("loan_approval", {"applicant_id": "x1"})
        self.assertIn("schema validation FAIL", app.engine.explain(bad["id"]))


if __name__ == "__main__":
    unittest.main()
