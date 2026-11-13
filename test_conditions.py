import unittest

from wfdp.conditions import evaluate, validate_condition
from wfdp.errors import ConditionError

CTX = {"input": {"age": 34, "name": "Asha", "tags": ["a", "b"], "code": "AB-12",
                 "income": 90000, "loan": 150000},
       "deps": {"score": 679}}


def ev(cond):
    return evaluate(cond, CTX)


class ConditionTests(unittest.TestCase):
    def test_operators_and_trace(self):
        cases = [
            ({"field": "input.age", "op": "eq", "value": 34}, True),
            ({"field": "input.age", "op": "ne", "value": 34}, False),
            ({"field": "input.age", "op": "gt", "value": 30}, True),
            ({"field": "input.age", "op": "gte", "value": 34}, True),
            ({"field": "input.age", "op": "lt", "value": 34}, False),
            ({"field": "input.age", "op": "lte", "value": 34}, True),
            ({"field": "input.name", "op": "in", "value": ["Asha", "Ravi"]}, True),
            ({"field": "input.name", "op": "not_in", "value": ["Asha"]}, False),
            ({"field": "input.tags", "op": "contains", "value": "a"}, True),
            ({"field": "input.name", "op": "contains", "value": "sh"}, True),
            ({"field": "input.code", "op": "matches", "value": "^[A-Z]{2}-\\d+$"}, True),
            ({"field": "input.name", "op": "exists"}, True),
            ({"field": "input.nope", "op": "exists"}, False),
            ({"field": "input.nope", "op": "exists", "value": False}, True),
        ]
        for cond, expected in cases:
            with self.subTest(cond=cond):
                self.assertEqual(ev(cond).ok, expected)
        entry = ev({"field": "deps.score", "op": "gte", "value": 550}).trace[0]
        self.assertEqual((entry["field"], entry["op"], entry["expected"], entry["actual"],
                          entry["missing"], entry["result"]),
                         ("deps.score", "gte", 550, 679, False, True))

    def test_combinators(self):
        yes = {"field": "input.age", "op": "gte", "value": 18}
        no = {"field": "input.age", "op": "lt", "value": 18}
        self.assertTrue(ev({"all": [yes, yes]}).ok)
        self.assertFalse(ev({"all": [yes, no]}).ok)
        self.assertTrue(ev({"any": [no, yes]}).ok)
        self.assertFalse(ev({"any": [no, no]}).ok)
        self.assertTrue(ev({"not": no}).ok)
        self.assertEqual(len(ev({"all": [yes, no]}).trace), 2)   # full trace kept

    def test_computed_operands(self):
        within = {"field": "input.loan", "op": "lte",
                  "value": {"field": "input.income", "mul": 5}}
        r = ev(within)
        self.assertTrue(r.ok)
        self.assertEqual(r.trace[0]["expected"], 450000)
        too_big = {"field": "input.loan", "op": "lte",
                   "value": {"field": "input.age", "mul": 1000}}
        self.assertFalse(ev(too_big).ok)
        plus = {"field": "input.loan", "op": "eq", "value": {"field": "input.income", "add": 60000}}
        self.assertTrue(ev(plus).ok)

    def test_missing_and_incomparable_data_fail_closed(self):
        missing = {"field": "input.nope", "op": "gte", "value": 1}
        r = ev(missing)
        self.assertFalse(r.ok)
        self.assertTrue(r.indeterminate)
        self.assertTrue(r.trace[0]["missing"])
        self.assertFalse(ev({"not": missing}).ok)        # cannot negate absent evidence
        self.assertFalse(ev({"field": "input.age", "op": "gt", "value": "abc"}).ok)
        self.assertFalse(ev({"field": "input.age", "op": "contains", "value": 3}).ok)
        self.assertFalse(ev({"field": "input.name", "op": "matches", "value": "("}).ok)
        self.assertFalse(ev({"field": "input.age", "op": "eq", "value": True}).ok)

    def test_unknown_operator_and_bad_shapes_rejected(self):
        for bad in ({"field": "x", "op": "between", "value": 1},
                    {"field": "x", "op": "eq"},
                    {"op": "eq", "value": 1},
                    {"all": []},
                    {"all": [{"field": "x", "op": "eq", "value": 1}], "extra": 1},
                    {"field": "x", "op": "eq", "value": {"mul": 2}},
                    "not a dict"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConditionError):
                    validate_condition(bad)
        validate_condition({"all": [{"field": "x", "op": "gte", "value": 1}]})


if __name__ == "__main__":
    unittest.main()
