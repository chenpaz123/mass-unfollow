import asyncio
import logging
import threading

import requests
from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    ClientError,
    LoginRequired,
    TwoFactorRequired,
)

from . import config

log = logging.getLogger("mass-unfollow.ig")


class AuthState:
    """Tracks login progress across the (possibly multi-step) auth flow.

    status: idle | logging_in | need_2fa | authenticated | error
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"
        self.error = ""
        self._pending_username = ""
        self._pending_password = ""

    def snapshot(self) -> dict:
        with self.lock:
            return {"status": self.status, "error": self.error}


auth_state = AuthState()
_client: Client | None = None
_client_lock = threading.Lock()

# Serializes all Instagram API calls (sync, avatar-triggered lookups, unfollow worker)
# so we never fire concurrent requests against the account.
ig_call_lock = asyncio.Lock()


def _new_client() -> Client:
    cl = Client()
    cl.delay_range = config.IG_DELAY_RANGE
    return cl


def get_client() -> Client:
    global _client
    if _client is None:
        raise RuntimeError("Not logged in")
    return _client


def is_authenticated() -> bool:
    return _client is not None and auth_state.status == "authenticated"


def get_connected_username() -> str | None:
    return _client.username if _client else None


def try_restore_session() -> bool:
    """Attempt to restore a previously saved session on startup."""
    global _client
    if not config.IG_SESSION_PATH.exists():
        return False
    cl = _new_client()
    try:
        cl.load_settings(str(config.IG_SESSION_PATH))
        cl.get_timeline_feed()  # cheap call to confirm the session is still valid
        with _client_lock:
            _client = cl
        with auth_state.lock:
            auth_state.status = "authenticated"
            auth_state.error = ""
        return True
    except Exception as e:
        log.warning("Saved session is no longer valid: %s", e)
        return False


def _save_session(cl: Client):
    cl.dump_settings(str(config.IG_SESSION_PATH))


def _adopt_authenticated_client(cl: Client):
    """Register cl as the live, authenticated client and persist its session."""
    global _client
    with _client_lock:
        _client = cl
    _save_session(cl)
    with auth_state.lock:
        auth_state.status = "authenticated"
        auth_state._pending_username = ""
        auth_state._pending_password = ""


def _is_soft_login_flow_failure(cl: Client) -> bool:
    """True if cl actually authenticated (has a user_id) despite login() raising.

    instagrapi's login_flow() makes extra "look like a real mobile app" warm-up
    calls (reels tray, timeline feed) *after* the real login already succeeded.
    Instagram frequently rejects those calls on new devices/IPs, which makes
    login() raise even though the account is fully authenticated at that point.
    """
    return bool(getattr(cl, "user_id", None))


def login_with_sessionid(sessionid: str):
    global _client
    cl = _new_client()
    with auth_state.lock:
        auth_state.status = "logging_in"
        auth_state.error = ""
    try:
        cl.login_by_sessionid(sessionid.strip())
        with _client_lock:
            _client = cl
        _save_session(cl)
        with auth_state.lock:
            auth_state.status = "authenticated"
    except Exception as e:
        with auth_state.lock:
            auth_state.status = "error"
            auth_state.error = f"Session login failed: {e}"


def login_with_password(username: str, password: str):
    global _client
    cl = _new_client()
    with auth_state.lock:
        auth_state.status = "logging_in"
        auth_state.error = ""
    try:
        cl.login(username, password)
    except TwoFactorRequired:
        with auth_state.lock:
            auth_state._pending_username = username
            auth_state._pending_password = password
            auth_state.status = "need_2fa"
        return
    except ChallengeRequired:
        with auth_state.lock:
            auth_state.status = "error"
            auth_state.error = (
                "Instagram put this login through a security checkpoint. Open Instagram "
                "in your regular browser/app, log in there and clear the checkpoint, "
                "then retry here."
            )
        return
    except BadPassword:
        with auth_state.lock:
            auth_state.status = "error"
            auth_state.error = "Incorrect username or password."
        return
    except Exception as e:
        if _is_soft_login_flow_failure(cl):
            log.warning("login_flow() warm-up call failed after real login succeeded: %s", e)
            _adopt_authenticated_client(cl)
            return
        with auth_state.lock:
            auth_state.status = "error"
            auth_state.error = f"Login failed: {e}"
        return

    _adopt_authenticated_client(cl)


def submit_2fa_code(code: str):
    global _client
    with auth_state.lock:
        username = auth_state._pending_username
        password = auth_state._pending_password
    if not username:
        with auth_state.lock:
            auth_state.status = "error"
            auth_state.error = "No login in progress."
        return

    cl = _new_client()
    try:
        cl.login(username, password, verification_code=code.strip())
        _adopt_authenticated_client(cl)
    except Exception as e:
        if _is_soft_login_flow_failure(cl):
            log.warning("login_flow() warm-up call failed after 2FA succeeded: %s", e)
            _adopt_authenticated_client(cl)
            return
        with auth_state.lock:
            auth_state.status = "error"
            auth_state.error = (
                f"2FA code rejected: {e}. Make sure you're entering the current code from an "
                "authenticator app (Google Authenticator, Authy, etc) — SMS-delivered codes won't "
                "work here. If the account only has SMS 2FA, enable an authenticator app under "
                "Instagram → Settings → Two-factor authentication first, or use the Session ID "
                "login method instead."
            )


def logout():
    global _client
    with _client_lock:
        _client = None
    with auth_state.lock:
        auth_state.status = "idle"
        auth_state.error = ""
    if config.IG_SESSION_PATH.exists():
        config.IG_SESSION_PATH.unlink()


def get_own_following_count() -> int:
    """Cheap lookup of the expected total, for a real "X / N" progress display."""
    cl = get_client()
    return cl.user_info_v1(cl.user_id).following_count


def _to_account_dict(u) -> dict:
    return {
        "user_id": str(u.pk),
        "username": u.username,
        "full_name": u.full_name or "",
        "is_private": 1 if u.is_private else 0,
        "is_verified": 1 if u.is_verified else 0,
        "profile_pic_url": str(u.profile_pic_url) if u.profile_pic_url else "",
    }


def iter_own_following():
    """Yields following accounts one at a time as they're paginated in.

    Blocking/sync generator — call from a worker thread, not the event loop.
    """
    cl = get_client()
    for u in cl.iter_user_following_v1(cl.user_id):
        yield _to_account_dict(u)


def unfollow(user_id: str):
    cl = get_client()
    cl.user_unfollow(int(user_id))


def refollow(user_id: str):
    cl = get_client()
    cl.user_follow(int(user_id))


def fetch_avatar_bytes(profile_pic_url: str) -> bytes | None:
    if not profile_pic_url:
        return None
    try:
        resp = requests.get(profile_pic_url, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        log.warning("Avatar download failed: %s", e)
        return None
