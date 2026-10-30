"""Small helpers: time handling, validation, redirects, pagination."""
import re
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")
COMMON_PASSWORDS = {
    "password123", "password1234", "1234567890", "qwerty12345", "letmein1234",
    "admin12345", "welcome1234", "iloveyou123", "demopass123!", "password123!",
}


def now():
    """Current local time (naive) in the configured timezone. All DB timestamps use this."""
    try:
        tz = ZoneInfo(current_app.config.get("TIMEZONE", "Asia/Manila"))
        return datetime.now(tz).replace(tzinfo=None, microsecond=0)
    except (ZoneInfoNotFoundError, KeyError):
        return datetime.now().replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(s):
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def fmt_dt(s):
    if not s:
        return "—"
    try:
        return parse_dt(s).strftime("%b %d, %Y %I:%M %p")
    except ValueError:
        return s


def fmt_date(s):
    try:
        return parse_dt(s).strftime("%a, %b %d, %Y")
    except (ValueError, TypeError):
        return s or "—"


def fmt_time(s):
    try:
        return parse_dt(s).strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return s or "—"


def safe_next(target, default):
    """Allow only same-site relative paths (prevents open redirects)."""
    if not target:
        return default
    t = target.strip()
    if (not t.startswith("/") or t.startswith("//") or "\\" in t
            or any(ord(c) < 32 for c in t)):
        return default
    p = urlparse(t)
    if p.scheme or p.netloc:
        return default
    return t


def valid_email(email):
    return bool(email) and len(email) <= 160 and bool(EMAIL_RE.match(email))


def password_problems(pw, user_id="", email="", min_len=10):
    problems = []
    if len(pw) < min_len:
        problems.append(f"at least {min_len} characters")
    if len(pw) > 128:
        problems.append("at most 128 characters")
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        problems.append("at least one letter and one number")
    low = pw.lower()
    if low in COMMON_PASSWORDS or (user_id and low == user_id.lower()) or (email and low == email.lower()):
        problems.append("not be a common password or match your ID/email")
    return problems


def clean(value, maxlen):
    return " ".join((value or "").split())[:maxlen]


def get_page(args):
    try:
        return max(1, int(args.get("page", 1)))
    except (TypeError, ValueError):
        return 1


def paginate(db, sql, params, page, per_page):
    total = db.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = min(page, pages)
    rows = db.execute(f"{sql} LIMIT ? OFFSET ?", list(params) + [per_page, (page - 1) * per_page]).fetchall()
    return rows, {"page": page, "pages": pages, "total": total, "per_page": per_page}


def like(term):
    """Escape LIKE wildcards in user input (use with ESCAPE '\\')."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
