# Mailer: personalised bulk email (CSV + template)

Sends a personalised email to every row of a CSV, through your own Gmail/Outlook account, from a private
dashboard that runs only on your computer. Uses only the Python standard library: nothing to `pip install`.
Also available as a command-line tool and a single Windows `.exe`. All three share the same code and logs.

## What it does
1. **Loads a CSV of recipients.** The delimiter (`,` `;` tab `|`) and the email column (`email`, `E-mail`,
   `Email Address`, … or whichever column is full of addresses) are detected automatically. Addresses are
   validated, duplicates are found ignoring upper/lower case, and bad or blank rows are reported instead of
   crashing. Excel's UTF-8 and Windows-1252 exports both work.
2. **Fills in a template for each person.** A subject, a plain-text body and an optional HTML body, with
   `{{placeholders}}` (or the older `{placeholder}`) for any CSV column, matched case-insensitively. Values
   are HTML-escaped in the HTML body.
3. **Sends with a pause between emails** (configurable). Temporary errors (server busy, dropped connection,
   4xx) are retried with a doubling wait; permanent errors (unknown address, 5xx) are not. If the server
   says the daily quota is used up, the run stops cleanly. A daily limit (rolling 24 h) is enforced too.
4. **Logs everything.** Every result goes to `sent_log.csv`. Each run is a *campaign* with its own copy of the
   list and message in `campaigns/`; an interrupted campaign can be resumed and nobody gets the email twice.

## Quick start
1. Install Python 3.9+ (tick "Add Python to PATH"), or use `Mailer.exe` (see below).
2. In this folder run `python app.py` (it creates `config.ini` from `config.example.ini` on first run). The dashboard opens at http://127.0.0.1:8765
3. Turn on 2-Step Verification and create an App Password at https://myaccount.google.com/apppasswords
4. **Settings → Mail account**: enter sender, login and the app password, then **Test connection**. Set the
   daily limit to your account's (Gmail ≈ 500, Google Workspace ≈ 2000). Run the **Health check**.
5. **Settings → Security**: set a dashboard password and click **Encrypt now** for the app password.
6. **Recipients**: import a CSV, review the valid / invalid / duplicate report, then **Remove duplicates**.
7. **Compose**: write the message with `{{column}}` fields (and optionally HTML), then **Send test to me**.
8. **Send**: fix any listed problems and click **Send**. Watch progress live; **Stop** any time.

The **Guide** tab in the dashboard has the full manual, the technology overview and troubleshooting.

## Dashboard
| Tab | What it does |
|---|---|
| Compose | Subject, plain-text and HTML editors, `{{column}}` chips, per-recipient preview (HTML in a sandbox), send a test to yourself, save/load named templates |
| Recipients | Spreadsheet editor, CSV import with analysis, export, search, remove duplicates, attachments, ↻ resend to one person |
| Send | Pre-flight check incl. daily limit, live progress with retries and Stop, resend to people already sent, **campaign history** (filter, per-campaign CSV export, **resume**), interrupted-campaign warning, exportable log |
| Settings | Mail account, reply-to, delay / retries / daily limit, **health check**, dashboard lock, session timeout, app-password encryption, reset, backups/restore, audit log |
| Guide | How to start, features, security, technology, files, CLI, troubleshooting, live system info |

### Resend and resume
Already-sent people are skipped by default. Click ↻ on a row, or tick people under
**Send → Resend to members already sent**. Resends are logged with the note `resend`.
A campaign that was stopped, hit the daily limit, or was cut off (computer shut down, window closed) shows
**Resume** under **Send → Campaigns**; it continues from the campaign's saved list and message.

### Reset
**Settings → Reset dashboard** clears any of: recipient list, subject/message, send history, attachments.
Sender name, SMTP login, app password and security settings are never touched. A backup is saved to
`backups/` first and can be restored from the Backups card.

## Security
- Listens on 127.0.0.1 only; Host-header checks block DNS rebinding.
- Optional dashboard password (PBKDF2-SHA256, 600k rounds), login throttling, idle session timeout.
- HttpOnly + SameSite=Strict session cookie and a per-session CSRF token on every change.
- Content-Security-Policy with a script nonce, `X-Frame-Options: DENY`, `nosniff`, no referrer.
- HTML previews render in a sandboxed iframe (no scripts); CSV exports are protected against formula injection.
- Campaign IDs and template names are validated before being used in file paths.
- App password encrypted at rest with Windows DPAPI and never sent to the browser or written to logs.
- SMTP over STARTTLS (587) or SSL (465) with certificate verification.
- Attachments confined to `attachments/`; executables/scripts blocked; 25 MB upload limit.
- Destructive actions ask for the dashboard password again; all admin events go to `audit_log.csv`.

Forgot the dashboard password? Stop the server, clear the `dashboard_password =` value in `config.ini`, restart.

## Command line
```powershell
python app.py                                  # dashboard
python app.py --port 9000 --no-browser

python send_mails.py                           # dry run: print every email + a list report (nothing is sent)
python send_mails.py --check                   # health check (exit code 1 if something must be fixed)
python send_mails.py --send                    # send to everyone not yet sent (asks to confirm)
python send_mails.py --send --yes --limit 100  # no prompt, at most 100 this run
python send_mails.py --send --no-resume        # also send to people already sent (alias: --resend)
python send_mails.py --csv list.csv --email-column "Work Email" --html template.html
python send_mails.py --send --resume 20261014-153000   # continue a saved campaign
$env:MAIL_PASSWORD = "abcdefghijklmnop"        # use a password without storing it in a file
```
If a run stops partway through, run it again: addresses already marked SENT are skipped.

## Windows .exe
```powershell
powershell -ExecutionPolicy Bypass -File build_exe.ps1   # builds dist\Mailer.exe (uses PyInstaller in .build-venv)
```
Copy `Mailer.exe` to any folder and run it: it opens the dashboard in your browser and keeps all data
(`config.ini`, lists, logs, campaigns) in that folder, next to the `.exe`.

## Settings (`config.ini`)
| Section | Key | Meaning |
|---|---|---|
| `[smtp]` | `host`, `port`, `username`, `password` | Mail server and login (password may be DPAPI-encrypted or come from `MAIL_PASSWORD`) |
| `[sender]` | `from_name`, `from_email`, `reply_to` | Who the email is from, and where replies go |
| `[options]` | `delay_seconds` | Pause between emails (default 2) |
| | `max_retries`, `retry_delay_seconds` | Retries for temporary errors and the first wait, which doubles (3, 5 s) |
| | `daily_limit` | Max sends per rolling 24 hours, 0 = off (500) |
| `[security]` | `dashboard_password`, `session_minutes` | Managed from Settings → Security |

## Files
| File | Purpose |
|---|---|
| `app.py`, `ui/index.html` | Dashboard server/API and the dashboard (plain HTML/CSS/JS, no build step) |
| `send_mails.py` | CSV parsing, templates, message building, sending/retries, campaigns, health check; also the CLI |
| `secure_store.py` | DPAPI encryption for the app password |
| `tests/` | `unittest` suite (also runs under pytest); SMTP is faked so nothing is ever sent |
| `build_exe.ps1` | Builds the single-file Windows executable |
| `recipients.csv` | One row per recipient |
| `template.txt` / `template.html` | First line `Subject: ...`, then the body / optional HTML body |
| `config.ini` | Settings (private, not in git; template: `config.example.ini`) |
| `attachments/` | Files that can be attached (CSV `attachment` column; separate several with `;`) |
| `sent_log.csv` | SENT/FAILED per address and campaign (decides who is skipped) |
| `campaigns/` | `index.csv` of every run plus each run's list and message (for resume) |
| `saved_templates/` | Named templates |
| `audit_log.csv` | Security and admin events |
| `backups/` | Snapshots from reset, clear history and restore |

## Development
```powershell
python -m unittest discover -s tests -t .    # or: python -m pytest
```
GitHub Actions runs the suite on Windows and Linux with Python 3.9 and 3.13.
