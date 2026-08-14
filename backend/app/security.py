from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

COOKIE_NAME = "mu_session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 days


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


def check_password(pw: str) -> bool:
    if not config.APP_PASSWORD:
        # No app password configured: rely entirely on Cloudflare Access in front.
        return True
    return pw == config.APP_PASSWORD
