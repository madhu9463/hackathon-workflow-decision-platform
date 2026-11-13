"""Dependency registry with simulated external services.

Calls carry an idempotency key: repeating a call with the same key returns the
first successful response instead of running the service again.
"""
import copy
import hashlib
import threading

from .errors import DependencyError, TransientDependencyError

# Fixed scores so the demo/sample payloads behave the same on every machine.
# Any other applicant gets a deterministic score in 300..850 derived from a hash.
DEMO_CREDIT_SCORES = {"cust000": 679, "cust004": 407, "cust009": 680,
                      "cust020": 580, "cust021": 720}


def credit_bureau(params):
    applicant = str(params.get("applicant_id") or "")
    if not applicant:
        raise DependencyError("applicant_id is required")
    digest = hashlib.sha256(applicant.encode()).hexdigest()
    score = DEMO_CREDIT_SCORES.get(applicant, 300 + int(digest[:8], 16) % 551)
    return {"score": score, "bureau_ref": digest[:8]}


def sanctions(params):
    vendor_id = str(params.get("vendor_id") or "")
    name = str(params.get("vendor_name") or "")
    if not vendor_id:
        raise DependencyError("vendor_id is required")
    listed = "sanction" in name.lower() or vendor_id.upper().startswith("SAN-")
    digest = hashlib.sha256(vendor_id.encode()).hexdigest()
    return {"listed": listed, "screening_ref": digest[:8]}


class DependencyRegistry:
    def __init__(self):
        self._services = {}
        self._fail = {}
        self._cache = {}
        self._lock = threading.Lock()
        self.call_log = []      # every call attempt: (service, idempotency_key)
        self.executions = {}    # how many times each service really ran

    def register(self, name, fn):
        self._services[name] = fn

    def names(self):
        return sorted(self._services)

    def fail_next(self, name, count):
        """Simulation: make the next `count` calls to `name` fail transiently."""
        if name not in self._services:
            raise DependencyError("unknown service %r" % name)
        with self._lock:
            self._fail[name] = max(0, int(count))

    def failing_next(self, name):
        return self._fail.get(name, 0)

    def call(self, name, params, idempotency_key):
        with self._lock:
            fn = self._services.get(name)
            if fn is None:
                raise DependencyError("unknown service %r" % name)
            self.call_log.append((name, idempotency_key))
            cache_key = (name, idempotency_key)
            if cache_key in self._cache:
                return copy.deepcopy(self._cache[cache_key])
            if self._fail.get(name, 0) > 0:
                self._fail[name] -= 1
                raise TransientDependencyError("%s unavailable (simulated 503)" % name)
            result = fn(dict(params))
            self.executions[name] = self.executions.get(name, 0) + 1
            self._cache[cache_key] = result
            return copy.deepcopy(result)


def default_dependencies():
    deps = DependencyRegistry()
    deps.register("credit_bureau", credit_bureau)
    deps.register("sanctions", sanctions)
    return deps
