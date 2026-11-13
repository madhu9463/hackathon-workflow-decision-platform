"""Command-line interface:  python -m wfdp <command> ..."""
import argparse
import json
import os
import sys

from . import DEFAULT_CONFIGS, build_app
from .api import make_server
from .errors import WfdpError

KEYS = ("id", "status", "current_stage", "reason")


def _load_json(arg):
    text = arg.strip()
    if text.startswith("{"):
        return json.loads(text)
    with open(arg, encoding="utf-8") as fh:
        return json.load(fh)


def _brief(request, **extra):
    out = dict(extra)
    out.update({k: request[k] for k in KEYS})
    return out


def _print(obj):
    print(json.dumps(obj, indent=2))


def _color(line, enabled):
    if not enabled:
        return line
    green, red, amber, reset = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
    if "skipped" in line or "FLAGGED" in line or "PENDING_RETRY" in line:
        return amber + line + reset
    if " FAIL" in line or "ERROR" in line or "REJECTED" in line or "INVALID" in line:
        return red + line + reset
    if " PASS" in line or ": OK" in line or "APPROVED" in line:
        return green + line + reset
    return line


def build_parser():
    p = argparse.ArgumentParser(prog="wfdp", description="Configurable Workflow Decision Platform")
    p.add_argument("--db", default=os.environ.get("WFDP_DB", "wfdp.db"), help="SQLite file")
    p.add_argument("--configs", default=DEFAULT_CONFIGS, help="folder of workflow JSON files")
    p.add_argument("--no-color", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the REST API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)

    s = sub.add_parser("submit", help="submit a request")
    s.add_argument("workflow")
    s.add_argument("payload", help="path to a JSON file, or an inline JSON object")
    s.add_argument("--idempotency-key")
    s.add_argument("--simulate-failures", type=int, default=0, metavar="N",
                   help="make the next N dependency calls fail")
    s.add_argument("--service", default="credit_bureau", help="service for --simulate-failures")
    s.add_argument("--full", action="store_true", help="print the whole stored request")

    for name, helptext in (("retry", "resume a PENDING_RETRY request"),
                           ("explain", "print the decision trace"),
                           ("show", "print the stored request"),
                           ("history", "print state history"),
                           ("audit", "print the audit trail")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("id")

    s = sub.add_parser("review", help="resolve a MANUAL_REVIEW request")
    s.add_argument("id")
    s.add_argument("--decision", required=True, choices=["APPROVED", "REJECTED"])
    s.add_argument("--reviewer", required=True)
    s.add_argument("--note", default="")

    sub.add_parser("workflows", help="list workflow versions")
    s = sub.add_parser("register", help="register a workflow JSON file")
    s.add_argument("file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    app = build_app(args.db, args.configs)
    try:
        if args.cmd == "serve":
            server = make_server(app, args.host, args.port)
            print("Serving on http://%s:%d  (db: %s)  Ctrl+C to stop"
                  % (args.host, server.server_address[1], args.db))
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped")
            finally:
                server.server_close()
        elif args.cmd == "submit":
            if args.simulate_failures:
                app.deps.fail_next(args.service, args.simulate_failures)
            request, created = app.engine.submit(args.workflow, _load_json(args.payload),
                                                 args.idempotency_key)
            _print(request if args.full else _brief(request, created=created))
        elif args.cmd == "retry":
            _print(_brief(app.engine.resume(args.id)))
        elif args.cmd == "explain":
            use_color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
            for line in app.engine.explain(args.id).splitlines():
                print(_color(line, use_color))
        elif args.cmd == "show":
            _print(app.engine.get(args.id))
        elif args.cmd == "history":
            _print(app.engine.history(args.id))
        elif args.cmd == "audit":
            _print(app.engine.audit(args.id))
        elif args.cmd == "review":
            _print(_brief(app.engine.resolve_review(args.id, args.decision, args.reviewer,
                                                    args.note)))
        elif args.cmd == "workflows":
            _print(app.registry.list())
        elif args.cmd == "register":
            wid, version, created = app.registry.register(_load_json(args.file))
            _print({"id": wid, "version": version, "created": created})
    except WfdpError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
