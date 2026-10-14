import csv
import io
import smtplib
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta

import send_mails as sm
from tests.helpers import Isolated

ROWS = [{"email": "a@example.com", "name": "Asha"},
        {"email": "b@example.com", "name": "Ben"},
        {"email": "A@Example.com", "name": "Asha again"}]


class FillTests(unittest.TestCase):
    def test_both_placeholder_styles(self):
        row = {"name": "Asha", "First Name": "A"}
        self.assertEqual(sm.fill("Hi {{name}} / {name} / {{ First Name }}", row), "Hi Asha / Asha / A")

    def test_case_insensitive_and_unknown_kept(self):
        self.assertEqual(sm.fill("{{NAME}} {{missing}} {other}", {"name": "x"}), "x {{missing}} {other}")

    def test_stray_braces_are_harmless(self):
        self.assertEqual(sm.fill("a { b } {{ c", {"b": "1"}), "a { b } {{ c")

    def test_html_escape(self):
        self.assertEqual(sm.fill("<b>{{n}}</b>", {"n": "<script>"}, escape=sm.html.escape), "<b>&lt;script&gt;</b>")

    def test_missing_fields(self):
        self.assertEqual(sm.missing_fields({"Name": "x"}, "{{name}} {{slot}}", "{city}"), ["city", "slot"])


class ParseTests(unittest.TestCase):
    def test_semicolon_and_email_alias(self):
        cols, rows, info = sm.parse_recipients("﻿Name;E-mail\nAsha;a@example.com\n")
        self.assertEqual(info["delimiter"], ";")
        self.assertEqual(info["email_column"], "E-mail")
        self.assertEqual(cols, ["Name", "email"])
        self.assertEqual(rows, [{"Name": "Asha", "email": "a@example.com"}])

    def test_email_address_header_and_blank_lines(self):
        _, rows, info = sm.parse_recipients("Email Address,name\n\nx@example.com,X\n,\n")
        self.assertEqual(info["email_column"], "Email Address")
        self.assertEqual(len(rows), 1)

    def test_detect_by_values(self):
        _, rows, info = sm.parse_recipients("who,contact\nA,a@example.com\nB,b@example.com\n")
        self.assertEqual(info["email_column"], "contact")
        self.assertEqual(rows[1]["email"], "b@example.com")

    def test_explicit_column_and_existing_email_column(self):
        cols, rows, _ = sm.parse_recipients("email,work\nold@example.com,new@example.com\n", "Work")
        self.assertEqual(cols, ["email_original", "email"])
        self.assertEqual(rows[0]["email"], "new@example.com")

    def test_no_email_column(self):
        with self.assertRaises(ValueError):
            sm.parse_recipients("a,b\n1,2\n")

    def test_duplicate_headers_are_renamed(self):
        cols, _, _ = sm.parse_recipients("email,name,Name\nx@example.com,a,b\n")
        self.assertEqual(cols, ["email", "name", "Name_2"])

    def test_analyze(self):
        rows = ROWS + [{"email": ""}, {"email": "not-an-email"}]
        a = sm.analyze(rows)
        self.assertEqual(a["valid"], 2)
        self.assertEqual(a["duplicates"], [{"row": 3, "email": "A@Example.com", "first": 1}])
        self.assertEqual(a["blank"], [4])
        self.assertEqual(a["invalid"], [{"row": 5, "email": "not-an-email"}])


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(sm.classify(smtplib.SMTPAuthenticationError(535, b"bad")), "auth")
        self.assertEqual(sm.classify(smtplib.SMTPDataError(421, b"try later")), "temporary")
        self.assertEqual(sm.classify(smtplib.SMTPDataError(550, b"no such user")), "permanent")
        self.assertEqual(sm.classify(smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded")), "quota")
        self.assertEqual(sm.classify(smtplib.SMTPServerDisconnected("gone")), "temporary")
        self.assertEqual(sm.classify(smtplib.SMTPRecipientsRefused({"a@x.com": (450, b"busy")})), "temporary")
        self.assertEqual(sm.classify(smtplib.SMTPRecipientsRefused({"a@x.com": (550, b"unknown")})), "permanent")
        self.assertEqual(sm.classify(ValueError("invalid email")), "permanent")
        self.assertEqual(sm.classify(FileNotFoundError("attachment")), "permanent")

    def test_csv_safe(self):
        self.assertEqual(sm.csv_safe("=1+1"), "'=1+1")
        self.assertEqual(sm.csv_safe("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(sm.csv_safe("a@example.com"), "a@example.com")


class MessageTests(Isolated):
    def test_plain_and_html(self):
        self.cfg["reply_to"] = "replies@example.org"
        msg = sm.build_message(self.cfg, {"email": "a@example.com", "name": "<A>"}, "Hi {{name}}",
                               "Hello {{name}}", "<p>Hello {{name}}</p>")
        self.assertEqual(msg["Subject"], "Hi <A>")
        self.assertEqual(msg["Reply-To"], "replies@example.org")
        self.assertIn("Hello <A>", msg.get_body(("plain",)).get_content())
        self.assertIn("<p>Hello &lt;A&gt;</p>", msg.get_body(("html",)).get_content())
        self.assertTrue(msg["Message-ID"].endswith("@example.org>"))

    def test_subject_header_injection(self):
        msg = sm.build_message(self.cfg, {"email": "a@example.com", "x": "a\r\nBcc: evil@x.com"}, "{{x}}", "b")
        self.assertNotIn("\n", msg["Subject"])
        self.assertIsNone(msg["Bcc"])

    def test_attachments_confined(self):
        (self.tmp / "attachments" / "a.txt").write_text("hi")
        msg = sm.build_message(self.cfg, {"email": "a@example.com", "attachment": "a.txt"}, "s", "b")
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["a.txt"])
        (self.tmp / "secret.txt").write_text("x")
        with self.assertRaises(ValueError):
            sm.build_message(self.cfg, {"email": "a@example.com", "attachment": "../secret.txt"}, "s", "b")
        with self.assertRaises(ValueError):
            sm.build_message(self.cfg, {"email": "bad"}, "s", "b")


class MailerTests(Isolated):
    def test_retries_temporary_then_succeeds(self):
        server = self.fake_smtp([smtplib.SMTPDataError(421, b"later"), smtplib.SMTPServerDisconnected("x"), None])
        retries = []
        attempts = sm.Mailer(self.cfg).send("msg", on_retry=lambda a, w, e: retries.append(a))
        self.assertEqual(attempts, 3)
        self.assertEqual(retries, [1, 2])
        self.assertEqual(server.sent, ["msg"])

    def test_permanent_not_retried(self):
        server = self.fake_smtp([smtplib.SMTPDataError(550, b"no such user")])
        with self.assertRaises(smtplib.SMTPDataError):
            sm.Mailer(self.cfg).send("msg")
        self.assertEqual(server.connects, 1)

    def test_gives_up_after_max_retries(self):
        self.fake_smtp([smtplib.SMTPDataError(451, b"later")] * 5)
        with self.assertRaises(smtplib.SMTPDataError):
            sm.Mailer(self.cfg).send("msg")

    def test_retry_delay_grows(self):
        self.cfg["retry_delay"] = 2
        self.fake_smtp([smtplib.SMTPDataError(421, b"x"), smtplib.SMTPDataError(421, b"x"), None])
        waits = []
        sm.Mailer(self.cfg, sleep=waits.append).send("msg")
        self.assertEqual(waits, [2, 4])

    def test_quota(self):
        self.fake_smtp([smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded")])
        with self.assertRaises(sm.QuotaExceeded):
            sm.Mailer(self.cfg).send("msg")


class CampaignTests(Isolated):
    def run_rows(self, rows=ROWS, **kw):
        events = []
        result = sm.run_campaign(self.cfg, rows, "Hi {{name}}", "Body {{name}}",
                                 on_event=lambda *e: events.append(e), **kw)
        return result, events

    def test_sends_dedupes_and_logs(self):
        server = self.fake_smtp()
        result, events = self.run_rows()
        self.assertEqual((result["status"], result["sent"], result["skipped"]), ("completed", 2, 1))
        self.assertEqual([m["To"] for m in server.sent], ["a@example.com", "b@example.com"])
        log = sm.read_log()
        self.assertEqual({r["campaign"] for r in log}, {result["campaign"]})
        c = sm.list_campaigns()[0]
        self.assertEqual((c["status"], c["sent"], c["skipped"]), ("completed", 2, 1))
        snap = sm.load_campaign(result["campaign"])
        self.assertEqual(snap["subject"], "Hi {{name}}")
        self.assertEqual(len(snap["rows"]), 3)

    def test_second_run_skips_already_sent(self):
        server = self.fake_smtp()
        self.run_rows()
        result, _ = self.run_rows()
        self.assertEqual((result["sent"], result["skipped"]), (0, 3))
        self.assertEqual(len(server.sent), 2)

    def test_resend(self):
        self.fake_smtp()
        self.run_rows()
        result, _ = self.run_rows(resend=frozenset({"a@example.com"}))
        self.assertEqual(result["sent"], 1)
        self.assertEqual(sm.read_log()[-1]["detail"], "resend")

    def test_permanent_failure_is_logged_and_run_continues(self):
        server = self.fake_smtp([smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"unknown user")})])
        result, _ = self.run_rows(ROWS[:2])
        self.assertEqual((result["sent"], result["failed"]), (1, 1))
        self.assertEqual(server.connects, 1)  # permanent errors are not retried
        self.assertIn("550", sm.read_log()[0]["detail"])

    def test_quota_stops_and_resume_continues(self):
        self.fake_smtp([None, smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota exceeded")])
        result, _ = self.run_rows()
        self.assertEqual(result["status"], "stopped")
        self.assertIn("daily sending limit", result["error"])
        self.fake_smtp()
        again, _ = self.run_rows(campaign=result["campaign"])
        self.assertEqual((again["status"], again["sent"]), ("completed", 1))
        self.assertEqual(sm.list_campaigns()[0]["sent"], 2)

    def test_limit_and_daily_limit(self):
        self.fake_smtp()
        result, _ = self.run_rows(limit=1)
        self.assertEqual((result["sent"], result["status"]), (1, "stopped"))
        self.cfg["daily_limit"] = 1
        result, _ = self.run_rows()
        self.assertEqual(result["sent"], 0)
        self.assertIn("Daily limit", result["error"])

    def test_auth_failure(self):
        self.fake_smtp([smtplib.SMTPAuthenticationError(535, b"bad")])
        result, _ = self.run_rows()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], sm.AUTH_HELP)

    def test_stop_flag(self):
        self.fake_smtp()
        stop = threading.Event()
        stop.set()
        result, _ = self.run_rows(stop=stop)
        self.assertEqual((result["status"], result["sent"]), ("stopped", 0))

    def test_crash_marks_interrupted(self):
        cid = sm.new_campaign("s", "b", "", ["email"], ROWS, "cli")
        sm.update_campaign(cid, pid=999999)
        self.assertEqual(sm.list_campaigns()[0]["status"], "interrupted")
        with self.assertRaises(ValueError):
            sm.campaign_path("../../etc")

    def test_sent_since_and_old_log_upgrade(self):
        old = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
        with open(sm.LOG_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["timestamp", "email", "status", "detail"], [old, "x@example.com", "SENT", ""]])
        sm.write_log("y@example.com", "SENT", "", "c1")
        self.assertEqual(sm.LOG_FILE.read_text(encoding="utf-8").splitlines()[0], ",".join(sm.LOG_FIELDS))
        self.assertEqual(sm.already_sent(), {"x@example.com", "y@example.com"})
        self.assertEqual(sm.sent_since(24), 1)


class CliTests(Isolated):
    def setUp(self):
        super().setUp()
        (self.tmp / "template.txt").write_text("Subject: Hi {{name}}\n\nHello {{name}} {{slot}}", encoding="utf-8")
        (self.tmp / "recipients.csv").write_text("Name;E-mail\nAsha;a@example.com\nBad;nope\n", encoding="utf-8")

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = sm.main(["--config", str(self.tmp / "config.ini"), "--csv", str(self.tmp / "recipients.csv"),
                            "--template", str(self.tmp / "template.txt"),
                            "--html", str(self.tmp / "template.html"), *args])
        return code, out.getvalue()

    def test_dry_run(self):
        server = self.fake_smtp()
        code, out = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("Subject: Hi Asha", out)
        self.assertIn("no column for slot", out)
        self.assertIn("invalid address 'nope'", out)
        self.assertEqual(server.sent, [])

    def test_send_yes(self):
        server = self.fake_smtp()
        code, out = self.cli("--send", "--yes")
        self.assertEqual(len(server.sent), 1)
        self.assertIn("Sent: 1", out)
        self.assertEqual(code, 0)

    def test_check(self):
        code, out = self.cli("--check")
        self.assertIn("Recipients: 1 valid, 1 invalid", out)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
