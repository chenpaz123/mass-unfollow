import hashlib
import hmac
import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config, db

COOKIE_NAME = "mu_session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 days
PBKDF2_ITERATIONS = 200_000


def _serializer() -> URLSafeTimedSerializer:
    secret = config.SECRET_KEY or "dev-insecure-secret-change-me"
    return URLSafeTimedSerializer(secret)


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
    if not config.APP_PASSWORD:
        # No app password configured at all: rely entirely on Cloudflare Access in front.
        return True
    return pw == config.APP_PASSWORD


def set_app_password(new_password: str):
    db.set_setting("app_password_hash", _hash_password(new_password))
