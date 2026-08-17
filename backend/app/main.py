import asyncio
import csv
import io
import logging
import time
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, ig_client, notify, security, worker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mass-unfollow")

app = FastAPI(title="Mass Unfollow")

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

_sync_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup():
    global _sync_task
    db.init_db()
    try:
        if ig_client.try_restore_session():
            log.info("Restored previous Instagram session")
    except Exception:
        log.exception("Failed restoring saved session")

    # The sync task only ever lives in memory (_sync_task) -- it cannot
    # survive a restart. If sync_state was left "running" by an unclean
    # shutdown (e.g. a deploy landing mid-sync), nothing will ever move it
    # out of that state on its own. A large following list can take several
    # minutes to sync and restarts during active development happen often,
    # so rather than just erroring out and losing all progress, resume
    # automatically from the last-saved page cursor (see _sync_worker) if
    # Instagram is still authenticated -- only fall back to a plain
    # "interrupted" error if it isn't (nothing to resume with, needs a
    # human to log back in first).
    if db.get_sync_state().get("status") == "running":
        if ig_client.is_authenticated():
            log.info("Resuming interrupted sync")
            _sync_task = asyncio.create_task(_run_sync())
        else:
            db.set_sync_status(
                "error",
                error="Interrupted by a server restart — log back into Instagram, then try syncing again.",
            )

    asyncio.create_task(worker.worker_loop())


# ---------------------------------------------------------------------------
# App-level auth (defense in depth behind the Cloudflare Tunnel / Access)
# ---------------------------------------------------------------------------


def require_auth(request: Request):
    if not security.verify_cookie(request.cookies.get(security.COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Not authenticated")


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def api_login(body: LoginBody, response: Response):
    if not security.check_password(body.password):
        raise HTTPException(status_code=401, detail="Wrong password")
    response.set_cookie(
        security.COOKIE_NAME,
        security.make_cookie_value(),
        max_age=security.MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@app.get("/api/session")
def api_session(request: Request):
    return {"authenticated": security.verify_cookie(request.cookies.get(security.COOKIE_NAME))}


@app.get("/api/version")
def api_version():
    # Intentionally not behind require_auth: the page needs to check this even
    # while sitting on the login screen, and a build timestamp isn't sensitive.
    return {"version": config.APP_VERSION}


@app.get("/api/version/stream")
async def api_version_stream(request: Request):
    """One long-lived connection instead of polling. The version can't change
    within a running process (it's read once at startup), so there's nothing
    to push after the first message — the point is what happens when this
    connection drops: a real deploy restarts the container, which severs it,
    and the browser's EventSource auto-reconnects on its own. The moment it
    reconnects to the new process and gets a different version, that's a real
    update, detected the instant it happens rather than on some poll delay.
    A dropped connection from a network blip just reconnects to the same
    process and reports the same version — no false positive.
    """

    async def event_stream():
        yield f"retry: 2000\ndata: {config.APP_VERSION}\n\n"
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(15)
            yield ": keep-alive\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/logout")
def api_logout(response: Response):
    response.delete_cookie(security.COOKIE_NAME)
    return {"ok": True}


class ChangeAppPasswordBody(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/settings/app-password", dependencies=[Depends(require_auth)])
def change_app_password(body: ChangeAppPasswordBody):
    if not security.check_password(body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(body.new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")
    security.set_app_password(body.new_password)
    return {"ok": True}


@app.post("/api/settings/reset-data", dependencies=[Depends(require_auth)])
def reset_data():
    db.reset_all_accounts()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Instagram auth
# ---------------------------------------------------------------------------


class SessionIdBody(BaseModel):
    sessionid: str


class PasswordBody(BaseModel):
    username: str
    password: str


class TwoFaBody(BaseModel):
    code: str


@app.get("/api/ig/status", dependencies=[Depends(require_auth)])
def ig_status():
    snap = ig_client.auth_state.snapshot()
    snap["authenticated"] = ig_client.is_authenticated()
    snap["username"] = ig_client.get_connected_username() if snap["authenticated"] else None
    return snap


@app.post("/api/ig/login/session", dependencies=[Depends(require_auth)])
async def ig_login_session(body: SessionIdBody):
    async with ig_client.ig_call_lock:
        await asyncio.to_thread(ig_client.login_with_sessionid, body.sessionid)
    return ig_client.auth_state.snapshot()


@app.post("/api/ig/login/password", dependencies=[Depends(require_auth)])
async def ig_login_password(body: PasswordBody):
    async with ig_client.ig_call_lock:
        await asyncio.to_thread(ig_client.login_with_password, body.username, body.password)
    return ig_client.auth_state.snapshot()


@app.post("/api/ig/login/2fa", dependencies=[Depends(require_auth)])
async def ig_login_2fa(body: TwoFaBody):
    async with ig_client.ig_call_lock:
        await asyncio.to_thread(ig_client.submit_2fa_code, body.code)
    return ig_client.auth_state.snapshot()


@app.post("/api/ig/logout", dependencies=[Depends(require_auth)])
def ig_logout():
    ig_client.logout()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sync following list
# ---------------------------------------------------------------------------


def _sync_worker() -> tuple[int, int]:
    """Runs in a worker thread. Paginates through the following list, one
    Instagram page (~200 accounts) at a time, upserting and checkpointing
    the resume cursor after each page so progress is real (shown to the
    user as it happens) and survives an interruption -- a full sync of a
    large following list takes minutes, and restarting mid-sync (e.g. a
    deploy) used to always lose everything and start over from scratch.

    Resumes from the last-saved page cursor if one exists (an interrupted
    sync); resuming re-fetches that one page again (harmless -- upserts are
    idempotent and never overwrite an existing decision) rather than risk
    skipping any accounts.
    """
    state = db.get_sync_state()
    start_cursor = state.get("resume_cursor") or ""
    fetched = state.get("resume_fetched") or 0

    try:
        total = ig_client.get_own_following_count()
    except Exception:
        total = 0
    db.set_sync_status("running", fetched_count=fetched, total_count=total)

    for page, next_cursor in ig_client.iter_own_following_pages(start_cursor=start_cursor):
        batch = []
        for row in page:
            row["sort_order"] = float(fetched)
            batch.append(row)
            fetched += 1
        db.upsert_accounts(batch)
        # Checkpoint only AFTER this page's upserts are confirmed committed --
        # advancing resume_cursor any earlier risks skipping accounts if the
        # process dies mid-page.
        db.set_sync_status("running", fetched_count=fetched, total_count=total, resume_cursor=next_cursor, resume_fetched=fetched)
    return fetched, total


async def _run_sync():
    try:
        async with ig_client.ig_call_lock:
            fetched, total = await asyncio.to_thread(_sync_worker)
        db.randomize_sort_order()
        # Instagram's own following count (fetched up front, before pagination)
        # is the ground truth. A large following list can have its pagination
        # end early — Instagram just stops returning a next page, no error —
        # so `fetched` can land short of it. Keep the real total instead of
        # overwriting it with `fetched`, so that gap stays visible in the UI
        # (and tells the user a resync may still be worth trying) rather than
        # silently reporting every sync as fully complete.
        db.set_sync_status(
            "done", fetched_count=fetched, total_count=max(total, fetched), resume_cursor="", resume_fetched=0
        )
    except Exception as e:
        log.exception("Sync failed")
        # Deliberately NOT touching resume_cursor/resume_fetched here -- keep
        # whatever _sync_worker last checkpointed so the next attempt (manual
        # retry, or an auto-resume on the next restart) continues from there
        # instead of starting over.
        db.set_sync_status("error", error=str(e))


@app.post("/api/sync/start", dependencies=[Depends(require_auth)])
async def sync_start():
    global _sync_task
    if not ig_client.is_authenticated():
        raise HTTPException(status_code=400, detail="Log in to Instagram first")
    state = db.get_sync_state()
    if state.get("status") == "running":
        return {"ok": True, "already_running": True}
    _sync_task = asyncio.create_task(_run_sync())
    return {"ok": True}


@app.get("/api/sync/status", dependencies=[Depends(require_auth)])
def sync_status():
    return db.get_sync_state()


# ---------------------------------------------------------------------------
# Swipe queue
# ---------------------------------------------------------------------------


def _card(row: dict) -> dict:
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "is_private": bool(row["is_private"]),
        "is_verified": bool(row["is_verified"]),
        "avatar_url": f"/api/avatar/{row['user_id']}",
        "last_post_checked": bool(row["last_post_checked"]),
        "last_post_at": row["last_post_at"],
        "follows_back_checked": bool(row["follows_back_checked"]),
        "follows_back": bool(row["follows_back"]) if row["follows_back"] is not None else None,
    }


@app.get("/api/queue/next", dependencies=[Depends(require_auth)])
def queue_next():
    row = db.get_next_pending()
    if not row:
        return {"done": True}
    return {"done": False, "card": _card(row)}


@app.get("/api/queue/peek", dependencies=[Depends(require_auth)])
def queue_peek(limit: int = 2):
    limit = max(1, min(limit, 30))
    rows = db.peek_pending(limit=limit)
    return {"cards": [_card(r) for r in rows]}


class DecisionBody(BaseModel):
    user_id: str
    decision: str  # "keep" | "remove"


@app.post("/api/decision", dependencies=[Depends(require_auth)])
def post_decision(body: DecisionBody):
    if body.decision not in ("keep", "remove"):
        raise HTTPException(status_code=400, detail="decision must be keep or remove")
    if not db.get_account(body.user_id):
        raise HTTPException(status_code=404, detail="Unknown account")
    db.record_decision(body.user_id, body.decision)
    row = db.get_next_pending()
    return {"done": row is None, "card": _card(row) if row else None}


@app.post("/api/decision/undo", dependencies=[Depends(require_auth)])
def post_undo():
    user_id = db.undo_last_decision()
    return {"user_id": user_id}


@app.get("/api/stats", dependencies=[Depends(require_auth)])
def stats():
    return db.get_stats()


class ReorderBody(BaseModel):
    mode: str  # one of db.REORDER_MODES


@app.post("/api/queue/reorder", dependencies=[Depends(require_auth)])
def reorder_queue(body: ReorderBody):
    if body.mode not in db.REORDER_MODES:
        raise HTTPException(status_code=400, detail="invalid reorder mode")
    db.reorder_pending(body.mode)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Accounts: search + history (retroactive decisions, refollow)
# ---------------------------------------------------------------------------


def _history_card(row: dict) -> dict:
    return {
        **_card(row),
        "decision": row["decision"],
        "decided_at": row["decided_at"],
        "unfollowed_at": row["unfollowed_at"],
    }


@app.get("/api/accounts", dependencies=[Depends(require_auth)])
def list_accounts(q: str = "", decision: str = "", limit: int = 100, offset: int = 0):
    if decision and decision not in ("pending", "keep", "remove"):
        raise HTTPException(status_code=400, detail="invalid decision filter")
    limit = max(1, min(limit, 300))
    offset = max(0, offset)
    rows, total = db.search_accounts(query=q.strip(), decision=decision or None, limit=limit, offset=offset)
    return {"total": total, "accounts": [_history_card(r) for r in rows]}


class AccountDecisionBody(BaseModel):
    decision: str  # "keep" | "remove" | "pending"


@app.post("/api/accounts/{user_id}/decision", dependencies=[Depends(require_auth)])
def set_account_decision(user_id: str, body: AccountDecisionBody):
    if body.decision not in ("keep", "remove", "pending"):
        raise HTTPException(status_code=400, detail="invalid decision")
    account = db.get_account(user_id)
    if not account:
        raise HTTPException(status_code=404, detail="Unknown account")
    if account["unfollowed_at"] and body.decision != "remove":
        raise HTTPException(
            status_code=400,
            detail="This account was already unfollowed on Instagram — use refollow instead of changing the decision.",
        )
    db.update_decision_generic(user_id, body.decision)
    return {"ok": True}


@app.get("/api/export/csv", dependencies=[Depends(require_auth)])
def export_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["username", "full_name", "decision", "decided_at", "unfollowed_at", "is_private", "is_verified", "profile_url"]
    )
    for a in db.all_accounts_for_export():
        writer.writerow(
            [
                a["username"],
                a["full_name"],
                a["decision"],
                datetime.fromtimestamp(a["decided_at"]).isoformat() if a["decided_at"] else "",
                datetime.fromtimestamp(a["unfollowed_at"]).isoformat() if a["unfollowed_at"] else "",
                "yes" if a["is_private"] else "no",
                "yes" if a["is_verified"] else "no",
                f"https://www.instagram.com/{a['username']}/",
            ]
        )
    filename = f"mass-unfollow-export-{config.local_today().isoformat()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/accounts/{user_id}/refollow", dependencies=[Depends(require_auth)])
async def refollow_account(user_id: str):
    account = db.get_account(user_id)
    if not account:
        raise HTTPException(status_code=404, detail="Unknown account")
    if not account["unfollowed_at"]:
        raise HTTPException(status_code=400, detail="This account hasn't been unfollowed through this app")
    if not ig_client.is_authenticated():
        raise HTTPException(status_code=400, detail="Log in to Instagram first")
    async with ig_client.ig_call_lock:
        try:
            await asyncio.to_thread(ig_client.refollow, user_id)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Instagram refollow failed: {e}")
    db.clear_unfollowed(user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Avatars (lazily fetched from Instagram's CDN and cached to disk)
# ---------------------------------------------------------------------------


@app.get("/api/avatar/{user_id}", dependencies=[Depends(require_auth)])
async def avatar(user_id: str):
    path = config.AVATAR_DIR / f"{user_id}.jpg"
    if path.exists():
        return Response(content=path.read_bytes(), media_type="image/jpeg")

    account = db.get_account(user_id)
    if not account or not account.get("profile_pic_url"):
        raise HTTPException(status_code=404)

    content = await asyncio.to_thread(ig_client.fetch_avatar_bytes, account["profile_pic_url"])
    if not content:
        raise HTTPException(status_code=404)
    path.write_bytes(content)
    return Response(content=content, media_type="image/jpeg")


@app.get("/api/last-post/{user_id}", dependencies=[Depends(require_auth)])
async def last_post(user_id: str):
    """Lazily fetched and cached, like avatars — one Instagram lookup per
    account, ever, the first time its card is actually viewed. Soft-fails
    (200 with checked=false) instead of raising, since this is a nice-to-have
    signal for the swipe screen, not something that should surface as an
    error toast if it can't be determined right now."""
    account = db.get_account(user_id)
    if not account:
        raise HTTPException(status_code=404)
    if account["last_post_checked"]:
        return {"checked": True, "last_post_at": account["last_post_at"]}
    if not ig_client.is_authenticated():
        return {"checked": False, "error": "not_authenticated"}
    async with ig_client.ig_call_lock:
        try:
            ts = await asyncio.to_thread(ig_client.get_last_post_timestamp, user_id)
        except Exception as e:
            log.warning("last-post lookup failed for %s: %s", user_id, e)
            return {"checked": False, "error": str(e)}
    db.set_last_post_info(user_id, ts)
    return {"checked": True, "last_post_at": ts}


@app.get("/api/follows-back/{user_id}", dependencies=[Depends(require_auth)])
async def follows_back(user_id: str):
    """Lazily fetched and cached exactly like last-post: one Instagram lookup
    per account, ever, the first time its card is actually viewed."""
    account = db.get_account(user_id)
    if not account:
        raise HTTPException(status_code=404)
    if account["follows_back_checked"]:
        fb = account["follows_back"]
        return {"checked": True, "follows_back": bool(fb) if fb is not None else None}
    if not ig_client.is_authenticated():
        return {"checked": False, "error": "not_authenticated"}
    async with ig_client.ig_call_lock:
        try:
            fb = await asyncio.to_thread(ig_client.get_follows_back, user_id)
        except Exception as e:
            log.warning("follows-back lookup failed for %s: %s", user_id, e)
            return {"checked": False, "error": str(e)}
    db.set_follows_back_info(user_id, fb)
    return {"checked": True, "follows_back": fb}


# ---------------------------------------------------------------------------
# Unfollow worker controls
# ---------------------------------------------------------------------------


class WorkerConfigBody(BaseModel):
    enabled: bool | None = None
    daily_cap: int | None = None
    min_delay_sec: int | None = None
    max_delay_sec: int | None = None


@app.post("/api/worker/config", dependencies=[Depends(require_auth)])
def worker_config(body: WorkerConfigBody):
    db.update_worker_config(
        enabled=body.enabled,
        daily_cap=body.daily_cap,
        min_delay_sec=body.min_delay_sec,
        max_delay_sec=body.max_delay_sec,
    )
    return db.get_worker_state()


@app.get("/api/worker/status", dependencies=[Depends(require_auth)])
def worker_status():
    state = db.get_worker_state()
    stats_ = db.get_stats()
    state["remaining_to_unfollow"] = stats_["remove"] - stats_["unfollowed"]
    state["stuck"] = stats_["stuck"]
    return state


@app.post("/api/worker/retry-stuck", dependencies=[Depends(require_auth)])
def worker_retry_stuck():
    db.retry_stuck_accounts()
    return {"ok": True}


@app.get("/api/worker/stuck-accounts", dependencies=[Depends(require_auth)])
def worker_stuck_accounts():
    return db.get_stuck_accounts()


# ---------------------------------------------------------------------------
# Push notifications (currently just: the unfollow worker auto-pausing
# because Instagram signaled a rate limit — see worker.py)
# ---------------------------------------------------------------------------


@app.get("/api/push/vapid-public-key", dependencies=[Depends(require_auth)])
def push_vapid_public_key():
    return {"key": config.VAPID_PUBLIC_KEY}


class PushSubscribeBody(BaseModel):
    endpoint: str
    keys: dict[str, str]


@app.post("/api/push/subscribe", dependencies=[Depends(require_auth)])
def push_subscribe(body: PushSubscribeBody):
    db.add_push_subscription(body.endpoint, body.keys["p256dh"], body.keys["auth"])
    return {"ok": True}


class PushUnsubscribeBody(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe", dependencies=[Depends(require_auth)])
def push_unsubscribe(body: PushUnsubscribeBody):
    db.remove_push_subscription(body.endpoint)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
