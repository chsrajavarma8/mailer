# Mailer: personalised bulk email (CSV + text template)

Sends a personalised email to every row of `recipients.csv`, using `template.txt`, through your own
Gmail/Outlook account. Includes a private web dashboard that runs only on your computer.
Uses only the Python standard library, so there is nothing to `pip install`.

## Quick start
1. Install Python 3.9+ (tick "Add Python to PATH").
2. In this folder run `python app.py` (it creates `config.ini` from `config.example.ini` on first run). The dashboard opens at http://127.0.0.1:8765
3. Turn on 2-Step Verification and create an App Password at https://myaccount.google.com/apppasswords
4. **Settings → Mail account**: enter sender, login and the app password, then **Test connection**.
5. **Settings → Security**: set a dashboard password and click **Encrypt now** for the app password.
6. **Recipients**: import a CSV with an `email` column, then **Remove duplicates**.
7. **Compose**: write the message with `{column}` fields, then **Send test to me**.
8. **Send**: fix any listed problems and click **Send**.

The **Guide** tab in the dashboard has the full manual, the technology overview and troubleshooting.

## Dashboard
| Tab | What it does |
|---|---|
| Compose | Subject/message editor, `{column}` chips, per-recipient preview, send a test to yourself |
| Recipients | Spreadsheet editor, CSV import/export, search, remove duplicates, attachments, ↻ resend to one person |
| Send | Pre-flight check, live progress with Stop, **resend to people already sent** (pick any or all), filterable/exportable history |
| Settings | Mail account, dashboard lock, session timeout, app-password encryption, **reset dashboard**, backups/restore, audit log |
| Guide | How to start, features, security, technology, files, CLI, troubleshooting, live system info |

### Reset
**Settings → Reset dashboard** clears any of: recipient list, subject/message, send history, attachments.
Sender name, SMTP login, app password and security settings are never touched. A backup is saved to
`backups/` first and can be restored from the Backups card.

### Resend
Already-sent people are skipped by default. Click ↻ on a row, or tick people under
**Send → Resend to members already sent**. Resends are logged with the note `resend`.

## Security
- Listens on 127.0.0.1 only; Host-header checks block DNS rebinding.
- Optional dashboard password (PBKDF2-SHA256, 600k rounds), login throttling, idle session timeout.
- HttpOnly + SameSite=Strict session cookie and a per-session CSRF token on every change.
- Content-Security-Policy with a script nonce, `X-Frame-Options: DENY`, `nosniff`, no referrer.
- App password encrypted at rest with Windows DPAPI and never sent to the browser.
- SMTP over STARTTLS (587) or SSL (465) with certificate verification; auto-reconnect if the server drops.
- Attachments confined to `attachments/`; executables/scripts blocked; 25 MB upload limit.
- Destructive actions ask for the dashboard password again; all admin events go to `audit_log.csv`.

Forgot the dashboard password? Stop the server, clear the `dashboard_password =` value in `config.ini`, restart.

## Technology
Python 3 standard library (`http.server`, `smtplib`, `ssl`, `email`, `csv`, `configparser`, `hashlib`,
`hmac`, `secrets`, `threading`, `ctypes` → Windows DPAPI) and a single-page HTML/CSS/vanilla-JS dashboard.
No frameworks, no CDN, no database: data lives in plain CSV/INI/TXT files.

## Files
| File | Purpose |
|---|---|
| `app.py`, `ui/index.html` | Dashboard server/API and the dashboard |
| `send_mails.py` | Message building and sending; also a command-line tool |
| `secure_store.py` | DPAPI encryption for the app password |
| `recipients.csv` | One row per recipient; must have an `email` column |
| `template.txt` | First line `Subject: ...`, then the body with `{column}` placeholders |
| `config.ini` | SMTP server, login, delay, security settings (private, not in git; template: `config.example.ini`) |
| `attachments/` | Files that can be attached (CSV `attachment` column; separate several with `;`) |
| `sent_log.csv` | SENT/FAILED per address (decides who is skipped) |
| `audit_log.csv` | Security and admin events |
| `backups/` | Snapshots from reset, clear history and restore |

## Command line
```powershell
python app.py                          # dashboard
python app.py --port 9000 --no-browser
python send_mails.py                   # preview every email (nothing is sent)
python send_mails.py --send            # send to everyone not yet sent
python send_mails.py --send --resend   # send to everyone, including already sent
$env:MAIL_PASSWORD = "abcdefghijklmnop"  # use a password without storing it in a file
```
If a run stops partway through, run it again: addresses already marked SENT are skipped.
