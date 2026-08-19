import hashlib
import hmac
import os
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config, db

COOKIE_NAME = "mu_session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 days
PBKDF2_ITERATIONS = 200_000

# Values straight out of .env.example -- if either SECRET_KEY or APP_PASSWORD
# is still set to one of these, the operator forgot to actually generate/set
# their own, so it's treated the same as unset rather than trusted as real.
PLACEHOLDER_VALUES = {"change-me", "change-me-too"}


def _serializer() -> URLSafeTimedSerializer:
    if not config.SECRET_KEY or config.SECRET_KEY in PLACEHOLDER_VALUES:
        raise RuntimeError(
            "SECRET_KEY is missing or still the placeholder from .env.example. "
            'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))" '
            "and set it in .env before running the app."
        )
    return URLSafeTimedSerializer(config.SECRET_KEY)


def make_cookie_value() -> str:
    return _serializer().dumps({"ok": True})


def verify_cookie(value: str | None) -> bool:
    if not value:
        return False
    try:
        _serializer().loads(value, max_age=MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{dk.hex()}"


def _verify_hash(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def check_password(pw: str) -> bool:
    stored_hash = db.get_setting("app_password_hash")
    if stored_hash:
        return _verify_hash(pw, stored_hash)
    if not config.APP_PASSWORD or config.APP_PASSWORD in PLACEHOLDER_VALUES:
        # No real app password configured: rely entirely on Cloudflare Access in front.
        return True
    return hmac.compare_digest(pw.encode(), config.APP_PASSWORD.encode())


def set_app_password(new_password: str):
    db.set_setting("app_password_hash", _hash_password(new_password))


# ---------------------------------------------------------------------------
# Login brute-force throttling
# ---------------------------------------------------------------------------
# In-memory (single process) is enough here: this app runs as one uvicorn
# worker per instance, and losing the counters on a restart just means a
# clean slate, not a bypass of anything persistent.

_LOCKOUT_THRESHOLD = 5
_LOCKOUT_WINDOW_SEC = 15 * 60
_failed_attempts: dict[str, list[float]] = {}


def seconds_until_unlocked(client_key: str) -> int | None:
    """None if not locked out, else how many seconds until the next attempt
    is allowed. Locked out once >= _LOCKOUT_THRESHOLD failures have happened
    for this key within the trailing _LOCKOUT_WINDOW_SEC (a rolling window,
    not a fixed lockout duration -- it self-clears as old failures age out)."""
    now = time.time()
    attempts = [t for t in _failed_attempts.get(client_key, []) if now - t < _LOCKOUT_WINDOW_SEC]
    _failed_attempts[client_key] = attempts
    if len(attempts) < _LOCKOUT_THRESHOLD:
        return None
    return max(1, int(_LOCKOUT_WINDOW_SEC - (now - attempts[0])))


def record_failed_login(client_key: str):
    _failed_attempts.setdefault(client_key, []).append(time.time())


def record_successful_login(client_key: str):
    _failed_attempts.pop(client_key, None)
