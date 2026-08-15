import asyncio
import logging
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, ig_client, security, worker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mass-unfollow")

app = FastAPI(title="Mass Unfollow")

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

_sync_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup():
    db.init_db()
    try:
        if ig_client.try_restore_session():
            log.info("Restored previous Instagram session")
    except Exception:
        log.exception("Failed restoring saved session")
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


SYNC_FLUSH_EVERY = 50


def _sync_worker() -> int:
    """Runs in a worker thread. Paginates through the following list,
    flushing batches to the DB as they arrive so progress is real (shown
    to the user as it happens) and isn't lost if something fails partway."""
    try:
        total = ig_client.get_own_following_count()
    except Exception:
        total = 0
    db.set_sync_status("running", fetched_count=0, total_count=total)

    batch = []
    fetched = 0
    for row in ig_client.iter_own_following():
        row["sort_order"] = float(fetched)
        batch.append(row)
        fetched += 1
        if len(batch) >= SYNC_FLUSH_EVERY:
            db.upsert_accounts(batch)
            batch = []
            db.set_sync_status("running", fetched_count=fetched, total_count=total)
    if batch:
        db.upsert_accounts(batch)
    return fetched


async def _run_sync():
    try:
        async with ig_client.ig_call_lock:
            fetched = await asyncio.to_thread(_sync_worker)
        db.randomize_sort_order()
        db.set_sync_status("done", fetched_count=fetched, total_count=fetched)
    except Exception as e:
        log.exception("Sync failed")
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
    }


@app.get("/api/queue/next", dependencies=[Depends(require_auth)])
def queue_next():
    row = db.get_next_pending()
    if not row:
        return {"done": True}
    return {"done": False, "card": _card(row)}


@app.get("/api/queue/peek", dependencies=[Depends(require_auth)])
def queue_peek(limit: int = 2):
    limit = max(1, min(limit, 10))
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
    return state


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
