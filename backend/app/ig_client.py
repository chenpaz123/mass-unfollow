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
from requests.adapters import HTTPAdapter

from . import config

log = logging.getLogger("mass-unfollow.ig")

# instagrapi never passes a `timeout=` to requests, and mounts no adapter
# default either — a single stalled connection (Instagram or anything
# between us and it just stops sending bytes, no error) hangs the calling
# thread forever, which wedges ig_call_lock and freezes the entire app
# (login, worker, avatars, everything) until the container is restarted.
# Mounting this adapter on every client gives every request a hard ceiling
# so a stall fails as a normal, retryable error instead.
_HTTP_TIMEOUT_SEC = 30


class _TimeoutHTTPAdapter(HTTPAdapter):
    def send(self, *args, **kwargs):
        # requests.Session.send() always passes `timeout` explicitly
        # (None when the caller didn't set one), so a plain setdefault()
        # would never fire here — the key already exists, just empty.
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _HTTP_TIMEOUT_SEC
        return super().send(*args, **kwargs)


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
    # Client.__init__ already mounted an adapter on `private` with retry-on-
    # 429/5xx built in (_configure_private_session_retry) — mounting a plain
    # adapter over it would silently replace that retry behavior, so carry
    # its retry strategy over onto our timeout-adding adapter instead of
    # dropping it.
    private_adapter = _TimeoutHTTPAdapter(max_retries=cl._build_private_session_retry_strategy())
    cl.private.mount("https://", private_adapter)
    cl.private.mount("http://", private_adapter)
    public_adapter = _TimeoutHTTPAdapter()
    cl.public.mount("https://", public_adapter)
    cl.public.mount("http://", public_adapter)
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
        # instagrapi's dumped settings never include username (only cookies/
        # auth tokens), so a client restored from disk has no .username until
        # we fetch it — account_info() doubles as the cheap call that
        # confirms the session is still valid.
        cl.username = cl.account_info().username
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
    except ChallengeRequired as e:
        # Diagnostic only for now: instagrapi has a real challenge_resolve()
        # mechanism that can drive a code-based (SMS/email) checkpoint
        # automatically, but only for some challenge types -- others
        # ("native flow" / "auth platform" redirects) have no automated path
        # at all and genuinely require the real app on a trusted device.
        # Logging exactly what Instagram sent before deciding whether
        # building that flow is even worth it for this kind of checkpoint.
        challenge = getattr(e, "challenge", None)
        log.warning(
            "ChallengeRequired for %s -- message=%r challenge=%r raw_message=%r",
            username,
            getattr(e, "message", None),
            challenge,
            getattr(e, "raw_message", None),
        )
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

    with auth_state.lock:
        auth_state.status = "logging_in"

    code = code.strip()
    cl = _new_client()
    try:
        cl.login(username, password, verification_code=code)
        _adopt_authenticated_client(cl)
        return
    except Exception as e:
        if _is_soft_login_flow_failure(cl):
            log.warning("login_flow() warm-up call failed after 2FA succeeded: %s", e)
            _adopt_authenticated_client(cl)
            return
        primary_error = e

    # Some accounts reject the classic two_factor_login endpoint outright and
    # need the newer Bloks/CAA login flow instead. instagrapi only tries that
    # fallback automatically when the *first* login call fails with
    # BadPassword — not when it fails with TwoFactorRequired, which is what
    # happens here since we already know 2FA is required. bloks_caa_login()
    # is instagrapi's own public entry point for that flow, so we drive it
    # ourselves rather than giving up after the classic path fails.
    cl2 = _new_client()
    cl2.username = username
    cl2.password = password
    try:
        outcome = cl2.bloks_caa_login(username=username, password=password, verification_code=code)
    except Exception as e2:
        log.warning("Bloks/CAA 2FA fallback also failed: %s", e2)
        outcome = {"logged_in": False, "reason": str(e2)}

    if outcome.get("logged_in") and cl2.user_id:
        _adopt_authenticated_client(cl2)
        return

    with auth_state.lock:
        auth_state.status = "error"
        auth_state.error = (
            f"2FA code rejected: {primary_error}. Make sure you're entering the current code from an "
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


def iter_own_following_pages(start_cursor: str = ""):
    """Yields (accounts_in_page, next_cursor) as pages are fetched.
    next_cursor is the cursor for the page AFTER this one -- callers should
    only persist it as a resume point once this page's accounts are
    confirmed durably saved, so an interruption always resumes by
    re-fetching the last (possibly not-yet-saved) page rather than skipping
    it. Re-fetching an already-saved page on resume is harmless (upserts
    are idempotent and never overwrite an existing decision); the point is
    resuming must never SKIP a page.

    Bypasses instagrapi's iter_user_following_v1() convenience wrapper
    (which always starts from an empty cursor) to drive pagination directly,
    since a full sync of a large following list takes minutes and a server
    restart mid-sync (e.g. a deploy) would otherwise always lose all
    progress and have to start over from scratch.

    Blocking/sync generator — call from a worker thread, not the event loop.
    """
    cl = get_client()
    cursor = start_cursor
    while True:
        users, next_cursor = cl.user_following_v1_chunk(cl.user_id, max_id=cursor)
        if not users:
            return
        yield [_to_account_dict(u) for u in users], next_cursor
        if not next_cursor or next_cursor == cursor:
            return
        cursor = next_cursor


def unfollow(user_id: str):
    cl = get_client()
    cl.user_unfollow(int(user_id))


def get_last_post_timestamp(user_id: str) -> float | None:
    """Unix timestamp of the account's most recent post, or None if they
    have no posts (or we couldn't see any — e.g. a private account with no
    visible media). Only fetches one media item, to keep this cheap."""
    cl = get_client()
    medias = cl.user_medias(int(user_id), amount=1)
    if not medias:
        return None
    return medias[0].taken_at.timestamp()


def refollow(user_id: str):
    cl = get_client()
    cl.user_follow(int(user_id))


def get_follows_back(user_id: str) -> bool | None:
    """Whether this account follows the logged-in account back. None if
    Instagram wouldn't tell us (user_friendship_v1 swallows ClientError and
    returns None itself), not just "not yet fetched"."""
    cl = get_client()
    rel = cl.user_friendship_v1(str(user_id))
    return bool(rel.followed_by) if rel is not None else None


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
