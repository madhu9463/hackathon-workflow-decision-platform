import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import loan, loan_config, make_app
from wfdp.api import make_server


class ApiTests(unittest.TestCase):
    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=dict(headers or {}, **{"Content-Type": "application/json"}))
        try:
            resp = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as err:
            resp = err
        raw = resp.read().decode()
        ctype = resp.headers.get("Content-Type", "")
        return resp.status if hasattr(resp, "status") else resp.code, resp.headers, \
            (json.loads(raw) if "json" in ctype else raw)

    def test_rest_api_flow(self):
        app = make_app(self)
        server = make_server(app, "127.0.0.1", 0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]

        body = {"workflow": "loan_approval", "payload": loan()}
        status, _, req = self.call("POST", "/requests", body, {"Idempotency-Key": "order-1001"})
        self.assertEqual((status, req["status"], req["version"]), (201, "APPROVED", 7))
        rid = req["id"]

        status, headers, again = self.call("POST", "/requests", body, {"Idempotency-Key": "order-1001"})
        self.assertEqual((status, headers["Idempotent-Replay"], again["id"]), (200, "true", rid))
        status, _, err = self.call("POST", "/requests", {"workflow": "loan_approval", "payload": loan(income=1)},
                                   {"Idempotency-Key": "order-1001"})
        self.assertEqual(status, 409)
        self.assertIn("different payload", err["error"])

        status, _, bad = self.call("POST", "/requests", {"workflow": "loan_approval",
                                                         "payload": {"applicant_id": "x1", "age": "abc",
                                                                     "employment": "pirate"}})
        self.assertEqual((status, bad["status"], len(bad["context"]["errors"])), (422, "INVALID", 5))
        self.assertEqual(self.call("POST", "/requests", {"workflow": "nope", "payload": {}})[0], 404)
        self.assertEqual(self.call("POST", "/requests", {"payload": {}})[0], 400)

        self.assertEqual(self.call("GET", "/requests/" + rid)[2]["status"], "APPROVED")
        self.assertEqual(len(self.call("GET", "/requests/%s/history" % rid)[2]), 7)
        audit = self.call("GET", "/requests/%s/audit" % rid)[2]
        self.assertEqual(audit[3]["detail"]["response"]["score"], 679)
        status, headers, text = self.call("GET", "/requests/%s/explanation" % rid)
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertIn("R3_MIN_SCORE", text)
        self.assertEqual(self.call("GET", "/requests/doesnotexist")[0], 404)
        self.assertEqual(self.call("GET", "/nothing/here")[0], 404)

        status, _, fail = self.call("POST", "/admin/dependencies/credit_bureau/fail", {"count": 3})
        self.assertEqual((status, fail), (200, {"service": "credit_bureau", "failing_next": 3}))
        status, _, pending = self.call("POST", "/requests", {"workflow": "loan_approval",
                                                             "payload": loan(applicant_id="cust009")})
        self.assertEqual((status, pending["status"]), (201, "PENDING_RETRY"))
        status, _, done = self.call("POST", "/requests/%s/retry" % pending["id"], {})
        self.assertEqual((status, done["status"]), (200, "APPROVED"))
        self.assertEqual(self.call("POST", "/requests/%s/retry" % pending["id"], {})[0], 409)
        self.assertEqual(self.call("POST", "/requests/%s/review" % rid,
                                   {"decision": "APPROVED", "reviewer": "priya"})[0], 409)

        status, _, flows = self.call("GET", "/workflows")
        self.assertEqual((status, {w["id"] for w in flows}), (200, {"loan_approval", "vendor_approval"}))
        broken = loan_config()
        broken["stages"]["approve"]["decision"] = "MAYBE"
        status, _, err = self.call("POST", "/workflows", broken)
        self.assertEqual(status, 400)
        self.assertTrue(err["details"])


if __name__ == "__main__":
    unittest.main()
