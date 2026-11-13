"""Workflow validation and the versioned, immutable registry."""
import json
import os
import re

from .conditions import validate_condition
from .errors import ConditionError, ConfigError, WorkflowNotFound
from .schema import validate_schema_definition

STAGE_TYPES = {"rules", "dependency", "branch", "terminal"}
RULE_ACTIONS = {"reject", "manual_review", "flag"}
DECISIONS = {"APPROVED", "REJECTED", "MANUAL_REVIEW"}
EXHAUSTION_ACTIONS = {"manual_review", "reject"}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_workflow(config, known_services=None):
    """Raise ConfigError listing every problem: dangling stages, cycles, unknown
    operators or services, duplicate rule ids, bad schema, bad retry settings."""
    if not isinstance(config, dict):
        raise ConfigError("workflow config must be a JSON object")
    errors = []
    wid = config.get("id")
    if not isinstance(wid, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", wid):
        errors.append("'id' must be 1-64 characters: letters, digits, '_' or '-'")
    errors += validate_schema_definition(config.get("schema"))
    stages = config.get("stages")
    if not isinstance(stages, dict) or not stages:
        raise ConfigError(errors + ["'stages' must be a non-empty object"])
    if config.get("start") not in stages:
        errors.append("'start' refers to unknown stage %r" % (config.get("start"),))

    rule_ids = set()
    graph = {}
    for name, st in stages.items():
        where = "stage '%s'" % name
        if not isinstance(st, dict):
            errors.append("%s: must be an object" % where)
            continue
        kind = st.get("type")
        if kind not in STAGE_TYPES:
            errors.append("%s: unknown stage type %r" % (where, kind))
            continue
        edges = []
        if kind == "rules":
            rules = st.get("rules")
            if not isinstance(rules, list) or not rules:
                errors.append("%s: 'rules' must be a non-empty list" % where)
                rules = []
            for i, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    errors.append("%s: rule #%d must be an object" % (where, i))
                    continue
                rid = rule.get("id")
                if not isinstance(rid, str) or not rid:
                    errors.append("%s: rule #%d needs an 'id'" % (where, i))
                elif rid in rule_ids:
                    errors.append("duplicate rule id '%s'" % rid)
                else:
                    rule_ids.add(rid)
                if rule.get("on_fail", "reject") not in RULE_ACTIONS:
                    errors.append("%s rule %s: on_fail must be one of %s"
                                  % (where, rid, sorted(RULE_ACTIONS)))
                for key in ("check", "when"):
                    if key in rule:
                        try:
                            validate_condition(rule[key], "%s rule %s %s" % (where, rid, key))
                        except ConditionError as exc:
                            errors.append(str(exc))
                if "check" not in rule:
                    errors.append("%s rule %s: 'check' is required" % (where, rid))
            edges.append(st.get("next"))
        elif kind == "dependency":
            service = st.get("service")
            if not isinstance(service, str) or not service:
                errors.append("%s: 'service' is required" % where)
            elif known_services is not None and service not in known_services:
                errors.append("%s: unknown service '%s'" % (where, service))
            params = st.get("params", {})
            if not isinstance(params, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in params.items()):
                errors.append("%s: 'params' must map names to context paths" % where)
            if st.get("on_exhausted", "manual_review") not in EXHAUSTION_ACTIONS:
                errors.append("%s: on_exhausted must be one of %s"
                              % (where, sorted(EXHAUSTION_ACTIONS)))
            r = st.get("retries", {})
            if not isinstance(r, dict):
                errors.append("%s: 'retries' must be an object" % where)
            else:
                per_run = r.get("max_attempts", 3)
                total = r.get("max_total_attempts", per_run * 2 if _num(per_run) else 6)
                if not isinstance(per_run, int) or per_run < 1:
                    errors.append("%s: retries.max_attempts must be an integer >= 1" % where)
                elif not isinstance(total, int) or total < per_run:
                    errors.append("%s: retries.max_total_attempts must be >= max_attempts" % where)
                for k in ("backoff_base_seconds", "backoff_factor"):
                    if k in r and (not _num(r[k]) or r[k] < 0):
                        errors.append("%s: retries.%s must be a number >= 0" % (where, k))
            edges.append(st.get("next"))
        elif kind == "branch":
            branches = st.get("branches", [])
            if not isinstance(branches, list):
                errors.append("%s: 'branches' must be a list" % where)
                branches = []
            for i, b in enumerate(branches):
                if not isinstance(b, dict) or "when" not in b:
                    errors.append("%s: branch #%d needs 'when' and 'next'" % (where, i))
                    continue
                try:
                    validate_condition(b["when"], "%s branch #%d" % (where, i))
                except ConditionError as exc:
                    errors.append(str(exc))
                edges.append(b.get("next"))
            edges.append(st.get("default"))
        else:  # terminal
            if st.get("decision") not in DECISIONS:
                errors.append("%s: decision must be one of %s" % (where, sorted(DECISIONS)))
            if "next" in st:
                errors.append("%s: a terminal stage cannot have 'next'" % where)
        for e in edges:
            if e not in stages:
                errors.append("%s points to unknown stage %r" % (where, e))
        graph[name] = [e for e in edges if e in stages]

    # cycle detection (depth-first)
    state = {}

    def visit(node, path):
        state[node] = 1
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                errors.append("cycle detected: %s" % " -> ".join(path + [node, nxt]))
            elif state.get(nxt) is None:
                visit(nxt, path + [node])
        state[node] = 2

    for n in graph:
        if state.get(n) is None:
            visit(n, [])
    if errors:
        raise ConfigError(errors)


class Registry:
    """Versioned, immutable workflow registry backed by the store."""

    def __init__(self, store, known_services=None):
        self.store = store
        self._known = known_services
        self._cache = {}

    def register(self, config):
        """Validate then store. Returns (id, version, created). Invalid configs are
        rejected before anything is written, so the previous version keeps serving."""
        known = self._known() if callable(self._known) else self._known
        validate_workflow(config, known)
        version, created = self.store.add_workflow_version(config["id"], config)
        return config["id"], version, created

    def load_directory(self, directory):
        out = []
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(directory, fname)
            try:
                with open(path, encoding="utf-8") as fh:
                    config = json.load(fh)
                out.append(self.register(config))
            except (ValueError, ConfigError) as exc:
                raise ConfigError("%s: %s" % (fname, exc))
        return out

    def resolve(self, workflow_id, version=None):
        """Return (version, config) for a version, or the latest if none is given."""
        if version is None:
            version = self.store.latest_version(workflow_id)
        if version is None:
            raise WorkflowNotFound("unknown workflow %r" % (workflow_id,))
        key = (workflow_id, version)
        if key not in self._cache:
            config = self.store.get_workflow(workflow_id, version)
            if config is None:
                raise WorkflowNotFound("unknown workflow %s@v%s" % (workflow_id, version))
            self._cache[key] = config      # versions are immutable, so caching is safe
        return version, self._cache[key]

    def get(self, workflow_id, version=None):
        return self.resolve(workflow_id, version)[1]

    def list(self):
        return self.store.list_workflows()
