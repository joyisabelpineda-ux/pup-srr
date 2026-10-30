# PUP Smart Room Reservation System (PUP SRR)

A Flask + SQLite web application for browsing rooms, requesting reservations, administrator approval,
and QR-code room-access checks. Maroon-and-gold design from the original PUP SRR prototype
(`docs/original-prototype.html`).

> **Important limits:** room access is **simulated**. No door lock or IoT device is connected, and the
> system does not verify that anyone physically entered a room. Sample campuses/rooms are demo data, and
> there is no PUP student-information or HR integration, so registration does not prove PUP enrollment.

## Quick start on Windows (local)

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/> (tick **Add Python to PATH**).
2. Unzip this project, then **double-click `run_windows.bat`**.
   It creates a virtual environment, installs dependencies, creates `.env`, starts the server and opens the browser.
3. Local URL: **http://127.0.0.1:5000**

Manual alternative (PowerShell):

```powershell
cd PUP_SRR
python -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
copy .env.example .env
python app.py
```

macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && cp .env.example .env && python app.py`

The database is created automatically at `instance/pup_srr.sqlite3` and survives restarts.
Back it up with `flask --app wsgi backup-db`.

### Local demo accounts (development only)

| Role | User ID | Password |
|---|---|---|
| Student | `DEMO-STU-001` | `DemoPass123!` |
| Professor | `DEMO-PROF-001` | `DemoPass123!` |
| Administrator | `DEMO-ADM-001` | `DemoPass123!` |

These exist only when `SEED_DEMO_USERS=1` (default in development, **off in production**). Never use them on a public site.

## Features

- **Accounts:** registration with validation, hashed passwords (Werkzeug scrypt/PBKDF2), login by user ID or email, secure sessions,
  logout (POST + CSRF), password change (signs out other devices), role-based access
  (Student, Professor, Faculty, Staff, Administrator), validated `next` redirects, login/registration rate limiting, account lockout window.
- **Users:** dashboard with live counts, room search/filter (text, campus, building, status, capacity, *free at a given date/time*),
  room details and schedule, reservation request (date + start/end + purpose), history with filters, details, cancellation, profile editing.
- **Reservations:** server-side validation (no past times, end after start, min/max length — students 4 h, others 8 h by default —
  opening hours, advance limit), overlap rejection, `BEGIN IMMEDIATE` transactions **and** a database trigger so two approved
  reservations can never overlap, re-check at approval, admins cannot approve their own requests.
- **Administrators** (`/admin/`): live statistics, room management (add/edit, status Available/Reserved/Occupied/Under Maintenance, QR print/regenerate),
  reservation review, user activation/deactivation, password reset and role changes (protected against locking out the last admin), room status history,
  access logs, audit logs, six CSV reports (formula-injection safe), confirmation dialogs, searchable/filterable/paginated tables.
- **QR access:** each room has a unique random token; the QR opens `/scan/<token>`; login is required; the server checks for an approved,
  currently active reservation by *that user* for *that room*; maintenance rooms are denied; every decision is logged with a specific reason
  (pending, rejected, cancelled, not yet active, expired, none). Results are labelled simulated.
- **Room status:** Available↔Reserved follows active approved reservations automatically; Occupied/Maintenance/manual Reserved are admin-controlled; every change is recorded.

## Configuration

All settings are environment variables (see `.env.example`). The important ones:

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` (default) or `production` (requires `SECRET_KEY`, secure cookies, HSTS, no demo accounts) |
| `SECRET_KEY` | Long random string (≥32 chars) – required in production |
| `PUP_SRR_DB` | Path of the SQLite file (point at a persistent disk in production) |
| `ADMIN_USER_ID` / `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Creates the first administrator on startup if missing |
| `SEED_DEMO` / `SEED_DEMO_USERS` | Sample campuses+rooms (once per database) / demo accounts |
| `ALLOW_REGISTRATION` | `0` = only administrators' accounts (create via `flask --app wsgi create-admin`) |
| `PUBLIC_BASE_URL` | Address embedded in printed QR codes |
| `APP_TIMEZONE` | Default `Asia/Manila` |
| `RES_*` | Reservation limits (see `.env.example`) |

Database upgrades are automatic and versioned (`PRAGMA user_version`). An existing database (including the one from the earlier
prototype) is copied to `*.bak-<timestamp>` before its first upgrade and no rows are deleted.

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

77 tests use a temporary database per test class (they never touch `instance/`). Latest recorded run: `docs/TEST_RESULTS.txt`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `python` not found (Windows) | Reinstall Python with “Add to PATH”, or use `py -3` |
| PowerShell blocks `Activate.ps1` | `Set-ExecutionPolicy -Scope Process Bypass`, or use `run_windows.bat` |
| Port 5000 busy | Set `PORT=5001` in `.env` |
| “SECRET_KEY is not set” | You set `APP_ENV=production`; set a ≥32-character `SECRET_KEY` |
| Cannot log in locally with `APP_ENV=production` | Production uses Secure cookies that need HTTPS; use `development` locally |
| “Too many failed attempts” | Wait `LOGIN_WINDOW_SECONDS` (15 min) or delete rows from `rate_limit_events` |
| QR codes show `localhost` | Set `PUBLIC_BASE_URL` to the address phones can reach |
| Data lost after redeploy | The host's disk is ephemeral – see `docs/DEPLOYMENT.md` |

See `docs/DEPLOYMENT.md` to publish the site and `docs/BUGS_AND_FIXES.md` for what was wrong in the original project.
Security note: this has automated tests and sensible defaults, but has **not** had an independent security review or penetration test.
