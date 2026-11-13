import copy
import json
import os
import sqlite3
import unittest

from helpers import CONFIGS, loan, loan_config, make_app, load_config
from wfdp.config import validate_workflow
from wfdp.errors import ConfigError
from wfdp.schema import validate_payload

SERVICES = ["credit_bureau", "sanctions"]


class SchemaTests(unittest.TestCase):
    def test_valid_payload_and_field_level_errors(self):
        schema = loan_config()["schema"]
        self.assertEqual(validate_payload(schema, loan()), [])
        errors = validate_payload(schema, {"applicant_id": "x1", "age": "abc",
                                           "employment": "pirate"})
        self.assertEqual(errors, [
            {"field": "applicant_id", "error": "length must be >= 3"},
            {"field": "age", "error": "expected integer, got str"},
            {"field": "income", "error": "required field missing"},
            {"field": "loan_amount", "error": "required field missing"},
            {"field": "employment",
             "error": "must be one of ['salaried', 'self_employed', 'unemployed']"}])
        self.assertEqual(validate_payload(schema, loan(age=True))[0]["error"],
                         "expected integer, got bool")
        self.assertEqual(validate_payload(schema, [1])[0]["field"], "$")


class ConfigValidationTests(unittest.TestCase):
    def test_shipped_configs_are_valid(self):
        names = [f for f in os.listdir(CONFIGS) if f.endswith(".json")]
        self.assertGreaterEqual(len(names), 2)
        for name in names:
            with open(os.path.join(CONFIGS, name), encoding="utf-8") as fh:
                validate_workflow(json.load(fh), SERVICES)

    def test_invalid_structures_are_rejected(self):
        def broken(mutate):
            cfg = loan_config()
            mutate(cfg)
            with self.assertRaises(ConfigError) as ctx:
                validate_workflow(cfg, SERVICES)
            return str(ctx.exception)

        self.assertIn("unknown stage", broken(
            lambda c: c["stages"]["mandatory_checks"].update(next="ghost")))
        self.assertIn("cycle detected", broken(
            lambda c: c["stages"]["risk_rules"].update(next="mandatory_checks")))
        self.assertIn("unknown service", broken(
            lambda c: c["stages"]["credit_check"].update(service="tarot_cards")))
        self.assertIn("unknown operator", broken(
            lambda c: c["stages"]["mandatory_checks"]["rules"][0]["check"].update(op="roughly")))
        self.assertIn("'start' refers to unknown stage", broken(
            lambda c: c.update(start="nowhere")))
        self.assertIn("decision must be", broken(
            lambda c: c["stages"]["approve"].update(decision="MAYBE")))

    def test_duplicate_rule_ids_are_rejected(self):
        cfg = loan_config()
        cfg["stages"]["risk_rules"]["rules"][0]["id"] = "R1_ADULT"
        with self.assertRaises(ConfigError) as ctx:
            validate_workflow(cfg, SERVICES)
        self.assertIn("duplicate rule id 'R1_ADULT'", str(ctx.exception))


class RegistryTests(unittest.TestCase):
    def test_version_pinning(self):
        app = make_app(self)
        app.deps.fail_next("credit_bureau", 3)
        pending, _ = app.engine.submit("loan_approval", loan())
        self.assertEqual(pending["status"], "PENDING_RETRY")
        self.assertEqual(pending["workflow_version"], 1)

        stricter = loan_config()
        stricter["stages"]["risk_rules"]["rules"][0]["check"]["value"] = 700
        self.assertEqual(app.registry.register(stricter), ("loan_approval", 2, True))

        new, _ = app.engine.submit("loan_approval", loan(), idempotency_key="after-change")
        self.assertEqual(new["workflow_version"], 2)
        self.assertEqual(new["status"], "REJECTED")           # 679 < 700 under v2
        resumed = app.engine.resume(pending["id"])
        self.assertEqual(resumed["workflow_version"], 1)      # still pinned to v1
        self.assertEqual(resumed["status"], "APPROVED")       # 679 >= 550 under v1

    def test_versions_immutable_and_bad_config_keeps_previous_serving(self):
        app = make_app(self)
        self.assertEqual(app.registry.register(loan_config()), ("loan_approval", 1, False))
        bad = loan_config()
        bad["stages"]["approve"]["decision"] = "MAYBE"
        with self.assertRaises(ConfigError):
            app.registry.register(bad)
        self.assertEqual(app.registry.resolve("loan_approval")[0], 1)
        self.assertEqual([w["version"] for w in app.registry.list() if w["id"] == "loan_approval"], [1])
        conn = sqlite3.connect(app.store.path)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE workflows SET config='{}' WHERE id='loan_approval'")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM workflows")

    def test_new_use_case_needs_only_configuration(self):
        expense = {
            "id": "expense_approval",
            "schema": {"fields": {"amount": {"type": "number", "min": 0},
                                  "category": {"type": "string", "enum": ["travel", "food"]}}},
            "start": "limit",
            "stages": {
                "limit": {"type": "rules", "next": "ok", "rules": [
                    {"id": "E1_LIMIT", "description": "Expense must be under 5000",
                     "check": {"field": "input.amount", "op": "lt", "value": 5000},
                     "on_fail": "reject"}]},
                "ok": {"type": "terminal", "decision": "APPROVED", "reason": "Within limit"}}}
        app = make_app(self)
        self.assertEqual(app.registry.register(expense), ("expense_approval", 1, True))
        small, _ = app.engine.submit("expense_approval", {"amount": 120, "category": "travel"})
        big, _ = app.engine.submit("expense_approval", {"amount": 99999, "category": "food"})
        self.assertEqual((small["status"], big["status"]), ("APPROVED", "REJECTED"))


if __name__ == "__main__":
    unittest.main()
