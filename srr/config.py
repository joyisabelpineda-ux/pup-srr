"""Configuration loaded from environment variables (see .env.example)."""
import os
import secrets
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"


def _bool(name, default=False):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _dev_secret_key():
    """Development only: a random key persisted in instance/ so local sessions survive restarts."""
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    path = INSTANCE_DIR / ".secret_key"
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key
    key = secrets.token_hex(32)
    path.write_text(key, encoding="utf-8")
    return key


def load_config(overrides=None):
    env = os.environ.get("APP_ENV", "development").strip().lower()
    production = env == "production"
    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret:
        if production:
            raise RuntimeError(
                "SECRET_KEY is not set. Set a long random SECRET_KEY environment variable "
                "(for example: python -c \"import secrets; print(secrets.token_hex(32))\")."
            )
        secret = _dev_secret_key()
    elif production and len(secret) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters in production.")

    cfg = {
        "ENV_NAME": env,
        "PRODUCTION": production,
        "SECRET_KEY": secret,
        "DATABASE": os.environ.get("PUP_SRR_DB") or str(INSTANCE_DIR / "pup_srr.sqlite3"),
        "TIMEZONE": os.environ.get("APP_TIMEZONE", "Asia/Manila"),
        # Sessions / cookies
        "SESSION_COOKIE_NAME": "pup_srr_session",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": _bool("COOKIE_SECURE", production),
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=_int("SESSION_HOURS", 8)),
        "MAX_CONTENT_LENGTH": 1_000_000,
        "TRUST_PROXY": _bool("TRUST_PROXY", production),
        "PUBLIC_BASE_URL": os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        # Accounts
        "ALLOW_REGISTRATION": _bool("ALLOW_REGISTRATION", True),
        "PASSWORD_MIN_LENGTH": _int("PASSWORD_MIN_LENGTH", 10),
        "PASSWORD_HASH_METHOD": os.environ.get("PASSWORD_HASH_METHOD", "scrypt"),
        "SEED_DEMO": _bool("SEED_DEMO", not production),  # demo campuses + rooms (once per database)
        "SEED_DEMO_USERS": _bool("SEED_DEMO_USERS", not production),  # demo accounts: never enable on a public site
        "ADMIN_USER_ID": os.environ.get("ADMIN_USER_ID", "").strip(),
        "ADMIN_EMAIL": os.environ.get("ADMIN_EMAIL", "").strip().lower(),
        "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", ""),
        "ADMIN_NAME": os.environ.get("ADMIN_NAME", "System Administrator").strip(),
        # Reservation rules
        "MIN_MINUTES": _int("RES_MIN_MINUTES", 30),
        "MAX_HOURS_STUDENT": _int("RES_MAX_HOURS_STUDENT", 4),
        "MAX_HOURS_DEFAULT": _int("RES_MAX_HOURS_DEFAULT", 8),
        "OPEN_HOUR": _int("RES_OPEN_HOUR", 6),
        "CLOSE_HOUR": _int("RES_CLOSE_HOUR", 22),
        "MAX_ADVANCE_DAYS": _int("RES_MAX_ADVANCE_DAYS", 90),
        "MAX_PENDING_PER_USER": _int("RES_MAX_PENDING_PER_USER", 10),
        # Rate limiting
        "RATE_LIMIT_ENABLED": _bool("RATE_LIMIT_ENABLED", True),
        "LOGIN_MAX_FAILS": _int("LOGIN_MAX_FAILS", 5),
        "LOGIN_IP_MAX_FAILS": _int("LOGIN_IP_MAX_FAILS", 30),
        "LOGIN_WINDOW_SECONDS": _int("LOGIN_WINDOW_SECONDS", 900),
        "REGISTER_MAX_PER_HOUR": _int("REGISTER_MAX_PER_HOUR", 10),
        # Misc
        "PER_PAGE": 20,
        "ROOM_SYNC_INTERVAL": _int("ROOM_SYNC_INTERVAL", 15),
    }
    if overrides:
        cfg.update(overrides)
    return cfg
