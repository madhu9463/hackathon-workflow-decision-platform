"""Declarative condition language.

Leaf:        {"field": "deps.credit.score", "op": "gte", "value": 550}
Computed:    {"field": "input.loan", "op": "lte", "value": {"field": "input.income", "mul": 5}}
Combinators: {"all": [...]}, {"any": [...]}, {"not": {...}}

Evaluation returns a Result with a trace of every comparison. Missing or
incomparable data FAILS CLOSED: a condition can never pass on absent evidence.
"""
import re

from .errors import ConditionError

MISSING = object()
OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "matches", "exists"}
ORDER_OPS = {"gt", "gte", "lt", "lte"}
COMBINATORS = ("all", "any", "not")


class Result:
    __slots__ = ("ok", "indeterminate", "trace")

    def __init__(self, ok, indeterminate, trace):
        self.ok = ok
        self.indeterminate = indeterminate  # True when data was missing/incomparable
        self.trace = trace


def lookup(context, path):
    """Resolve a dotted path like 'deps.credit.score'; MISSING if absent."""
    cur = context
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return MISSING
    return cur


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def resolve_operand(operand, context):
    """A literal, or {"field": path, "mul": n, "add": n} read from the context."""
    if isinstance(operand, dict):
        value = lookup(context, operand["field"])
        if value is MISSING:
            return MISSING
        if "mul" in operand or "add" in operand:
            if not _is_number(value):
                return MISSING
            if "mul" in operand:
                value = value * operand["mul"]
            if "add" in operand:
                value = value + operand["add"]
        return value
    return operand


def _apply(op, actual, expected):
    if op in ("eq", "ne"):
        same = (isinstance(actual, bool) == isinstance(expected, bool)) and actual == expected
        return same if op == "eq" else not same
    if op in ORDER_OPS:
        both_num = _is_number(actual) and _is_number(expected)
        both_str = isinstance(actual, str) and isinstance(expected, str)
        if not (both_num or both_str):
            raise TypeError("incomparable types")
        return {"gt": actual > expected, "gte": actual >= expected,
                "lt": actual < expected, "lte": actual <= expected}[op]
    if op in ("in", "not_in"):
        if not isinstance(expected, (list, tuple)):
            raise TypeError("expected a list")
        found = actual in expected
        return found if op == "in" else not found
    if op == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, (list, tuple)):
            return expected in actual
        raise TypeError("contains needs a string or list")
    if op == "matches":
        if not (isinstance(actual, str) and isinstance(expected, str)):
            raise TypeError("matches needs strings")
        return re.search(expected, actual) is not None
    raise ConditionError("unknown operator %r" % (op,))


def _leaf(cond, context):
    field, op = cond["field"], cond["op"]
    actual = lookup(context, field)
    if op == "exists":
        want = bool(cond.get("value", True))
        present = actual is not MISSING
        ok = present == want
        entry = {"field": field, "op": op, "expected": want,
                 "actual": None if not present else actual,
                 "missing": not present, "result": ok}
        return Result(ok, False, [entry])
    expected = resolve_operand(cond.get("value"), context)
    missing = actual is MISSING or expected is MISSING
    ok, indeterminate = False, missing
    if not missing:
        try:
            ok = bool(_apply(op, actual, expected))
        except (TypeError, ValueError, re.error):
            ok, indeterminate = False, True
    entry = {"field": field, "op": op,
             "expected": None if expected is MISSING else expected,
             "actual": None if actual is MISSING else actual,
             "missing": missing, "result": ok}
    return Result(ok, indeterminate, [entry])


def evaluate(cond, context):
    if not isinstance(cond, dict):
        raise ConditionError("condition must be an object")
    if "all" in cond:
        parts = [evaluate(c, context) for c in cond["all"]]
        ok = all(p.ok for p in parts)
        definite_false = any((not p.ok) and (not p.indeterminate) for p in parts)
        ind = (not ok) and (not definite_false) and any(p.indeterminate for p in parts)
        return Result(ok, ind, [t for p in parts for t in p.trace])
    if "any" in cond:
        parts = [evaluate(c, context) for c in cond["any"]]
        ok = any(p.ok for p in parts)
        ind = (not ok) and any(p.indeterminate for p in parts)
        return Result(ok, ind, [t for p in parts for t in p.trace])
    if "not" in cond:
        inner = evaluate(cond["not"], context)
        if inner.indeterminate:          # fail closed: cannot negate unknown evidence
            return Result(False, True, inner.trace)
        return Result(not inner.ok, False, inner.trace)
    return _leaf(cond, context)


def _validate_operand(operand, where):
    if isinstance(operand, dict):
        if not isinstance(operand.get("field"), str) or not operand["field"]:
            raise ConditionError("%s: computed operand needs a 'field'" % where)
        extra = set(operand) - {"field", "mul", "add"}
        if extra:
            raise ConditionError("%s: unknown operand keys %s" % (where, sorted(extra)))
        for k in ("mul", "add"):
            if k in operand and not _is_number(operand[k]):
                raise ConditionError("%s: '%s' must be a number" % (where, k))


def validate_condition(cond, where="condition"):
    """Raise ConditionError if `cond` is malformed or uses an unknown operator."""
    if not isinstance(cond, dict):
        raise ConditionError("%s: must be an object" % where)
    present = [k for k in COMBINATORS if k in cond]
    if present:
        if len(present) != 1 or len(cond) != 1:
            raise ConditionError("%s: a combinator must be the only key" % where)
        key = present[0]
        if key == "not":
            validate_condition(cond["not"], where + ".not")
        else:
            items = cond[key]
            if not isinstance(items, list) or not items:
                raise ConditionError("%s: '%s' needs a non-empty list" % (where, key))
            for i, child in enumerate(items):
                validate_condition(child, "%s.%s[%d]" % (where, key, i))
        return
    extra = set(cond) - {"field", "op", "value"}
    if extra:
        raise ConditionError("%s: unknown keys %s" % (where, sorted(extra)))
    if not isinstance(cond.get("field"), str) or not cond["field"]:
        raise ConditionError("%s: 'field' is required" % where)
    op = cond.get("op")
    if op not in OPS:
        raise ConditionError("%s: unknown operator %r" % (where, op))
    if op == "exists":
        if "value" in cond and not isinstance(cond["value"], bool):
            raise ConditionError("%s: 'exists' value must be true or false" % where)
        return
    if "value" not in cond:
        raise ConditionError("%s: 'value' is required for operator %r" % (where, op))
    _validate_operand(cond["value"], where)
