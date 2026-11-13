"""Config-driven intake validation.

schema = {"fields": {"age": {"type": "integer", "min": 0, "required": true}, ...},
          "allow_extra": true}
Fields are required unless "required": false.
"""
import re

TYPE_MAP = {"string": (str,), "integer": (int,), "number": (int, float),
            "boolean": (bool,), "array": (list,), "object": (dict,)}
FIELD_KEYS = {"type", "required", "enum", "min", "max", "min_length", "max_length", "pattern"}


def validate_schema_definition(schema):
    """Return a list of problems with the schema itself (empty if fine)."""
    if not isinstance(schema, dict) or not isinstance(schema.get("fields"), dict) \
            or not schema["fields"]:
        return ["schema.fields must be a non-empty object"]
    problems = []
    for name, spec in schema["fields"].items():
        where = "schema field '%s'" % name
        if not isinstance(spec, dict):
            problems.append("%s: must be an object" % where)
            continue
        if spec.get("type") not in TYPE_MAP:
            problems.append("%s: unknown type %r" % (where, spec.get("type")))
        extra = set(spec) - FIELD_KEYS
        if extra:
            problems.append("%s: unknown keys %s" % (where, sorted(extra)))
        if "enum" in spec and not isinstance(spec["enum"], list):
            problems.append("%s: enum must be a list" % where)
        if "pattern" in spec:
            try:
                re.compile(spec["pattern"])
            except (re.error, TypeError):
                problems.append("%s: invalid pattern" % where)
    return problems


def _type_ok(value, type_name):
    if type_name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, TYPE_MAP[type_name])


def validate_payload(schema, payload):
    """Return a list of {"field", "error"} dicts; empty means valid."""
    if not isinstance(payload, dict):
        return [{"field": "$", "error": "payload must be a JSON object"}]
    errors = []
    fields = schema.get("fields", {})
    for name, spec in fields.items():
        value = payload.get(name)
        if value is None:
            if spec.get("required", True):
                errors.append({"field": name, "error": "required field missing"})
            continue
        type_name = spec.get("type")
        if type_name and not _type_ok(value, type_name):
            errors.append({"field": name,
                           "error": "expected %s, got %s" % (type_name, type(value).__name__)})
            continue
        if "enum" in spec and value not in spec["enum"]:
            errors.append({"field": name, "error": "must be one of %s" % (spec["enum"],)})
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "min" in spec and value < spec["min"]:
                errors.append({"field": name, "error": "must be >= %s" % spec["min"]})
            if "max" in spec and value > spec["max"]:
                errors.append({"field": name, "error": "must be <= %s" % spec["max"]})
        if isinstance(value, (str, list)):
            if "min_length" in spec and len(value) < spec["min_length"]:
                errors.append({"field": name, "error": "length must be >= %s" % spec["min_length"]})
            if "max_length" in spec and len(value) > spec["max_length"]:
                errors.append({"field": name, "error": "length must be <= %s" % spec["max_length"]})
        if isinstance(value, str) and "pattern" in spec and not re.search(spec["pattern"], value):
            errors.append({"field": name, "error": "does not match pattern %s" % spec["pattern"]})
    if not schema.get("allow_extra", True):
        for name in payload:
            if name not in fields:
                errors.append({"field": name, "error": "unexpected field"})
    return errors
