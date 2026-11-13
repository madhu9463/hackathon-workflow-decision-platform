"""SQLite persistence: atomic commits, optimistic locking, append-only logs."""
import contextlib
import datetime
import json
import sqlite3

from .errors import ConcurrencyError, IllegalTransition, RequestNotFound

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT NOT NULL, version INTEGER NOT NULL, config TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (id, version));
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL, workflow_version INTEGER NOT NULL,
    payload_hash TEXT NOT NULL, status TEXT NOT NULL, current_stage TEXT,
    context TEXT NOT NULL, reason TEXT, version INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
    from_status TEXT, to_status TEXT NOT NULL, from_stage TEXT, to_stage TEXT,
    reason TEXT, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_history_request ON history(request_id);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
    stage TEXT, kind TEXT NOT NULL, rule_id TEXT, description TEXT,
    result TEXT, detail TEXT, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit(request_id);

CREATE TRIGGER IF NOT EXISTS history_no_update BEFORE UPDATE ON history
  BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS history_no_delete BEFORE DELETE ON history
  BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS workflows_no_update BEFORE UPDATE ON workflows
  BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS workflows_no_delete BEFORE DELETE ON workflows
  BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
"""

# Lifecycle rules; the store rejects anything else.
ALLOWED_TRANSITIONS = {
    "RECEIVED": {"IN_PROGRESS", "INVALID"},
    "IN_PROGRESS": {"IN_PROGRESS", "PENDING_RETRY", "MANUAL_REVIEW", "APPROVED", "REJECTED"},
    "PENDING_RETRY": {"IN_PROGRESS"},
    "MANUAL_REVIEW": {"APPROVED", "REJECTED"},
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path):
        self.path = path
        self.fault_hook = None  # tests set this to inject a failure before COMMIT
        conn = self._connect()
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    # -- plumbing ---------------------------------------------------------
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextlib.contextmanager
    def transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def query(self, sql, params=()):
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    @staticmethod
    def _request(row):
        if row is None:
            return None
        data = dict(row)
        data["context"] = json.loads(data["context"])
        return data

    # -- workflows --------------------------------------------------------
    def add_workflow_version(self, workflow_id, config):
        """Store a new immutable version. Identical config -> no new version."""
        with self.transaction() as c:
            row = c.execute("SELECT version, config FROM workflows WHERE id=? "
                            "ORDER BY version DESC LIMIT 1", (workflow_id,)).fetchone()
            if row and _canonical(json.loads(row["config"])) == _canonical(config):
                return row["version"], False
            version = (row["version"] if row else 0) + 1
            c.execute("INSERT INTO workflows (id, version, config, created_at) VALUES (?,?,?,?)",
                      (workflow_id, version, json.dumps(config), now()))
            return version, True

    def get_workflow(self, workflow_id, version):
        rows = self.query("SELECT config FROM workflows WHERE id=? AND version=?",
                          (workflow_id, version))
        return json.loads(rows[0]["config"]) if rows else None

    def latest_version(self, workflow_id):
        rows = self.query("SELECT MAX(version) AS v FROM workflows WHERE id=?", (workflow_id,))
        return rows[0]["v"] if rows and rows[0]["v"] is not None else None

    def list_workflows(self):
        return [dict(r) for r in self.query(
            "SELECT id, version, created_at FROM workflows ORDER BY id, version")]

    # -- requests ---------------------------------------------------------
    def create_request(self, request_id, idem_key, workflow_id, workflow_version,
                       payload_hash, start_stage, context):
        """Insert a RECEIVED request. The UNIQUE idem_key makes duplicates race safely:
        exactly one insert wins; losers get the stored row back with created=False."""
        ts = now()
        try:
            with self.transaction() as c:
                c.execute(
                    "INSERT INTO requests (id, idem_key, workflow_id, workflow_version, "
                    "payload_hash, status, current_stage, context, reason, version, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request_id, idem_key, workflow_id, workflow_version, payload_hash,
                     "RECEIVED", start_stage, json.dumps(context), "request received", 1, ts, ts))
                c.execute(
                    "INSERT INTO history (request_id, from_status, to_status, from_stage, "
                    "to_stage, reason, ts) VALUES (?,?,?,?,?,?,?)",
                    (request_id, None, "RECEIVED", None, start_stage, "request received", ts))
            return self.get_request(request_id), True
        except sqlite3.IntegrityError:
            existing = self.get_by_idem_key(idem_key)
            if existing is None:
                raise
            return existing, False

    def get_request(self, request_id):
        rows = self.query("SELECT * FROM requests WHERE id=?", (request_id,))
        return self._request(rows[0]) if rows else None

    def get_by_idem_key(self, idem_key):
        rows = self.query("SELECT * FROM requests WHERE idem_key=?", (idem_key,))
        return self._request(rows[0]) if rows else None

    def commit_stage(self, request_id, expected_version, status, stage, context, reason,
                     audit=()):
        """ONE atomic transaction: request row + history row + audit rows.
        Raises ConcurrencyError on a stale version and IllegalTransition on a
        forbidden status move; either way nothing is written."""
        ts = now()
        with self.transaction() as c:
            row = c.execute("SELECT status, current_stage, version FROM requests WHERE id=?",
                            (request_id,)).fetchone()
            if row is None:
                raise RequestNotFound(request_id)
            if row["version"] != expected_version:
                raise ConcurrencyError("request %s changed (expected v%s, found v%s)"
                                       % (request_id, expected_version, row["version"]))
            if status not in ALLOWED_TRANSITIONS.get(row["status"], set()):
                raise IllegalTransition("%s -> %s is not allowed" % (row["status"], status))
            cur = c.execute(
                "UPDATE requests SET status=?, current_stage=?, context=?, reason=?, "
                "version=version+1, updated_at=? WHERE id=? AND version=?",
                (status, stage, json.dumps(context), reason, ts, request_id, expected_version))
            if cur.rowcount != 1:
                raise ConcurrencyError("stale write rejected for %s" % request_id)
            c.execute(
                "INSERT INTO history (request_id, from_status, to_status, from_stage, "
                "to_stage, reason, ts) VALUES (?,?,?,?,?,?,?)",
                (request_id, row["status"], status, row["current_stage"], stage, reason, ts))
            for a in audit:
                c.execute(
                    "INSERT INTO audit (request_id, stage, kind, rule_id, description, result, "
                    "detail, ts) VALUES (?,?,?,?,?,?,?,?)",
                    (request_id, a.get("stage"), a["kind"], a.get("rule_id"),
                     a.get("description"), a.get("result"),
                     json.dumps(a.get("detail") or {}), ts))
            if self.fault_hook:
                self.fault_hook("before_commit")
        return self.get_request(request_id)

    def list_history(self, request_id):
        return [dict(r) for r in self.query(
            "SELECT * FROM history WHERE request_id=? ORDER BY seq", (request_id,))]

    def list_audit(self, request_id):
        out = []
        for r in self.query("SELECT * FROM audit WHERE request_id=? ORDER BY seq", (request_id,)):
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            out.append(d)
        return out
