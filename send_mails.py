"""Send personalised emails from recipients.csv using template.txt (and optional template.html).

Run with --help for options. Without --send nothing is sent (dry run).
"""

import argparse
import configparser
import csv
import html
import io
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import secure_store

if getattr(sys, "frozen", False):  # packaged .exe: keep data next to the executable
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

LOG_FILE = BASE_DIR / "sent_log.csv"
ATTACH_DIR = BASE_DIR / "attachments"
CAMPAIGN_DIR = BASE_DIR / "campaigns"
EMAIL_RE = re.compile(r"^[^\s@<>,;\"]+@[^\s@<>,;\"]+\.[^\s@<>,;\"]+$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}|\{(\w+)\}")
QUOTA_RE = re.compile(r"5\.4\.5|sending (limit|quota) exceeded|daily (user )?(sending )?(limit|quota)", re.I)
CAMPAIGN_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")

LOG_FIELDS = ["timestamp", "email", "status", "detail", "campaign"]
CAMPAIGN_FIELDS = ["id", "started", "finished", "source", "subject", "total",
                   "sent", "failed", "skipped", "status", "error", "pid"]
EMAIL_HEADERS = {"email", "emails", "emailaddress", "emailid", "mail", "mailid", "mailaddress"}
AUTH_HELP = "Login failed. Gmail needs a 16-character App Password, not your normal password."

_log_lock = threading.Lock()
_campaign_lock = threading.Lock()


class QuotaExceeded(Exception):
    """The mail server says the account has hit its sending limit."""


class ConnectionFailed(Exception):
    """Could not connect or log in to the mail server, even after retrying."""


# --------------------------------------------------------------------------- config

def load_config(path):
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(path, encoding="utf-8"):
        sys.exit(f"Config file not found: {path}")
    password = os.environ.get("MAIL_PASSWORD") or cfg.get("smtp", "password", fallback="")
    try:
        password = secure_store.decrypt(password.strip())
    except ValueError as e:
        sys.exit(str(e))
    try:
        return {
            "host": cfg.get("smtp", "host"),
            "port": cfg.getint("smtp", "port"),
            "username": cfg.get("smtp", "username"),
            "password": password.replace(" ", ""),
            "from_name": cfg.get("sender", "from_name", fallback=""),
            "from_email": cfg.get("sender", "from_email"),
            "reply_to": cfg.get("sender", "reply_to", fallback="").strip(),
            "delay": cfg.getfloat("options", "delay_seconds", fallback=2),
            "max_retries": max(0, cfg.getint("options", "max_retries", fallback=3)),
            "retry_delay": max(0.0, cfg.getfloat("options", "retry_delay_seconds", fallback=5)),
            "daily_limit": max(0, cfg.getint("options", "daily_limit", fallback=500)),
        }
    except (configparser.Error, ValueError) as e:
        sys.exit(f"Problem in {Path(path).name}: {e}")


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


# --------------------------------------------------------------------------- templates

def load_template(path):
    text = read_text(path)
    first, _, body = text.partition("\n")
    if not first.lower().startswith("subject:"):
        sys.exit("Template must start with a 'Subject: ...' line.")
    return first.split(":", 1)[1].strip(), body.lstrip("\n")


def load_html(path):
    path = Path(path)
    return read_text(path) if path.is_file() else ""


def placeholders(text):
    """Names used as {name} or {{name}} in a template."""
    return {(m.group(1) or m.group(2)).strip() for m in PLACEHOLDER_RE.finditer(text or "")}


def missing_fields(row, *texts):
    keys = {k.lower() for k in row}
    return sorted({p for t in texts for p in placeholders(t) if p.lower() not in keys})


def fill(text, row, escape=None):
    """Replace {name} and {{name}} with the row's value (case-insensitive). Unknown names stay as-is."""
    lower = {str(k).lower(): v for k, v in row.items()}

    def sub(m):
        key = (m.group(1) or m.group(2)).strip()
        if key in row:
            value = row[key]
        elif key.lower() in lower:
            value = lower[key.lower()]
        else:
            return m.group(0)
        value = "" if value is None else str(value)
        return escape(value) if escape else value

    return PLACEHOLDER_RE.sub(sub, text or "")


# --------------------------------------------------------------------------- recipients

def read_text(path):
    """Read a text file saved by Excel/Notepad: UTF-8 (with or without BOM), else Windows-1252."""
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return text.replace("\r\n", "\n")


def sniff_delimiter(text):
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def find_email_column(columns, rows=(), preferred=None):
    """Pick the email column: an explicit name, a known header, or the column full of addresses."""
    if preferred:
        for c in columns:
            if c.lower() == preferred.strip().lower():
                return c
        raise ValueError(f"No column named {preferred!r}. Columns: {', '.join(columns)}")
    norm = {c: re.sub(r"[^a-z]", "", c.lower()) for c in columns}
    for c in columns:
        if norm[c] in EMAIL_HEADERS:
            return c
    for c in columns:
        if "email" in norm[c] or "mail" in norm[c]:
            return c
    sample = list(rows)[:50]
    best, score = None, 0
    for c in columns:
        hits = sum(1 for r in sample if EMAIL_RE.match(str(r.get(c, "")).strip()))
        if hits > score:
            best, score = c, hits
    if best and score >= max(1, len(sample) // 2):
        return best
    return None


def parse_recipients(text, email_column=None):
    """Parse CSV text into (columns, rows, info). The email column is renamed to 'email'."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise ValueError("The file is empty.")
    delimiter = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, [])
    columns, seen = [], set()
    for i, c in enumerate(header):
        c = re.sub(r"[{}]", "", (c or "").strip()) or f"column{i + 1}"
        base, n = c, 2
        while c.lower() in seen:
            c, n = f"{base}_{n}", n + 1
        seen.add(c.lower())
        columns.append(c)
    rows = []
    for values in reader:
        if not any((v or "").strip() for v in values):
            continue
        rows.append({c: (values[i] if i < len(values) else "").strip() for i, c in enumerate(columns)})
    found = find_email_column(columns, rows, email_column)
    if not found:
        raise ValueError("Couldn't find an email column. Name it 'email' or choose it with --email-column.")
    if found != "email":
        if any(c.lower() == "email" for c in columns):
            other = next(c for c in columns if c.lower() == "email")
            columns[columns.index(other)] = other + "_original"
            for r in rows:
                r[other + "_original"] = r.pop(other)
        columns[columns.index(found)] = "email"
        for r in rows:
            r["email"] = r.pop(found)
    rows = [{c: r.get(c, "") for c in columns} for r in rows]
    return columns, rows, {"delimiter": delimiter, "email_column": found}


def load_recipients(path, email_column=None):
    try:
        _, rows, _ = parse_recipients(read_text(path), email_column)
    except FileNotFoundError:
        sys.exit(f"Recipient file not found: {path}")
    except ValueError as e:
        sys.exit(f"{Path(path).name}: {e}")
    if not rows:
        sys.exit(f"No rows found in {path}")
    return rows


def analyze(rows):
    """Count valid, invalid, blank and duplicate (case-insensitive) addresses. Row numbers are 1-based."""
    seen, out = {}, {"total": len(rows), "valid": 0, "invalid": [], "blank": [], "duplicates": []}
    for i, r in enumerate(rows, 1):
        email = str(r.get("email", "")).strip()
        if not email:
            out["blank"].append(i)
        elif not EMAIL_RE.match(email):
            out["invalid"].append({"row": i, "email": email})
        elif email.lower() in seen:
            out["duplicates"].append({"row": i, "email": email, "first": seen[email.lower()]})
        else:
            seen[email.lower()] = i
            out["valid"] += 1
    return out


# --------------------------------------------------------------------------- logs

def read_log():
    if not LOG_FILE.exists():
        return []
    with _log_lock, open(LOG_FILE, newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "") for k in LOG_FIELDS} for r in csv.DictReader(f)]


def already_sent():
    return {r["email"].lower() for r in read_log() if r["status"] == "SENT"}


def sent_since(hours=24):
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    return sum(1 for r in read_log() if r["status"] == "SENT" and r["timestamp"] >= cutoff)


def _upgrade_log_header():
    """Older logs had no 'campaign' column; rewrite them once with the current header."""
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == LOG_FIELDS:
            return
        rows = list(reader)
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: r.get(k) or "" for k in LOG_FIELDS} for r in rows)


def write_log(email, status, detail="", campaign=""):
    with _log_lock:
        new = not LOG_FILE.exists()
        if not new:
            _upgrade_log_header()
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(LOG_FIELDS)
            w.writerow([datetime.now().isoformat(timespec="seconds"), email, status, detail, campaign])


def csv_safe(value):
    """Stop spreadsheet apps from running a cell as a formula when an export is opened."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def to_csv(fields, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for r in rows:
        w.writerow([csv_safe(r.get(k, "")) for k in fields])
    return buf.getvalue()


# --------------------------------------------------------------------------- campaigns

def campaign_path(cid):
    cid = str(cid or "")
    if not CAMPAIGN_ID_RE.match(cid):
        raise ValueError("Unknown campaign.")
    return CAMPAIGN_DIR / cid


def _write_index(rows):
    CAMPAIGN_DIR.mkdir(exist_ok=True)
    tmp = CAMPAIGN_DIR / "index.csv.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPAIGN_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, CAMPAIGN_DIR / "index.csv")


def _read_index():
    p = CAMPAIGN_DIR / "index.csv"
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "") for k in CAMPAIGN_FIELDS} for r in csv.DictReader(f)]


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))) and code.value == 259
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def list_campaigns():
    """Newest first. A 'running' campaign whose process has gone is reported as 'interrupted'."""
    with _campaign_lock:
        rows = _read_index()
    for r in rows:
        if r["status"] == "running" and not pid_alive(r["pid"]):
            r["status"] = "interrupted"
        for k in ("total", "sent", "failed", "skipped"):
            r[k] = int(r[k] or 0)
    return rows[::-1]


def new_campaign(subject, body, html_body, columns, rows, source):
    with _campaign_lock:
        CAMPAIGN_DIR.mkdir(exist_ok=True)
        cid = f"{datetime.now():%Y%m%d-%H%M%S}"
        n = 1
        while (CAMPAIGN_DIR / cid).exists():
            n += 1
            cid = f"{datetime.now():%Y%m%d-%H%M%S}-{n}"
        d = CAMPAIGN_DIR / cid
        d.mkdir()
        (d / "template.txt").write_text(f"Subject: {subject}\n\n{body}", encoding="utf-8")
        if html_body.strip():
            (d / "template.html").write_text(html_body, encoding="utf-8")
        with open(d / "recipients.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        index = _read_index()
        index.append({"id": cid, "started": datetime.now().isoformat(timespec="seconds"), "source": source,
                      "subject": subject, "total": len(rows), "sent": 0, "failed": 0, "skipped": 0,
                      "status": "running", "pid": os.getpid()})
        _write_index(index)
    return cid


def update_campaign(cid, **fields):
    with _campaign_lock:
        index = _read_index()
        for r in index:
            if r["id"] == cid:
                r.update({k: v for k, v in fields.items() if k in CAMPAIGN_FIELDS})
        _write_index(index)


def load_campaign(cid):
    d = campaign_path(cid)
    if not d.is_dir():
        raise ValueError("Campaign files not found.")
    subject, body = load_template(d / "template.txt")
    columns, rows, _ = parse_recipients(read_text(d / "recipients.csv"))
    return {"subject": subject, "body": body, "html": load_html(d / "template.html"),
            "columns": columns, "rows": rows}


def campaign_results(cid):
    return [r for r in read_log() if r["campaign"] == cid]


# --------------------------------------------------------------------------- sending

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


def build_message(cfg, row, subject_tpl, body_tpl, html_tpl=""):
    email = str(row.get("email", "")).strip()
    if not EMAIL_RE.match(email):
        raise ValueError(f"invalid email address: {email!r}")
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"] = email
    msg["Subject"] = fill(subject_tpl, row).replace("\r", " ").replace("\n", " ")
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    domain = (cfg["from_email"].rpartition("@")[2] or None) if cfg.get("from_email") else None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(fill(body_tpl, row))
    if (html_tpl or "").strip():
        msg.add_alternative(fill(html_tpl, row, escape=html.escape), subtype="html")

    for item in filter(None, (p.strip() for p in str(row.get("attachment", "")).split(";"))):
        path = resolve_attachment(item)
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


def describe(exc):
    if isinstance(exc, smtplib.SMTPRecipientsRefused) and exc.recipients:
        code, text = next(iter(exc.recipients.values()))
        return f"{code} {_text(text)}"
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"{exc.smtp_code} {_text(exc.smtp_error)}"
    return str(exc) or type(exc).__name__


def _text(v):
    return (v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)).replace("\n", " ").strip()


def classify(exc):
    """'auth', 'quota', 'temporary' (worth retrying) or 'permanent'."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth"
    if isinstance(exc, (ValueError, FileNotFoundError, IsADirectoryError, PermissionError)):
        return "permanent"
    if QUOTA_RE.search(describe(exc)):
        return "quota"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [c for c, _ in exc.recipients.values()] or [550]
        return "temporary" if all(400 <= c < 500 for c in codes) else "permanent"
    if isinstance(exc, smtplib.SMTPResponseException):
        return "temporary" if 400 <= exc.smtp_code < 500 else "permanent"
    if isinstance(exc, (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError, OSError)):
        return "temporary"
    return "permanent"


class Mailer:
    """An SMTP session that retries temporary failures with a growing delay.

    `sleep(seconds)` may return True to abandon the wait (used for the Stop button).
    """

    def __init__(self, cfg, sleep=None):
        self.cfg = cfg
        self.server = None
        self.sleep = sleep or time.sleep

    def __enter__(self):
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

    def send(self, msg, on_retry=None):
        attempts = self.cfg.get("max_retries", 3) + 1
        for attempt in range(1, attempts + 1):
            connecting = False
            try:
                if self.server is None:
                    connecting = True
                    self.server = connect(self.cfg)
                    connecting = False
                self.server.send_message(msg)
                return attempt
            except Exception as e:
                kind = classify(e)
                if kind == "quota":
                    raise QuotaExceeded(describe(e)) from e
                if kind != "temporary" or attempt == attempts:
                    if connecting and kind == "temporary":
                        raise ConnectionFailed(f"Could not connect to {self.cfg['host']}:{self.cfg['port']} "
                                               f"({describe(e)})") from e
                    raise
                self.close()
                wait = self.cfg.get("retry_delay", 5) * 2 ** (attempt - 1)
                if on_retry:
                    on_retry(attempt, wait, e)
                if self.sleep(wait):
                    raise


def run_campaign(cfg, rows, subject, body, html_body="", *, columns=None, resend=frozenset(),
                 skip_sent=True, limit=None, on_event=None, stop=None, source="cli", campaign=None):
    """Send to every row and log each result. Used by both the dashboard and the command line.

    on_event(i, total, email, status, detail) is called for SENT / FAILED / SKIP / RETRY.
    Returns a summary dict with the campaign id, counts, final status and any error.
    """
    stop = stop or threading.Event()
    emit = on_event or (lambda *a: None)
    columns = columns or list(dict.fromkeys(k for r in rows for k in r))
    if campaign:
        cid = campaign
        update_campaign(cid, status="running", pid=os.getpid(), finished="", error="")
    else:
        cid = new_campaign(subject, body, html_body, columns, rows, source)
    previous = next((c for c in _read_index() if c["id"] == cid), {})
    skip = (already_sent() - set(resend)) if skip_sent else set()
    budget = max(0, cfg["daily_limit"] - sent_since(24)) if cfg.get("daily_limit") else None
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    status, error, done, total = None, None, set(), len(rows)

    def log(i, email, status_, detail=""):
        if status_ in ("SENT", "FAILED"):
            write_log(email, status_, detail, cid)
        key = {"SENT": "sent", "FAILED": "failed", "SKIP": "skipped"}.get(status_)
        if key:
            counts[key] += 1
        emit(i, total, email, status_, detail)

    try:
        if not cfg["password"]:
            raise ValueError("No password set. Add an App Password in Settings or MAIL_PASSWORD.")
        with Mailer(cfg, sleep=stop.wait) as mailer:
            for i, row in enumerate(rows, 1):
                if stop.is_set():
                    status, error = "stopped", "Stopped by you."
                    break
                email = str(row.get("email", "")).strip()
                key = email.lower()
                if not email:
                    log(i, "(blank)", "SKIP", "no email address")
                    continue
                if key in skip:
                    log(i, email, "SKIP", "already sent")
                    continue
                if key in done:
                    log(i, email, "SKIP", "duplicate in list")
                    continue
                if limit is not None and counts["sent"] >= limit:
                    status, error = "stopped", f"Reached the limit of {limit} email(s) for this run."
                    break
                if budget is not None and counts["sent"] >= budget:
                    status = "stopped"
                    error = (f"Daily limit reached ({cfg['daily_limit']} in 24 hours). "
                             "Run this campaign again later to continue where it stopped.")
                    break
                try:
                    msg = build_message(cfg, row, subject, body, html_body)
                    mailer.send(msg, on_retry=lambda a, w, e, i=i, email=email: emit(
                        i, total, email, "RETRY", f"attempt {a} failed ({describe(e)}); retrying in {w:g}s"))
                    note = "resend" if key in resend else ""
                    done.add(key)
                    log(i, email, "SENT", note)
                except QuotaExceeded as e:
                    log(i, email, "FAILED", str(e))
                    status = "stopped"
                    error = (f"The mail server says the daily sending limit is reached ({e}). "
                             "Wait 24 hours, then resume this campaign.")
                    break
                except (smtplib.SMTPAuthenticationError, ConnectionFailed):
                    raise
                except Exception as e:
                    log(i, email, "FAILED", describe(e))
                if i < total:
                    stop.wait(cfg["delay"])
        status = status or "completed"
    except smtplib.SMTPAuthenticationError:
        status, error = "failed", AUTH_HELP
    except Exception as e:
        status, error = "failed", describe(e)
    finally:
        status = status or "interrupted"
        update_campaign(cid, status=status, error=error or "",
                        finished=datetime.now().isoformat(timespec="seconds"),
                        sent=int(previous.get("sent") or 0) + counts["sent"],
                        failed=counts["failed"], skipped=counts["skipped"])
    return {"campaign": cid, "status": status, "error": error, **counts}


# --------------------------------------------------------------------------- health

def health_checks(config_path, csv_path=None, template_path=None, html_path=None):
    """Return [{'level': ok|warn|err, 'name', 'detail'}] describing whether a send would work."""
    out = []

    def add(level, name, detail=""):
        out.append({"level": level, "name": name, "detail": detail})

    add("ok" if sys.version_info >= (3, 9) else "err", "Python", sys.version.split()[0])
    cfg = None
    if not Path(config_path).exists():
        add("err", "Settings file", f"{Path(config_path).name} not found")
    else:
        try:
            cfg = load_config(config_path)
            add("ok", "Settings file", Path(config_path).name)
        except SystemExit as e:
            add("err", "Settings file", str(e))
    if cfg:
        for key, label in (("host", "SMTP server"), ("username", "Login"), ("from_email", "From address")):
            ok = bool(cfg[key]) and "example.com" not in cfg[key]
            add("ok" if ok else "err", label, cfg[key] or "not set")
        if cfg["from_email"] and not EMAIL_RE.match(cfg["from_email"]):
            add("err", "From address", "doesn't look like an email address")
        pw = cfg["password"]
        add("ok" if len(pw) == 16 else ("warn" if pw else "err"), "App password",
            "set" if len(pw) == 16 else (f"set, but {len(pw)} characters (Gmail App Passwords have 16)" if pw else "not set"))
        if cfg["daily_limit"]:
            used = sent_since(24)
            left = cfg["daily_limit"] - used
            add("ok" if left > 0 else "warn", "Daily limit", f"{used} sent in the last 24 h, {max(0, left)} left of {cfg['daily_limit']}")
        if cfg["delay"] < 1:
            add("warn", "Delay", f"{cfg['delay']}s between emails is fast; Gmail may throttle you")
    if template_path:
        try:
            subject, body = load_template(template_path)
            add("ok" if subject else "warn", "Template", f"subject: {subject}" if subject else "subject is empty")
        except (SystemExit, OSError) as e:
            add("err", "Template", str(e) or "template.txt missing")
    if html_path and Path(html_path).is_file():
        add("ok", "HTML template", Path(html_path).name)
    if csv_path:
        try:
            _, rows, info = parse_recipients(read_text(csv_path))
            a = analyze(rows)
            level = "ok" if a["valid"] and not a["invalid"] else ("warn" if a["valid"] else "err")
            add(level, "Recipients", f"{a['valid']} valid, {len(a['invalid'])} invalid, "
                f"{len(a['duplicates'])} duplicate, {len(a['blank'])} blank (email column: {info['email_column']})")
            missing = set()
            for r in rows:
                for item in filter(None, (p.strip() for p in r.get("attachment", "").split(";"))):
                    try:
                        resolve_attachment(item)
                    except (ValueError, OSError):
                        missing.add(item)
            if missing:
                add("err", "Attachments", "missing: " + ", ".join(sorted(missing)[:5]))
        except FileNotFoundError:
            add("warn", "Recipients", "recipients.csv not found")
        except ValueError as e:
            add("err", "Recipients", str(e))
    try:
        probe = BASE_DIR / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        add("ok", "Folder writable", str(BASE_DIR))
    except OSError as e:
        add("err", "Folder writable", str(e))
    return out


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="Send personalised emails from a CSV file. "
                                             "Without --send this is a dry run that sends nothing.")
    ap.add_argument("--csv", default=BASE_DIR / "recipients.csv", help="recipient list (default recipients.csv)")
    ap.add_argument("--template", default=BASE_DIR / "template.txt", help="plain-text template")
    ap.add_argument("--html", default=BASE_DIR / "template.html", help="optional HTML template")
    ap.add_argument("--config", default=BASE_DIR / "config.ini")
    ap.add_argument("--email-column", help="name of the email column (default: detected)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print every email, send nothing (default)")
    mode.add_argument("--send", action="store_true", help="actually send")
    mode.add_argument("--check", action="store_true", help="run the health check and exit")
    ap.add_argument("--limit", type=int, help="send at most N emails this run")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation before sending")
    ap.add_argument("--no-resume", "--resend", dest="no_resume", action="store_true",
                    help="also send to addresses already logged as SENT")
    ap.add_argument("--resume", metavar="CAMPAIGN_ID",
                    help="continue an earlier campaign using its saved list and template")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # emoji in subjects on a legacy Windows console

    if args.check:
        icons = {"ok": "OK  ", "warn": "WARN", "err": "FAIL"}
        results = health_checks(args.config, args.csv, args.template, args.html)
        for r in results:
            print(f"[{icons[r['level']]}] {r['name']}: {r['detail']}")
        return 1 if any(r["level"] == "err" for r in results) else 0

    cfg = load_config(args.config)
    if args.resume:
        try:
            c = load_campaign(args.resume)
        except ValueError as e:
            sys.exit(str(e))
        subject, body, html_body, rows = c["subject"], c["body"], c["html"], c["rows"]
    else:
        subject, body = load_template(args.template)
        html_body = load_html(args.html)
        rows = load_recipients(args.csv, args.email_column)
    skip = set() if args.no_resume else already_sent()
    report = analyze(rows)
    pending = len({r["email"].strip().lower() for r in rows if EMAIL_RE.match(r["email"].strip())} - skip)
    if args.limit is not None:
        pending = min(pending, args.limit)

    print(f"{report['total']} row(s): {report['valid']} valid, {len(report['invalid'])} invalid, "
          f"{len(report['duplicates'])} duplicate, {len(report['blank'])} blank.")
    for bad in report["invalid"]:
        print(f"  row {bad['row']}: invalid address {bad['email']!r}")

    if not args.send:
        print("DRY RUN - nothing will be sent. Add --send to send for real.\n")
        for row in rows:
            email = row["email"].strip()
            tag = " (already sent, will skip)" if email.lower() in skip else ""
            try:
                msg = build_message(cfg, row, subject, body, html_body)
            except Exception as e:
                print(f"!! {email or '(blank)'}: {e}\n")
                continue
            print(f"--- To: {msg['To']}{tag}\nSubject: {msg['Subject']}")
            print(msg.get_body(("plain",)).get_content().rstrip())
            if html_body:
                print("[+ HTML version]")
            names = [p.get_filename() for p in msg.iter_attachments()]
            if names:
                print(f"[attachments: {', '.join(names)}]")
            missing = missing_fields(row, subject, body, html_body)
            if missing:
                print(f"[warning: no column for {', '.join(missing)}]")
            print()
        print(f"{pending} email(s) would be sent.")
        return 0

    if not cfg["password"]:
        sys.exit("No password: set it in config.ini or the MAIL_PASSWORD environment variable.")
    if not pending:
        print("Nothing to send: everyone on the list has already been sent to (use --no-resume to send again).")
        return 0
    if cfg["daily_limit"]:
        left = cfg["daily_limit"] - sent_since(24)
        if left < pending:
            print(f"Note: only {max(0, left)} of today's {cfg['daily_limit']} sends are left; "
                  "the run will stop there and can be resumed later.")
    if not args.yes:
        answer = input(f"Send {pending} email(s) from {cfg['from_email']}? Type 'yes' to continue: ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1

    def show(i, total, email, status, detail):
        label = {"SENT": "SENT  ", "FAILED": "FAILED", "SKIP": "SKIP  ", "RETRY": "RETRY "}[status]
        print(f"[{i}/{total}] {label} {email}" + (f" ({detail})" if detail else ""), flush=True)

    stop = threading.Event()
    try:
        result = run_campaign(cfg, rows, subject, body, html_body, resend=frozenset(already_sent() if args.no_resume else ()),
                              limit=args.limit, on_event=show, stop=stop,
                              source="cli", campaign=args.resume)
    except KeyboardInterrupt:
        stop.set()
        sys.exit("\nInterrupted. Run again (or --resume) to continue; sent addresses are skipped.")
    print(f"\nCampaign {result['campaign']}: {result['status']}. Sent: {result['sent']}  "
          f"Failed: {result['failed']}  Skipped: {result['skipped']}  (log: {LOG_FILE.name})")
    if result["error"]:
        print(result["error"])
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
