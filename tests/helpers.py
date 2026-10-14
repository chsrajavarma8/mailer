"""Shared test fixtures: a throwaway project folder and a fake SMTP server (nothing is ever sent)."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
import send_mails as sm

CONFIG = """[smtp]
host = smtp.example.test
port = 587
username = sender@example.org
password = abcdabcdabcdabcd

[sender]
from_name = Test Sender
from_email = sender@example.org

[options]
delay_seconds = 0
max_retries = 2
retry_delay_seconds = 0
daily_limit = 0
"""


class FakeSMTP:
    """Records messages. `script` is a list of exceptions (or None) raised by successive sends."""

    def __init__(self, script=None):
        self.sent = []
        self.script = list(script or [])
        self.connects = 0

    def send_message(self, msg):
        if self.script:
            exc = self.script.pop(0)
            if exc is not None:
                raise exc
        self.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


class Isolated(unittest.TestCase):
    """Points every file path in send_mails and app at a temporary folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mailer-test-"))
        (self.tmp / "attachments").mkdir()
        (self.tmp / "config.ini").write_text(CONFIG, encoding="utf-8")
        patches = {
            sm: {"BASE_DIR": self.tmp, "LOG_FILE": self.tmp / "sent_log.csv",
                 "ATTACH_DIR": self.tmp / "attachments", "CAMPAIGN_DIR": self.tmp / "campaigns"},
            app: {"BASE_DIR": self.tmp, "CSV_FILE": self.tmp / "recipients.csv",
                  "TEMPLATE_FILE": self.tmp / "template.txt", "TEMPLATE_HTML": self.tmp / "template.html",
                  "CONFIG_FILE": self.tmp / "config.ini", "ATTACH_DIR": self.tmp / "attachments",
                  "BACKUP_DIR": self.tmp / "backups", "SAVED_DIR": self.tmp / "saved_templates",
                  "AUDIT_FILE": self.tmp / "audit_log.csv"},
        }
        for module, attrs in patches.items():
            for name, value in attrs.items():
                p = mock.patch.object(module, name, value)
                p.start()
                self.addCleanup(p.stop)
        data_files = {"recipients": self.tmp / "recipients.csv", "template": self.tmp / "template.txt",
                      "template_html": self.tmp / "template.html", "history": self.tmp / "sent_log.csv"}
        p = mock.patch.object(app, "DATA_FILES", data_files)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = sm.load_config(self.tmp / "config.ini")

    def fake_smtp(self, script=None):
        server = FakeSMTP(script)

        def connect(cfg, timeout=30):
            server.connects += 1
            return server

        p = mock.patch.object(sm, "connect", connect)
        p.start()
        self.addCleanup(p.stop)
        return server
