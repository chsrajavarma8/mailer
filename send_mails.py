"""Send personalised emails from recipients.csv using template.txt. Run with --help for options."""

import argparse
import configparser
import csv
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

import secure_store

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "sent_log.csv"
ATTACH_DIR = BASE_DIR / "attachments"
EMAIL_RE = re.compile(r"^[^\s@<>,;\"]+@[^\s@<>,;\"]+\.[^\s@<>,;\"]+$")


class SafeDict(dict):
    """Leave unknown {placeholders} untouched instead of raising KeyError."""

    def __missing__(self, key):
        return "{" + key + "}"


def load_config(path):
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(path, encoding="utf-8"):
        sys.exit(f"Config file not found: {path}")
    password = os.environ.get("MAIL_PASSWORD") or cfg.get("smtp", "password", fallback="")
    try:
        password = secure_store.decrypt(password.strip())
    except ValueError as e:
        sys.exit(str(e))
    return {
        "host": cfg.get("smtp", "host"),
        "port": cfg.getint("smtp", "port"),
        "username": cfg.get("smtp", "username"),
        "password": password.replace(" ", ""),
        "from_name": cfg.get("sender", "from_name", fallback=""),
        "from_email": cfg.get("sender", "from_email"),
        "delay": cfg.getfloat("options", "delay_seconds", fallback=2),
    }


def connect(cfg, timeout=30):
    ctx = ssl.create_default_context()
    if cfg["port"] == 465:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout, context=ctx)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout)
    try:
        if cfg["port"] != 465:
            server.starttls(context=ctx)
        server.login(cfg["username"], cfg["password"])
    except BaseException:
        server.close()
        raise
    return server


def load_template(path):
    text = Path(path).read_text(encoding="utf-8")
    first, _, body = text.partition("\n")
    if not first.lower().startswith("subject:"):
        sys.exit("Template must start with a 'Subject: ...' line.")
    return first.split(":", 1)[1].strip(), body.lstrip("\n")


def load_recipients(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]
    if not rows:
        sys.exit(f"No rows found in {path}")
    if "email" not in rows[0]:
        sys.exit("CSV must have an 'email' column.")
    return rows


def already_sent():
    if not LOG_FILE.exists():
        return set()
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        return {r["email"].lower() for r in csv.DictReader(f) if r["status"] == "SENT"}


def write_log(email, status, detail=""):
    new = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "email", "status", "detail"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), email, status, detail])


def resolve_attachment(item):
    """Map a CSV attachment entry to a file inside attachments/ (nothing outside it)."""
    root = ATTACH_DIR.resolve()
    path = Path(item)
    if not path.is_absolute():
        path = BASE_DIR / path
        if not path.exists() and len(Path(item).parts) == 1:
            path = ATTACH_DIR / item
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"attachment must be in the attachments folder: {item}")
    if not path.is_file():
        raise FileNotFoundError(f"attachment not found: {item}")
    return path


def build_message(cfg, row, subject_tpl, body_tpl):
    email = row.get("email", "").strip()
    if not EMAIL_RE.match(email):
        raise ValueError(f"invalid email address: {email!r}")
    values = SafeDict(row)
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"] = email
    msg["Subject"] = subject_tpl.format_map(values).replace("\r", " ").replace("\n", " ")
    msg.set_content(body_tpl.format_map(values))

    for item in filter(None, (p.strip() for p in row.get("attachment", "").split(";"))):
        path = resolve_attachment(item)
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


class Mailer:
    """An SMTP session that reconnects once if the server drops the connection."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.server = None

    def __enter__(self):
        self.server = connect(self.cfg)
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.server is not None:
            try:
                self.server.quit()
            except Exception:
                self.server.close()
            self.server = None

    def send(self, msg):
        try:
            if self.server is None:
                self.server = connect(self.cfg)
            self.server.send_message(msg)
        except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError):
            self.close()
            time.sleep(2)
            self.server = connect(self.cfg)
            self.server.send_message(msg)


def main():
    ap = argparse.ArgumentParser(description="Send personalised emails from a CSV file.")
    ap.add_argument("--csv", default=BASE_DIR / "recipients.csv")
    ap.add_argument("--template", default=BASE_DIR / "template.txt")
    ap.add_argument("--config", default=BASE_DIR / "config.ini")
    ap.add_argument("--send", action="store_true", help="actually send (default is a dry-run preview)")
    ap.add_argument("--resend", action="store_true", help="also send to addresses already logged as SENT")
    args = ap.parse_args()

    cfg = load_config(args.config)
    subject_tpl, body_tpl = load_template(args.template)
    rows = load_recipients(args.csv)
    skip = set() if args.resend else already_sent()

    if not args.send:
        print("DRY RUN - nothing will be sent. Add --send to send for real.\n")
        for row in rows:
            tag = " (already sent, will skip)" if row["email"].lower() in skip else ""
            try:
                msg = build_message(cfg, row, subject_tpl, body_tpl)
            except Exception as e:
                print(f"!! {row['email']}: {e}\n")
                continue
            print(f"--- To: {msg['To']}{tag}\nSubject: {msg['Subject']}")
            print(msg.get_body().get_content().rstrip())
            names = [p.get_filename() for p in msg.iter_attachments()]
            if names:
                print(f"[attachments: {', '.join(names)}]")
            print()
        print(f"{len(rows)} recipient(s) loaded.")
        return

    if not cfg["password"]:
        sys.exit("No password: set it in config.ini or the MAIL_PASSWORD environment variable.")

    sent = failed = skipped = 0
    done = set()
    with Mailer(cfg) as mailer:
        print(f"Logged in as {cfg['username']}\n")

        for i, row in enumerate(rows, 1):
            email = row["email"]
            if not email:
                continue
            if email.lower() in skip or email.lower() in done:
                print(f"[{i}/{len(rows)}] SKIP   {email} (already sent)")
                skipped += 1
                continue
            try:
                mailer.send(build_message(cfg, row, subject_tpl, body_tpl))
                write_log(email, "SENT", "resend" if args.resend else "")
                done.add(email.lower())
                print(f"[{i}/{len(rows)}] SENT   {email}")
                sent += 1
            except Exception as e:
                write_log(email, "FAILED", str(e))
                print(f"[{i}/{len(rows)}] FAILED {email}: {e}")
                failed += 1
            time.sleep(cfg["delay"])

    print(f"\nDone. Sent: {sent}  Failed: {failed}  Skipped: {skipped}  (log: {LOG_FILE.name})")


if __name__ == "__main__":
    main()
