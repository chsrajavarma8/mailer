"""Local web dashboard for send_mails.py. Run: python app.py [--port N] [--no-browser]"""

import argparse
import base64
import configparser
import csv
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import smtplib
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import secure_store
import send_mails as sm

APP_NAME = "Mailer"
VERSION = "3.0.0"

BASE_DIR = sm.BASE_DIR
RES_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))  # bundled read-only files when packaged as an .exe
CSV_FILE = BASE_DIR / "recipients.csv"
TEMPLATE_FILE = BASE_DIR / "template.txt"
TEMPLATE_HTML = BASE_DIR / "template.html"
CONFIG_FILE = BASE_DIR / "config.ini"
ATTACH_DIR = sm.ATTACH_DIR
BACKUP_DIR = BASE_DIR / "backups"
SAVED_DIR = BASE_DIR / "saved_templates"
AUDIT_FILE = BASE_DIR / "audit_log.csv"
UI_FILE = RES_DIR / "ui" / "index.html"

MAX_JSON = 5 * 1024 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
MAX_CSV = 10 * 1024 * 1024
TEMPLATE_NAME_RE = re.compile(r"^\w[\w .()-]{0,59}$")
RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
BLOCKED_EXT = {".exe", ".bat", ".cmd", ".com", ".scr", ".pif", ".msi", ".msp", ".vbs", ".vbe", ".js",
               ".jse", ".wsf", ".wsh", ".ps1", ".psm1", ".hta", ".cpl", ".jar", ".lnk", ".reg", ".dll", ".sys"}
PBKDF2_ITER = 600_000
MIN_LOCK_PASSWORD = 8
STARTED = datetime.now()

file_lock = threading.Lock()
audit_lock = threading.Lock()


class UserError(Exception):
    pass


class AuthError(Exception):
    pass


def safe(fn, *args):
    try:
        return fn(*args)
    except SystemExit as e:
        raise UserError(str(e))


def read_template():
    html_body = sm.load_html(TEMPLATE_HTML)
    if not TEMPLATE_FILE.exists():
        return "", "", html_body
    text = sm.read_text(TEMPLATE_FILE)
    first, _, body = text.partition("\n")
    if first.lower().startswith("subject:"):
        return first.split(":", 1)[1].strip(), body.lstrip("\n"), html_body
    return "", text, html_body


def write_template(subject, body, html_body=None):
    subject = str(subject or "").replace("\r", " ").replace("\n", " ").strip()
    TEMPLATE_FILE.write_text(f"Subject: {subject}\n\n{body or ''}", encoding="utf-8")
    if html_body is not None:
        if str(html_body).strip():
            TEMPLATE_HTML.write_text(str(html_body), encoding="utf-8")
        elif TEMPLATE_HTML.exists():
            TEMPLATE_HTML.unlink()


def read_recipients():
    if not CSV_FILE.exists():
        return ["email", "name"], []
    text = sm.read_text(CSV_FILE)
    try:
        columns, rows, _ = sm.parse_recipients(text)
        return columns, rows
    except ValueError:
        reader = csv.DictReader(text.lstrip("﻿").splitlines())
        columns = [c.strip() for c in (reader.fieldnames or []) if c]
        rows = [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in reader]
        return columns or ["email"], rows


def write_recipients(columns, rows):
    columns = [c.strip() for c in columns if c and c.strip()]
    if "email" not in columns:
        raise UserError("The recipient list must have an 'email' column.")
    if len(set(columns)) != len(columns):
        raise UserError("Column names must be unique.")
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: str(r.get(c, "")).strip() for c in columns})


def read_ini():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


def read_config_public():
    cfg = safe(sm.load_config, CONFIG_FILE)
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "username": cfg["username"],
        "has_password": bool(cfg["password"]),
        "from_name": cfg["from_name"],
        "from_email": cfg["from_email"],
        "reply_to": cfg["reply_to"],
        "delay": cfg["delay"],
        "max_retries": cfg["max_retries"],
        "retry_delay": cfg["retry_delay"],
        "daily_limit": cfg["daily_limit"],
        "sent_24h": sm.sent_since(24),
    }


def update_ini(path, updates):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = {s: dict(kv) for s, kv in updates.items()}
    out, section = [], None

    def flush(sec):
        for k, v in pending.pop(sec, {}).items():
            out.append(f"{k} = {v}")

    for line in lines:
        m = re.match(r"\s*\[([^\]]+)\]\s*$", line)
        if m:
            if section in pending:
                flush(section)
            section = m.group(1).strip()
            out.append(line)
            continue
        m = re.match(r"\s*([^=;#\s][^=]*?)\s*=", line)
        if m and section in pending and m.group(1) in pending[section]:
            out.append(f"{m.group(1)} = {pending[section].pop(m.group(1))}")
            continue
        out.append(line)
    if section in pending:
        flush(section)
    for sec in list(pending):
        out += ["", f"[{sec}]"]
        flush(sec)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def list_attachments():
    ATTACH_DIR.mkdir(exist_ok=True)
    return sorted(
        ({"path": f"attachments/{p.name}", "name": p.name, "size": p.stat().st_size}
         for p in ATTACH_DIR.iterdir() if p.is_file()),
        key=lambda a: a["name"].lower(),
    )


read_log = sm.read_log


def render(cfg, row, subject, body, skip, html_body=""):
    row = {str(k): "" if v is None else str(v) for k, v in row.items()}
    email = row.get("email", "").strip()
    item = {"email": email, "already_sent": email.lower() in skip}
    if not email:
        item["error"] = "missing email address"
        return item
    try:
        msg = sm.build_message(cfg, row, subject, body, html_body)
    except Exception as e:
        item["error"] = str(e)
        return item
    html_part = msg.get_body(("html",))
    item.update(
        from_=msg["From"],
        subject=msg["Subject"],
        body=msg.get_body(("plain",)).get_content(),
        html=html_part.get_content() if html_part else "",
        attachments=[p.get_filename() for p in msg.iter_attachments()],
        missing=sm.missing_fields(row, subject, body, html_body),
    )
    return item


def saved_template_path(name):
    name = str(name or "").strip()
    if not TEMPLATE_NAME_RE.match(name) or name.endswith(".") or name.split(".")[0].lower() in RESERVED_NAMES:
        raise UserError("Use a template name of letters, numbers, spaces, dots, dashes or brackets (max 60).")
    return SAVED_DIR / f"{name}.json"


def list_saved_templates():
    if not SAVED_DIR.exists():
        return []
    out = []
    for p in sorted(SAVED_DIR.glob("*.json"), key=lambda p: p.stem.lower()):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"name": p.stem, "subject": str(d.get("subject", "")), "body": str(d.get("body", "")),
                    "html": str(d.get("html", "")),
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")})
    return out


DATA_FILES = {"recipients": CSV_FILE, "template": TEMPLATE_FILE, "template_html": TEMPLATE_HTML,
              "history": sm.LOG_FILE}


def make_backup(reason):
    dest = BACKUP_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{reason}"
    n = 1
    while dest.exists():
        n += 1
        dest = dest.with_name(f"{dest.name}-{n}")
    dest.mkdir(parents=True)
    for f in DATA_FILES.values():
        if f.exists():
            shutil.copy2(f, dest / f.name)
    if ATTACH_DIR.exists() and any(ATTACH_DIR.iterdir()):
        shutil.copytree(ATTACH_DIR, dest / "attachments")
    return dest.name


def list_backups():
    if not BACKUP_DIR.exists():
        return []
    out = []
    for d in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        rows = 0
        if (d / CSV_FILE.name).exists():
            with open(d / CSV_FILE.name, encoding="utf-8-sig") as f:
                rows = max(0, sum(1 for _ in f) - 1)
        att = d / "attachments"
        out.append({"name": d.name, "recipients": rows,
                    "has_template": (d / TEMPLATE_FILE.name).exists(),
                    "has_history": (d / sm.LOG_FILE.name).exists(),
                    "attachments": len(list(att.iterdir())) if att.exists() else 0})
    return out


def backup_path(name):
    name = Path(name or "").name
    p = BACKUP_DIR / name
    if not name or not p.is_dir():
        raise UserError("Backup not found.")
    return p


def clear_attachments():
    if ATTACH_DIR.exists():
        for p in ATTACH_DIR.iterdir():
            if p.is_file():
                p.unlink()


def b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${b64e(salt)}${b64e(dk)}"


def verify_password(pw, stored):
    try:
        algo, iters, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), b64d(salt), int(iters))
        return hmac.compare_digest(calc, b64d(dk))
    except (ValueError, TypeError):
        return False


def security_settings():
    ini = read_ini()
    try:
        minutes = int(ini.get("security", "session_minutes", fallback="30"))
    except ValueError:
        minutes = 30
    return {"lock_hash": ini.get("security", "dashboard_password", fallback="").strip(),
            "session_minutes": max(5, min(minutes, 1440))}


def smtp_password_storage():
    raw = read_ini().get("smtp", "password", fallback="").strip()
    if os.environ.get("MAIL_PASSWORD"):
        return "environment"
    if not raw:
        return "none"
    return "encrypted" if secure_store.is_encrypted(raw) else "plaintext"


def audit(event, detail=""):
    with audit_lock:
        new = not AUDIT_FILE.exists()
        with open(AUDIT_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "event", "detail"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), event, detail])


def read_audit(limit=200):
    if not AUDIT_FILE.exists():
        return []
    with open(AUDIT_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[-limit:][::-1]


class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def create(self):
        sid = secrets.token_urlsafe(32)
        s = {"csrf": secrets.token_urlsafe(32), "seen": time.time()}
        with self.lock:
            self.items[sid] = s
        return sid, s

    def get(self, sid, minutes):
        with self.lock:
            s = self.items.get(sid or "")
            if not s:
                return None
            if time.time() - s["seen"] > minutes * 60:
                del self.items[sid]
                return None
            s["seen"] = time.time()
            return s

    def drop(self, sid):
        with self.lock:
            self.items.pop(sid or "", None)

    def drop_others(self, keep):
        with self.lock:
            self.items = {k: v for k, v in self.items.items() if k == keep}


class Throttle:
    """After 5 wrong passwords, lock out for 30s, doubling each further failure (max 15 min)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.failures = 0
        self.until = 0.0

    def check(self):
        with self.lock:
            wait = self.until - time.time()
        if wait > 0:
            raise AuthError(f"Too many wrong attempts. Try again in {int(wait) + 1} seconds.")

    def fail(self):
        with self.lock:
            self.failures += 1
            if self.failures >= 5:
                self.until = time.time() + min(900, 30 * 2 ** (self.failures - 5))

    def ok(self):
        with self.lock:
            self.failures, self.until = 0, 0.0


sessions = Sessions()
throttle = Throttle()


class SendJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "events": [], "total": 0, "sent": 0, "failed": 0, "skipped": 0,
                      "error": None, "finished": False, "resend": False, "campaign": None}
        self.stop_flag = threading.Event()

    def snapshot(self, since=0):
        with self.lock:
            s = dict(self.state)
            s["events"] = self.state["events"][since:]
            s["event_count"] = len(self.state["events"])
            return s

    def _event(self, i, total, email, status, detail=""):
        with self.lock:
            self.state["events"].append({"i": i, "email": email, "status": status, "detail": detail})
            key = {"SENT": "sent", "FAILED": "failed", "SKIP": "skipped"}.get(status)
            if key:
                self.state[key] += 1

    def start(self, rows, subject, body, html_body="", columns=None, resend=frozenset(), campaign=None):
        cfg = safe(sm.load_config, CONFIG_FILE)
        if not cfg["password"]:
            raise UserError("No password set. Add an App Password in Settings.")
        with self.lock:
            if self.state["running"]:
                raise UserError("A send is already running.")
            if campaign is None:
                campaign = sm.new_campaign(subject, body, html_body, columns or ["email"], rows, "dashboard")
            self.state = {"running": True, "events": [], "total": len(rows), "sent": 0, "failed": 0,
                          "skipped": 0, "error": None, "finished": False, "resend": bool(resend),
                          "campaign": campaign}
        self.stop_flag.clear()
        threading.Thread(target=self._run, args=(cfg, rows, subject, body, html_body, columns, resend, campaign),
                         daemon=True).start()
        return campaign

    def _run(self, cfg, rows, subject, body, html_body, columns, resend, campaign):
        error = None
        try:
            result = sm.run_campaign(cfg, rows, subject, body, html_body, columns=columns, resend=resend,
                                     on_event=self._event, stop=self.stop_flag, source="dashboard",
                                     campaign=campaign)
            error = result["error"]
        except Exception as e:
            error = str(e)
        finally:
            with self.lock:
                self.state["error"] = error
                self.state["running"] = False
                self.state["finished"] = True
                s = dict(self.state)
            audit("send_finished", f"campaign {campaign}: sent {s['sent']}, failed {s['failed']}, "
                  f"skipped {s['skipped']}" + (f" ({error})" if error else ""))


job = SendJob()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "MailUI"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    @property
    def cookie_name(self):
        return f"mailer_sid_{self.server.server_address[1]}"

    def _send(self, code, payload, ctype="application/json", headers=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _csv(self, filename, text):
        return self._send(200, ("﻿" + text).encode("utf-8"), "text/csv; charset=utf-8",
                          {"Content-Disposition": f'attachment; filename="{filename}"'})

    def _cookie(self, sid, clear=False):
        attrs = "HttpOnly; SameSite=Strict; Path=/"
        if clear:
            return {"Set-Cookie": f"{self.cookie_name}=; Max-Age=0; {attrs}"}
        return {"Set-Cookie": f"{self.cookie_name}={sid}; {attrs}"}

    def _sid(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        m = c.get(self.cookie_name)
        return m.value if m else None

    def _session(self):
        return sessions.get(self._sid(), security_settings()["session_minutes"])

    def _body(self, limit=MAX_JSON):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise UserError("Bad Content-Length")
        if n > limit:
            raise UserError(f"Too large (limit {limit // (1024 * 1024)} MB).")
        return self.rfile.read(n) if n else b""

    def _json(self):
        try:
            d = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            raise UserError("Bad JSON")
        if not isinstance(d, dict):
            raise UserError("Bad JSON")
        return d

    def _reauth(self, d):
        """Destructive actions need the dashboard password again when the lock is on."""
        h = security_settings()["lock_hash"]
        if not h:
            return
        throttle.check()
        if not verify_password(d.get("password", ""), h):
            throttle.fail()
            audit("reauth_failed")
            raise UserError("Wrong dashboard password.")
        throttle.ok()

    def _page(self):
        nonce = secrets.token_urlsafe(16)
        html = UI_FILE.read_text(encoding="utf-8").replace("__CSP_NONCE__", nonce)
        csp = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        return self._send(200, html.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                return self._page()
            if url.path == "/api/session":
                return self.get_session()
            sess = self._session()
            if not sess:
                return self._send(401, {"error": "login required", "auth": True})
            if url.path == "/api/state":
                return self._send(200, self.state())
            if url.path == "/api/progress":
                since = int(parse_qs(url.query).get("since", ["0"])[0])
                return self._send(200, job.snapshot(since))
            if url.path == "/api/log":
                return self._send(200, {"log": read_log()})
            if url.path == "/api/log.csv":
                return self._csv("sent_log.csv", sm.to_csv(sm.LOG_FIELDS, read_log()))
            if url.path == "/api/campaigns":
                return self._send(200, {"campaigns": sm.list_campaigns(), "sent_24h": sm.sent_since(24)})
            if url.path == "/api/campaign.csv":
                cid = parse_qs(url.query).get("id", [""])[0]
                sm.campaign_path(cid)
                return self._csv(f"campaign-{cid}.csv", sm.to_csv(sm.LOG_FIELDS[:4], sm.campaign_results(cid)))
            if url.path == "/api/health":
                return self._send(200, {"checks": sm.health_checks(CONFIG_FILE, CSV_FILE, TEMPLATE_FILE, TEMPLATE_HTML)})
            if url.path == "/api/templates":
                return self._send(200, {"templates": list_saved_templates()})
            if url.path == "/api/security":
                return self._send(200, self.security_state())
            if url.path == "/api/about":
                return self._send(200, self.about())
            if url.path.startswith("/attachments/"):
                p = ATTACH_DIR / Path(unquote(url.path)).name
                if p.is_file():
                    return self._send(200, p.read_bytes(), "application/octet-stream",
                                      {"Content-Disposition": f'attachment; filename="{p.name}"'})
            return self._send(404, {"error": "not found"})
        except UserError as e:
            return self._send(400, {"error": str(e)})
        except (ValueError, OSError) as e:
            return self._send(400, {"error": str(e)})

    def do_POST(self):
        # Requiring a non-"simple" content type forces a CORS preflight, which we never
        # approve, so other websites cannot drive this server from your browser.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if not self._host_ok() or ctype not in ("application/json", "application/octet-stream"):
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        name = url.path.removeprefix("/api/").replace("-", "_")
        route = getattr(self, "post_" + name, None) if re.fullmatch(r"[a-z_]+", name) else None
        if not url.path.startswith("/api/") or route is None:
            return self._send(404, {"error": "not found"})
        try:
            if name != "login":
                sess = self._session()
                if not sess:
                    return self._send(401, {"error": "Your session has expired. Please log in again.", "auth": True})
                if not hmac.compare_digest(self.headers.get("X-CSRF-Token") or "", sess["csrf"]):
                    return self._send(403, {"error": "Security token mismatch. Reload the page."})
            with file_lock:
                result = route(url)
            if isinstance(result, tuple):
                return self._send(200, result[0], headers=result[1])
            return self._send(200, result or {"ok": True})
        except AuthError as e:
            return self._send(429, {"error": str(e)})
        except UserError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def get_session(self):
        sec = security_settings()
        locked = bool(sec["lock_hash"])
        sess = self._session()
        if sess:
            return self._send(200, {"authenticated": True, "lock": locked, "csrf": sess["csrf"]})
        if not locked:
            sid, sess = sessions.create()
            audit("session_started", "no dashboard password set")
            return self._send(200, {"authenticated": True, "lock": False, "csrf": sess["csrf"]}, headers=self._cookie(sid))
        return self._send(200, {"authenticated": False, "lock": True})

    def post_login(self, url):
        d = self._json()
        h = security_settings()["lock_hash"]
        throttle.check()
        if h and not verify_password(d.get("password", ""), h):
            throttle.fail()
            audit("login_failed")
            raise UserError("Wrong password.")
        throttle.ok()
        sessions.drop(self._sid())
        sid, sess = sessions.create()
        audit("login")
        return {"ok": True, "csrf": sess["csrf"], "lock": bool(h)}, self._cookie(sid)

    def post_logout(self, url):
        sessions.drop(self._sid())
        audit("logout")
        return {"ok": True}, self._cookie("", clear=True)

    def state(self):
        subject, body, html_body = read_template()
        columns, rows = read_recipients()
        try:
            config, config_error = read_config_public(), None
        except UserError as e:
            config, config_error = None, str(e)
        return {
            "subject": subject, "body": body, "html": html_body, "columns": columns, "rows": rows,
            "templates": list_saved_templates(), "campaigns": sm.list_campaigns(),
            "config": config, "config_error": config_error,
            "attachments": list_attachments(), "log": read_log(),
            "sent": sorted(sm.already_sent()), "job": job.snapshot(),
        }

    def security_state(self):
        sec = security_settings()
        return {
            "lock": bool(sec["lock_hash"]),
            "session_minutes": sec["session_minutes"],
            "smtp_storage": smtp_password_storage(),
            "dpapi": secure_store.available(),
            "audit": read_audit(),
            "backups": list_backups(),
        }

    def about(self):
        def info(p):
            return {"name": p.name, "exists": p.exists(),
                    "size": p.stat().st_size if p.is_file() else None,
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds") if p.exists() else None}
        columns, rows = read_recipients()
        return {
            "app": APP_NAME, "version": VERSION,
            "python": platform.python_version(), "implementation": platform.python_implementation(),
            "os": f"{platform.system()} {platform.release()}", "machine": platform.machine(),
            "url": f"http://127.0.0.1:{self.server.server_address[1]}/",
            "started": STARTED.isoformat(timespec="seconds"),
            "folder": str(BASE_DIR),
            "files": [info(p) for p in (CSV_FILE, TEMPLATE_FILE, TEMPLATE_HTML, CONFIG_FILE, sm.LOG_FILE,
                                        AUDIT_FILE, sm.CAMPAIGN_DIR / "index.csv")],
            "counts": {"recipients": len(rows), "columns": len(columns), "log": len(read_log()),
                       "sent": len(sm.already_sent()), "attachments": len(list_attachments()),
                       "backups": len(list_backups()), "campaigns": len(sm.list_campaigns()),
                       "templates": len(list_saved_templates())},
        }

    def post_template(self, url):
        d = self._json()
        write_template(d.get("subject", ""), d.get("body", ""), d.get("html"))

    def post_recipients(self, url):
        d = self._json()
        write_recipients(d.get("columns", []), d.get("rows", []))

    def post_parse_csv(self, url):
        """Parse an uploaded CSV the same way the command line does: detect delimiter and email column."""
        raw = self._body(MAX_CSV)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        column = parse_qs(url.query).get("email_column", [""])[0] or None
        try:
            columns, rows, info = sm.parse_recipients(text, column)
        except ValueError as e:
            raise UserError(str(e))
        return {"columns": columns, "rows": rows, "info": info, "report": sm.analyze(rows)}

    def post_preview(self, url):
        d = self._json()
        cfg = safe(sm.load_config, CONFIG_FILE)
        skip = sm.already_sent()
        rows = d.get("rows", [])
        html_body = d.get("html", "")
        return {"items": [render(cfg, r, d.get("subject", ""), d.get("body", ""), skip, html_body) for r in rows]}

    def post_save_template_as(self, url):
        d = self._json()
        p = saved_template_path(d.get("name"))
        if p.exists() and not d.get("overwrite"):
            raise UserError(f'A template called "{p.stem}" already exists.')
        SAVED_DIR.mkdir(exist_ok=True)
        p.write_text(json.dumps({k: str(d.get(k) or "") for k in ("subject", "body", "html")}, indent=2),
                     encoding="utf-8")
        audit("template_saved", p.stem)
        return {"ok": True, "templates": list_saved_templates()}

    def post_delete_template(self, url):
        p = saved_template_path(self._json().get("name"))
        if p.exists():
            p.unlink()
            audit("template_deleted", p.stem)
        return {"ok": True, "templates": list_saved_templates()}

    def post_config(self, url):
        d = self._json()
        try:
            port = int(d.get("port", 587))
            delay = float(d.get("delay", 2))
            max_retries = int(d.get("max_retries", 3))
            retry_delay = float(d.get("retry_delay", 5))
            daily_limit = int(d.get("daily_limit", 500))
        except (TypeError, ValueError):
            raise UserError("Port, delay, retries and limits must be numbers.")
        if not 1 <= port <= 65535 or not 0 <= delay <= 600:
            raise UserError("Port must be 1-65535 and delay 0-600 seconds.")
        if not 0 <= max_retries <= 10 or not 0 <= retry_delay <= 600 or not 0 <= daily_limit <= 100000:
            raise UserError("Retries must be 0-10, retry delay 0-600 seconds and the daily limit 0-100000.")
        clean = {k: str(d.get(k, "")).replace("\r", "").replace("\n", "").strip()
                 for k in ("host", "username", "from_name", "from_email", "reply_to")}
        if clean["reply_to"] and not sm.EMAIL_RE.match(clean["reply_to"]):
            raise UserError("Reply-to must be an email address (or empty).")
        smtp = {"host": clean["host"], "port": port, "username": clean["username"]}
        pw = (d.get("password") or "").replace(" ", "").replace("\n", "").replace("\r", "")
        if pw:
            smtp["password"] = secure_store.encrypt(pw)
        if d.get("clear_password"):
            smtp["password"] = ""
        update_ini(CONFIG_FILE, {
            "smtp": smtp,
            "sender": {"from_name": clean["from_name"], "from_email": clean["from_email"],
                       "reply_to": clean["reply_to"]},
            "options": {"delay_seconds": delay, "max_retries": max_retries,
                        "retry_delay_seconds": retry_delay, "daily_limit": daily_limit},
        })
        audit("settings_saved", "password changed" if pw else "")
        return {"ok": True, "config": read_config_public()}

    def post_test_connection(self, url):
        cfg = safe(sm.load_config, CONFIG_FILE)
        if not cfg["password"]:
            raise UserError("No password set.")
        try:
            sm.connect(cfg, timeout=20).quit()
        except smtplib.SMTPAuthenticationError:
            audit("test_connection", "login failed")
            raise UserError("Login failed. Gmail needs a 16-character App Password, not your normal password.")
        except (OSError, smtplib.SMTPException) as e:
            raise UserError(f"Could not connect to {cfg['host']}:{cfg['port']} ({e})")
        audit("test_connection", "ok")
        return {"ok": True, "message": f"Logged in to {cfg['host']} as {cfg['username']} (TLS verified)"}

    def post_test_email(self, url):
        d = self._json()
        cfg = safe(sm.load_config, CONFIG_FILE)
        if not cfg["password"]:
            raise UserError("No password set. Add an App Password in Settings.")
        to = cfg["from_email"] or cfg["username"]
        row = {str(k): str(v) for k, v in (d.get("row") or {}).items()}
        row.setdefault("email", to)
        if not sm.EMAIL_RE.match(row["email"]):
            row["email"] = to
        try:
            msg = sm.build_message(cfg, row, d.get("subject", ""), d.get("body", ""), d.get("html", ""))
        except Exception as e:
            raise UserError(str(e))
        msg.replace_header("To", to)
        msg.replace_header("Subject", "[TEST] " + msg["Subject"])
        try:
            with sm.Mailer(cfg) as m:
                m.send(msg)
        except smtplib.SMTPAuthenticationError:
            raise UserError("Login failed. Check the App Password in Settings.")
        except (OSError, smtplib.SMTPException) as e:
            raise UserError(f"Could not send: {e}")
        audit("test_email", f"to {to}")
        return {"ok": True, "message": f"Test email sent to {to}"}

    def post_upload(self, url):
        name = Path(unquote(parse_qs(url.query).get("name", [""])[0])).name.strip()
        if not name or name.startswith(".") or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
            raise UserError("Bad file name.")
        if Path(name).suffix.lower() in BLOCKED_EXT:
            raise UserError(f"{Path(name).suffix} files are blocked for safety (mail providers reject them too).")
        data = self._body(MAX_UPLOAD)
        ATTACH_DIR.mkdir(exist_ok=True)
        (ATTACH_DIR / name).write_bytes(data)
        audit("attachment_uploaded", f"{name} ({len(data)} bytes)")
        return {"ok": True, "path": f"attachments/{name}", "attachments": list_attachments()}

    def post_delete_attachment(self, url):
        name = Path(self._json().get("name", "")).name
        p = ATTACH_DIR / name
        if name and p.is_file():
            p.unlink()
            audit("attachment_deleted", name)
        return {"ok": True, "attachments": list_attachments()}

    def post_send(self, url):
        d = self._json()
        subject, body, html_body = d.get("subject", ""), d.get("body", ""), d.get("html", "")
        columns, rows = d.get("columns", []), d.get("rows", [])
        write_template(subject, body, html_body)
        write_recipients(columns, rows)
        if not subject.strip():
            raise UserError("The subject is empty.")
        only = {str(e).strip().lower() for e in d.get("only") or [] if str(e).strip()}
        resend = frozenset(only) if d.get("resend") else frozenset()
        if only:
            rows = [r for r in rows if str(r.get("email", "")).strip().lower() in only]
            if not rows:
                raise UserError("None of the selected addresses are in the recipient list.")
        rows = [{c: str(r.get(c, "")).strip() for c in columns} for r in rows]
        cid = job.start(rows, subject, body, html_body, columns, resend)
        audit("send_started", f"campaign {cid}: {len(rows)} recipient(s)"
              + (f", resending to {len(resend)}" if resend else ""))
        return {"ok": True, "campaign": cid}

    def post_resume(self, url):
        cid = str(self._json().get("id", ""))
        try:
            c = sm.load_campaign(cid)
        except (ValueError, OSError, SystemExit) as e:
            raise UserError(str(e) or "Campaign files not found.")
        if not any(x["id"] == cid for x in sm.list_campaigns()):
            raise UserError("Unknown campaign.")
        if any(x["id"] == cid and x["status"] == "running" for x in sm.list_campaigns()):
            raise UserError("That campaign is still running.")
        job.start(c["rows"], c["subject"], c["body"], c["html"], c["columns"], campaign=cid)
        audit("send_resumed", f"campaign {cid}")
        return {"ok": True, "campaign": cid}

    def post_stop(self, url):
        job.stop_flag.set()
        audit("send_stopped")

    def post_clear_log(self, url):
        d = self._json()
        if job.snapshot()["running"]:
            raise UserError("Wait for the current send to finish.")
        self._reauth(d)
        if sm.LOG_FILE.exists():
            backup = make_backup("history")
            sm.LOG_FILE.unlink()
            audit("history_cleared", f"backup {backup}")

    def post_reset(self, url):
        d = self._json()
        if str(d.get("confirm", "")).strip() != "RESET":
            raise UserError("Type RESET to confirm.")
        if job.snapshot()["running"]:
            raise UserError("Wait for the current send to finish.")
        self._reauth(d)
        parts = set(d.get("parts") or []) & {"recipients", "template", "history", "attachments"}
        if not parts:
            raise UserError("Choose at least one thing to reset.")
        backup = make_backup("reset") if d.get("backup", True) else None
        if "recipients" in parts:
            write_recipients(["email", "name"], [])
        if "template" in parts:
            write_template("", "", "")
        if "history" in parts and sm.LOG_FILE.exists():
            sm.LOG_FILE.unlink()
        if "attachments" in parts:
            clear_attachments()
        audit("dashboard_reset", ", ".join(sorted(parts)) + (f"; backup {backup}" if backup else "; no backup"))
        return {"ok": True, "backup": backup}

    def post_restore(self, url):
        d = self._json()
        if job.snapshot()["running"]:
            raise UserError("Wait for the current send to finish.")
        self._reauth(d)
        src = backup_path(d.get("name"))
        safety = make_backup("before-restore")
        for f in DATA_FILES.values():
            if (src / f.name).exists():
                shutil.copy2(src / f.name, f)
            elif f in (sm.LOG_FILE, TEMPLATE_HTML) and f.exists():
                f.unlink()
        if (src / "attachments").exists():
            clear_attachments()
            ATTACH_DIR.mkdir(exist_ok=True)
            for p in (src / "attachments").iterdir():
                if p.is_file():
                    shutil.copy2(p, ATTACH_DIR / p.name)
        audit("backup_restored", f"{src.name} (previous state saved as {safety})")
        return {"ok": True, "safety": safety}

    def post_delete_backup(self, url):
        d = self._json()
        self._reauth(d)
        p = backup_path(d.get("name"))
        shutil.rmtree(p)
        audit("backup_deleted", p.name)

    def post_security(self, url):
        d = self._json()
        action = d.get("action")
        sec = security_settings()
        if action in ("set_lock", "remove_lock"):
            if sec["lock_hash"]:
                throttle.check()
                if not verify_password(d.get("current", ""), sec["lock_hash"]):
                    throttle.fail()
                    audit("reauth_failed", action)
                    raise UserError("The current dashboard password is wrong.")
                throttle.ok()
            if action == "set_lock":
                new = d.get("new", "")
                if len(new) < MIN_LOCK_PASSWORD:
                    raise UserError(f"Use at least {MIN_LOCK_PASSWORD} characters.")
                update_ini(CONFIG_FILE, {"security": {"dashboard_password": hash_password(new)}})
                sessions.drop_others(self._sid())
                audit("lock_password_set", "other sessions signed out")
            else:
                update_ini(CONFIG_FILE, {"security": {"dashboard_password": ""}})
                audit("lock_password_removed")
        elif action == "session_minutes":
            try:
                minutes = int(d.get("minutes"))
            except (TypeError, ValueError):
                raise UserError("Enter a number of minutes.")
            if not 5 <= minutes <= 1440:
                raise UserError("Choose between 5 and 1440 minutes.")
            update_ini(CONFIG_FILE, {"security": {"session_minutes": minutes}})
            audit("session_timeout_changed", f"{minutes} min")
        elif action == "encrypt_smtp":
            raw = read_ini().get("smtp", "password", fallback="").strip()
            if not secure_store.available():
                raise UserError("Encryption is only available on Windows.")
            if not raw or secure_store.is_encrypted(raw):
                raise UserError("There is no plain-text password to encrypt.")
            update_ini(CONFIG_FILE, {"smtp": {"password": secure_store.encrypt(raw.replace(" ", ""))}})
            audit("smtp_password_encrypted")
        else:
            raise UserError("Unknown action.")
        return self.security_state()


def main():
    ap = argparse.ArgumentParser(description="Web dashboard for send_mails.py")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    example = RES_DIR / "config.example.ini"
    if not CONFIG_FILE.exists() and example.exists():
        shutil.copy(example, CONFIG_FILE)
        print("Created config.ini from config.example.ini. Fill it in under Settings.")

    url = f"http://127.0.0.1:{args.port}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"Port {args.port} is busy; {APP_NAME} is probably already running at {url}")
        print("Opening it. To run a second copy, use --port with another number.")
        if not args.no_browser:
            webbrowser.open(url)
        return
    print(f"{APP_NAME} {VERSION} running at {url}  (Ctrl+C to stop)")
    if smtp_password_storage() == "plaintext" and secure_store.available():
        print("Tip: your SMTP password is stored as plain text. Encrypt it in Settings > Security.")
    audit("server_started", url)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        audit("server_stopped")


if __name__ == "__main__":
    main()
