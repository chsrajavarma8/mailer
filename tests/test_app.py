import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import app
import send_mails as sm
from tests.helpers import Isolated


class DashboardTests(Isolated):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cookie = ""
        self.csrf = ""
        status, body, headers = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = body["csrf"]

    def request(self, method, path, data=None, host="127.0.0.1", ctype="application/json", raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"{host}:{self.port}"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        body = None
        if method == "POST":
            headers.update({"Content-Type": ctype, "X-CSRF-Token": self.csrf})
            body = data if isinstance(data, bytes) else json.dumps(data or {}).encode()
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        payload = r.read()
        hdrs = dict(r.getheaders())
        conn.close()
        if raw:
            return r.status, payload, hdrs
        return r.status, json.loads(payload or b"{}"), hdrs

    def test_security_headers_and_host_check(self):
        status, _, headers = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.request("GET", "/api/state", host="evil.example")[0], 403)

    def test_csrf_and_content_type_required(self):
        self.csrf = "wrong"
        self.assertEqual(self.request("POST", "/api/template", {"subject": "x"})[0], 403)
        self.assertEqual(self.request("POST", "/api/template", b"subject=x", ctype="text/plain")[0], 403)

    def test_template_html_and_preview(self):
        status, _, _ = self.request("POST", "/api/template", {"subject": "Hi {{name}}", "body": "B", "html": "<b>{{name}}</b>"})
        self.assertEqual(status, 200)
        self.assertEqual(app.read_template(), ("Hi {{name}}", "B", "<b>{{name}}</b>"))
        _, r, _ = self.request("POST", "/api/preview", {"subject": "Hi {{name}}", "body": "{{slot}}", "html": "<i>{{name}}</i>",
                                                        "rows": [{"email": "a@example.com", "name": "<A>"}]})
        it = r["items"][0]
        self.assertEqual(it["subject"], "Hi <A>")
        self.assertIn("<i>&lt;A&gt;</i>", it["html"])
        self.assertEqual(it["missing"], ["slot"])
        self.request("POST", "/api/template", {"subject": "s", "body": "b", "html": ""})
        self.assertFalse(app.TEMPLATE_HTML.exists())

    def test_parse_csv(self):
        status, r, _ = self.request("POST", "/api/parse-csv", "Name;Email Address\nA;a@x.com\nB;a@x.com\n".encode(),
                                    ctype="application/octet-stream")
        self.assertEqual(status, 200)
        self.assertEqual(r["columns"], ["Name", "email"])
        self.assertEqual(len(r["report"]["duplicates"]), 1)
        status, r, _ = self.request("POST", "/api/parse-csv", b"a,b\n1,2\n", ctype="application/octet-stream")
        self.assertEqual(status, 400)

    def test_saved_templates(self):
        status, r, _ = self.request("POST", "/api/save-template-as", {"name": "Welcome", "subject": "S", "body": "B"})
        self.assertEqual(status, 200)
        self.assertEqual([t["name"] for t in r["templates"]], ["Welcome"])
        self.assertEqual(self.request("POST", "/api/save-template-as", {"name": "Welcome", "subject": "x"})[0], 400)
        for bad in ("../evil", "CON", "", "a/b"):
            self.assertEqual(self.request("POST", "/api/save-template-as", {"name": bad})[0], 400, bad)
        _, r, _ = self.request("POST", "/api/delete-template", {"name": "Welcome"})
        self.assertEqual(r["templates"], [])

    def test_config_validation(self):
        base = {"host": "smtp.example.test", "port": 587, "username": "u@example.org", "from_name": "n",
                "from_email": "u@example.org", "delay": 1, "max_retries": 4, "retry_delay": 3, "daily_limit": 2000}
        status, r, _ = self.request("POST", "/api/config", base)
        self.assertEqual(status, 200)
        self.assertEqual((r["config"]["daily_limit"], r["config"]["max_retries"]), (2000, 4))
        self.assertNotIn("password", r["config"])
        self.assertEqual(self.request("POST", "/api/config", {**base, "reply_to": "nope"})[0], 400)
        self.assertEqual(self.request("POST", "/api/config", {**base, "max_retries": 50})[0], 400)

    def test_send_campaign_resume_and_export(self):
        server = self.fake_smtp([None, smtplib_quota()])
        rows = [{"email": "a@example.com", "name": "=HYPERLINK(1)"}, {"email": "b@example.com", "name": "B"},
                {"email": "c@example.com", "name": "C"}]
        status, r, _ = self.request("POST", "/api/send", {"subject": "Hi", "body": "{{name}}", "columns": ["email", "name"], "rows": rows})
        self.assertEqual(status, 200, r)
        cid = r["campaign"]
        self.wait_for_job()
        self.assertEqual(app.job.snapshot()["sent"], 1)
        self.assertIn("daily sending limit", app.job.snapshot()["error"])
        self.assertEqual(sm.list_campaigns()[0]["status"], "stopped")

        server.script = []
        status, r, _ = self.request("POST", "/api/resume", {"id": cid})
        self.assertEqual(status, 200, r)
        self.wait_for_job()
        self.assertEqual(len(server.sent), 3)
        self.assertEqual(sm.list_campaigns()[0]["sent"], 3)

        status, data, headers = self.request("GET", f"/api/campaign.csv?id={cid}", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.request("GET", "/api/campaign.csv?id=..%2F..%2Fx")[0], 400)
        self.assertEqual(self.request("POST", "/api/resume", {"id": "../../x"})[0], 400)

    def test_log_export_is_formula_safe(self):
        sm.write_log("=cmd|' /C calc'!A0", "FAILED", "@SUM(1)")
        status, data, _ = self.request("GET", "/api/log.csv", raw=True)
        text = data.decode("utf-8-sig")
        self.assertIn("'=cmd", text)
        self.assertIn("'@SUM(1)", text)

    def test_health(self):
        _, r, _ = self.request("GET", "/api/health")
        names = {c["name"] for c in r["checks"]}
        self.assertTrue({"Settings file", "App password", "Folder writable"} <= names)

    def wait_for_job(self):
        for _ in range(200):
            if not app.job.snapshot()["running"]:
                return
            time.sleep(0.02)
        self.fail("send job did not finish")


def smtplib_quota():
    import smtplib
    return smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded")


if __name__ == "__main__":
    unittest.main()
