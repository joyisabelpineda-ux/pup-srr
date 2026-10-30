# Important bugs found in the original project and how they were fixed

Original = `PUP_SRR_Flask_Upgrade.zip` (app nested in `venv/pup_srr/`). Its 6 bundled tests passed, but they covered very little.

| # | Problem found | Fix |
|---|---|---|
| 1 | Project packaging: the app lived inside a Windows virtual environment folder (`venv/pup_srr`), together with a real SQLite database and `__pycache__`. | Clean layout (`srr/` package, `templates/`, `static/`, `tests/`, `docs/`); venv, caches and databases excluded; `.gitignore` added. |
| 2 | **Deployment crash:** `init_db()` only ran under `python app.py`. Starting with `waitress-serve app:app` (as the README said) gave `no such table: users` (reproduced: HTTP 500 on login). | Database is created/migrated automatically when the app is created; `/healthz` added. |
| 3 | No CSRF protection on login/registration; logout had a token but none existed before login. | CSRF token in every session, checked on **every** POST (including login/register). |
| 4 | `next` redirect was generated but ignored, so deep links/QR scans were lost after login; no validation existed. | `next` honoured only for same-site relative paths (blocks `//host`, `https://…`, `\\`, `javascript:`); tested. |
| 5 | Hard-coded fallback `SECRET_KEY` used silently, even in production; cookies not Secure by default. | Production refuses to start without a ≥32-char key; Secure cookies/HSTS by default in production; dev key is random and stored outside Git. |
| 6 | Room status was set to `Reserved` at approval time and never reverted; seed rooms were `Reserved`/`Occupied` with no reservations behind them. | Status is synchronised with active approved reservations; manual statuses are respected; seeds are consistent; every change goes to history + audit. |
| 7 | Double-booking only guarded by app code. | Plus a SQLite trigger forbidding overlapping Approved rows; approval re-checks under `BEGIN IMMEDIATE`; concurrency tests (parallel requests and parallel approvals) pass. |
| 8 | Weak validation: free-form datetime, hard-coded 8 h, no minimum, no opening hours, no advance limit, no per-user pending cap. | Date + start/end form, configurable limits (students 4 h, others 8 h), hours and horizon, pending cap, rate limit. |
| 9 | Users could cancel after a booking had started; cancelling did not always free the room; no distinction for admins. | Owners: before start; admins: until end; room status recalculated; non-owners get 404. |
| 10 | QR image was available to every logged-in user; scan GET logged anonymous hits without limit; denial reason was generic; invalid-token spam unbounded. | QR images/printing admin-only; scan requires login and returns to the room page; specific denial reasons; invalid tokens rate-limited and logged; token regeneration. |
| 11 | Only one “dashboard”; no separate admin area, room/user management, audit log page; CSV exports had IDs only and were open to formula injection. | Full admin area (overview, rooms, reservations, users, history, access logs, audit logs, reports); CSV with names, filtered exports, `'` prefix for formula cells. |
| 12 | Auth gaps: case-sensitive IDs allowed look-alike duplicates; any `@` accepted as an email; 10-char minimum only; no brute-force protection; timing leak for unknown users; no way to change password. | Case-insensitive unique IDs, email regex, password policy, SQLite-backed rate limiting, dummy hash check, password change that signs out other sessions. |
| 13 | No migrations or versioning; schema changes would require deleting data. | Versioned migrations with automatic backup, tested on a copy of the original schema. |
| 14 | CSS inlined in `base.html`, inline styles everywhere, no responsive navigation, minimal accessibility. | External CSS/JS, strict Content-Security-Policy (no inline script/style), responsive sidebar, skip link, labels, focus styles, dialogs, reduced-motion support. |
| 15 | Errors could surface internals; no friendly 400/405/429/500 pages. | Generic error pages; stack traces only in server logs; tested. |
