"""REST API (http.server). Holds no business logic: it only maps HTTP to the engine."""
import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import (BadRequest, ConcurrencyError, ConfigError, DependencyError,
                     IdempotencyConflict, InvalidState, RequestNotFound, WorkflowNotFound)

ROUTES = [
    ("POST", r"^/requests$", "submit"),
    ("GET", r"^/requests/(?P<id>[^/]+)$", "get_request"),
    ("GET", r"^/requests/(?P<id>[^/]+)/history$", "history"),
    ("GET", r"^/requests/(?P<id>[^/]+)/audit$", "audit"),
    ("GET", r"^/requests/(?P<id>[^/]+)/explanation$", "explanation"),
    ("POST", r"^/requests/(?P<id>[^/]+)/retry$", "retry"),
    ("POST", r"^/requests/(?P<id>[^/]+)/review$", "review"),
    ("GET", r"^/workflows$", "list_workflows"),
    ("POST", r"^/workflows$", "register_workflow"),
    ("POST", r"^/admin/dependencies/(?P<name>[^/]+)/fail$", "fail_dependency"),
]
ERROR_STATUS = ((WorkflowNotFound, 404), (RequestNotFound, 404), (IdempotencyConflict, 409),
                (InvalidState, 409), (ConcurrencyError, 409), (ConfigError, 400),
                (BadRequest, 400), (DependencyError, 400))


class Api:
    def __init__(self, app):
        self.app = app

    def dispatch(self, method, raw_path, headers, raw_body):
        """Return (status, extra_headers, body). A str body is sent as text/plain."""
        path = raw_path.split("?")[0].rstrip("/") or "/"
        try:
            for m, pattern, name in ROUTES:
                match = re.match(pattern, path)
                if match and m == method:
                    body = self._json(raw_body) if method == "POST" else None
                    return getattr(self, "_" + name)(headers, body, **match.groupdict())
            if any(re.match(p, path) for _, p, _ in ROUTES):
                return 405, {}, {"error": "method not allowed"}
            return 404, {}, {"error": "no such route"}
        except tuple(e for e, _ in ERROR_STATUS) as exc:
            for cls, status in ERROR_STATUS:
                if isinstance(exc, cls):
                    return status, {}, {"error": str(exc), **(
                        {"details": exc.errors} if isinstance(exc, ConfigError) else {})}
        except Exception:                                   # never leak internals
            traceback.print_exc(file=sys.stderr)
            return 500, {}, {"error": "internal error"}

    @staticmethod
    def _json(raw):
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("body must be valid JSON")
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        return body

    # -- handlers ---------------------------------------------------------
    def _submit(self, headers, body):
        key = headers.get("Idempotency-Key")
        request, created = self.app.engine.submit(body.get("workflow"), body.get("payload"), key)
        if not created:
            return 200, {"Idempotent-Replay": "true"}, request
        return (422 if request["status"] == "INVALID" else 201), {}, request

    def _get_request(self, headers, body, id):
        return 200, {}, self.app.engine.get(id)

    def _history(self, headers, body, id):
        return 200, {}, self.app.engine.history(id)

    def _audit(self, headers, body, id):
        return 200, {}, self.app.engine.audit(id)

    def _explanation(self, headers, body, id):
        return 200, {}, self.app.engine.explain(id)

    def _retry(self, headers, body, id):
        return 200, {}, self.app.engine.resume(id)

    def _review(self, headers, body, id):
        return 200, {}, self.app.engine.resolve_review(
            id, body.get("decision"), body.get("reviewer"), body.get("note", ""))

    def _list_workflows(self, headers, body):
        return 200, {}, self.app.registry.list()

    def _register_workflow(self, headers, body):
        wid, version, created = self.app.registry.register(body)
        return 201 if created else 200, {}, {"id": wid, "version": version, "created": created}

    def _fail_dependency(self, headers, body, name):
        count = body.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise BadRequest("'count' must be a non-negative integer")
        self.app.deps.fail_next(name, count)
        return 200, {}, {"service": name, "failing_next": self.app.deps.failing_next(name)}


def make_server(app, host="127.0.0.1", port=8080, quiet=False):
    api = Api(app)

    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            status, extra, body = api.dispatch(method, self.path, self.headers, raw)
            if isinstance(body, str):
                data, ctype = body.encode("utf-8"), "text/plain; charset=utf-8"
            else:
                data, ctype = json.dumps(body, indent=2).encode("utf-8"), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def log_message(self, fmt, *args):
            if not quiet:
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
