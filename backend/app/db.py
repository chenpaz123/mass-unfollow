import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


@contextmanager
def db_lock():
    with _lock:
        yield get_conn()


def init_db():
    with db_lock() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                full_name TEXT DEFAULT '',
                is_private INTEGER DEFAULT 0,
                is_verified INTEGER DEFAULT 0,
                profile_pic_url TEXT DEFAULT '',
                decision TEXT NOT NULL DEFAULT 'pending',
                decided_at REAL,
                unfollowed_at REAL,
                sort_order REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_accounts_decision_sort
                ON accounts (decision, sort_order);

            CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS worker_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                daily_cap INTEGER NOT NULL DEFAULT 150,
                min_delay_sec INTEGER NOT NULL DEFAULT 45,
                max_delay_sec INTEGER NOT NULL DEFAULT 180,
                unfollowed_today INTEGER NOT NULL DEFAULT 0,
                day_marker TEXT NOT NULL DEFAULT '',
                last_action_at REAL,
                last_error TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL DEFAULT 'idle',
                fetched_count INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                last_synced_at REAL,
                last_error TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS kv_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO worker_state (id, daily_cap, min_delay_sec, max_delay_sec) "
            "VALUES (1, ?, ?, ?)",
            (config.DEFAULT_DAILY_CAP, config.DEFAULT_MIN_DELAY_SEC, config.DEFAULT_MAX_DELAY_SEC),
        )
        conn.execute("INSERT OR IGNORE INTO sync_state (id) VALUES (1)")

        # Migrations for columns added after accounts already existed in the
        # wild — CREATE TABLE IF NOT EXISTS above is a no-op on those DBs.
        existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
        if "last_post_at" not in existing_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_post_at REAL")
        if "last_post_checked" not in existing_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_post_checked INTEGER NOT NULL DEFAULT 0")
        if "follows_back" not in existing_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN follows_back INTEGER")
        if "follows_back_checked" not in existing_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN follows_back_checked INTEGER NOT NULL DEFAULT 0")
        if "fail_count" not in existing_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
        if "last_fail_error" not in existing_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_fail_error TEXT DEFAULT ''")

        existing_sync_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sync_state)")}
        if "resume_cursor" not in existing_sync_cols:
            conn.execute("ALTER TABLE sync_state ADD COLUMN resume_cursor TEXT NOT NULL DEFAULT ''")
        if "resume_fetched" not in existing_sync_cols:
            conn.execute("ALTER TABLE sync_state ADD COLUMN resume_fetched INTEGER NOT NULL DEFAULT 0")

        conn.commit()


def upsert_accounts(rows: list[dict]):
    """rows: user_id, username, full_name, is_private, is_verified, profile_pic_url, sort_order.
    Only inserts new accounts; never overwrites an existing decision."""
    with db_lock() as conn:
        conn.executemany(
            """
            INSERT INTO accounts (user_id, username, full_name, is_private, is_verified, profile_pic_url, sort_order)
            VALUES (:user_id, :username, :full_name, :is_private, :is_verified, :profile_pic_url, :sort_order)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                is_private=excluded.is_private,
                is_verified=excluded.is_verified,
                profile_pic_url=excluded.profile_pic_url
            """,
            rows,
        )
        conn.commit()


def randomize_sort_order():
    """Reshuffle the swipe queue order after a sync completes."""
    with db_lock() as conn:
        conn.execute("UPDATE accounts SET sort_order = ABS(RANDOM())")
        conn.commit()


def set_sync_status(
    status: str,
    fetched_count: int = None,
    total_count: int = None,
    error: str = None,
    resume_cursor: str = None,
    resume_fetched: int = None,
):
    fields = ["status = ?"]
    values = [status]
    if fetched_count is not None:
        fields.append("fetched_count = ?")
        values.append(fetched_count)
    if total_count is not None:
        fields.append("total_count = ?")
        values.append(total_count)
    if error is not None:
        fields.append("last_error = ?")
        values.append(error)
    if resume_cursor is not None:
        fields.append("resume_cursor = ?")
        values.append(resume_cursor)
    if resume_fetched is not None:
        fields.append("resume_fetched = ?")
        values.append(resume_fetched)
    if status == "done":
        fields.append("last_synced_at = ?")
        values.append(time.time())
    with db_lock() as conn:
        conn.execute(f"UPDATE sync_state SET {', '.join(fields)} WHERE id = 1", values)
        conn.commit()


def get_sync_state() -> dict:
    with db_lock() as conn:
        row = conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
        return dict(row) if row else {}


def get_next_pending() -> dict | None:
    with db_lock() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE decision = 'pending' ORDER BY sort_order ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def peek_pending(limit: int = 2) -> list[dict]:
    """Non-mutating look at the next N pending accounts, in queue order."""
    with db_lock() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE decision = 'pending' ORDER BY sort_order ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def record_decision(user_id: str, decision: str):
    now = time.time()
    with db_lock() as conn:
        conn.execute(
            # Reset fail_count on every fresh decision — a new "remove" decision
            # (including re-queuing something after it previously failed
            # repeatedly and got skipped) always gets a clean slate of retries.
            "UPDATE accounts SET decision = ?, decided_at = ?, fail_count = 0 WHERE user_id = ?",
            (decision, now, user_id),
        )
        conn.execute(
            "INSERT INTO decision_log (user_id, decision, created_at) VALUES (?, ?, ?)",
            (user_id, decision, now),
        )
        conn.commit()


def undo_last_decision() -> str | None:
    with db_lock() as conn:
        row = conn.execute(
            "SELECT * FROM decision_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM decision_log WHERE id = ?", (row["id"],))
        conn.execute(
            "UPDATE accounts SET decision = 'pending', decided_at = NULL, fail_count = 0 "
            "WHERE user_id = ? AND unfollowed_at IS NULL",
            (row["user_id"],),
        )
        conn.commit()
        return row["user_id"]


# After this many failed unfollow attempts on the same account, the worker
# stops retrying it automatically (see get_next_to_unfollow) so one broken
# account (deleted, already unfollowed elsewhere, some IG-side quirk) can't
# wedge the entire queue forever. Still visible via get_stats()["stuck"] and
# recoverable with retry_stuck_accounts().
MAX_ACCOUNT_FAIL_COUNT = 3


def bump_account_fail_count(user_id: str, error: str = ""):
    with db_lock() as conn:
        conn.execute(
            "UPDATE accounts SET fail_count = fail_count + 1, last_fail_error = ? WHERE user_id = ?",
            (error, user_id),
        )
        conn.commit()


def retry_stuck_accounts():
    """Resets fail_count so accounts skipped after repeated failures get
    picked up by the worker again."""
    with db_lock() as conn:
        conn.execute(
            "UPDATE accounts SET fail_count = 0 WHERE decision = 'remove' AND unfollowed_at IS NULL"
        )
        conn.commit()


def get_stuck_accounts() -> list[dict]:
    """Accounts the worker gave up retrying (see MAX_ACCOUNT_FAIL_COUNT),
    with the error from their last attempt, so a human can go look at them
    on Instagram directly."""
    with db_lock() as conn:
        rows = conn.execute(
            "SELECT user_id, username, full_name, profile_pic_url, fail_count, last_fail_error "
            "FROM accounts WHERE decision = 'remove' AND unfollowed_at IS NULL AND fail_count >= ? "
            "ORDER BY username COLLATE NOCASE",
            (MAX_ACCOUNT_FAIL_COUNT,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    with db_lock() as conn:
        rows = conn.execute(
            "SELECT decision, COUNT(*) AS n FROM accounts GROUP BY decision"
        ).fetchall()
        counts = {r["decision"]: r["n"] for r in rows}
        unfollowed = conn.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE unfollowed_at IS NOT NULL"
        ).fetchone()["n"]
        stuck = conn.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE decision = 'remove' AND unfollowed_at IS NULL AND fail_count >= ?",
            (MAX_ACCOUNT_FAIL_COUNT,),
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "keep": counts.get("keep", 0),
            "stuck": stuck,
            "remove": counts.get("remove", 0),
            "unfollowed": unfollowed,
        }


def get_account(user_id: str) -> dict | None:
    with db_lock() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def set_last_post_info(user_id: str, last_post_at: float | None):
    """Cache a fetched last-post timestamp (or None for "confirmed no posts")
    so the same account never triggers a second Instagram lookup."""
    with db_lock() as conn:
        conn.execute(
            "UPDATE accounts SET last_post_at = ?, last_post_checked = 1 WHERE user_id = ?",
            (last_post_at, user_id),
        )
        conn.commit()


def set_follows_back_info(user_id: str, follows_back: bool | None):
    """Same lazy-cache-forever pattern as last-post: one Instagram lookup per
    account, ever, the first time its card is actually viewed."""
    with db_lock() as conn:
        conn.execute(
            "UPDATE accounts SET follows_back = ?, follows_back_checked = 1 WHERE user_id = ?",
            (follows_back, user_id),
        )
        conn.commit()


def get_worker_state() -> dict:
    with db_lock() as conn:
        row = conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
        return dict(row)


def update_worker_config(enabled: bool = None, daily_cap: int = None, min_delay_sec: int = None, max_delay_sec: int = None):
    fields, values = [], []
    if enabled is not None:
        fields.append("enabled = ?")
        values.append(1 if enabled else 0)
    if daily_cap is not None:
        fields.append("daily_cap = ?")
        values.append(daily_cap)
    if min_delay_sec is not None:
        fields.append("min_delay_sec = ?")
        values.append(min_delay_sec)
    if max_delay_sec is not None:
        fields.append("max_delay_sec = ?")
        values.append(max_delay_sec)
    if not fields:
        return
    with db_lock() as conn:
        conn.execute(f"UPDATE worker_state SET {', '.join(fields)} WHERE id = 1", values)
        conn.commit()


def get_next_to_unfollow() -> dict | None:
    with db_lock() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE decision = 'remove' AND unfollowed_at IS NULL AND fail_count < ? "
            "ORDER BY decided_at ASC LIMIT 1",
            (MAX_ACCOUNT_FAIL_COUNT,),
        ).fetchone()
        return dict(row) if row else None


def mark_unfollowed(user_id: str):
    with db_lock() as conn:
        conn.execute(
            "UPDATE accounts SET unfollowed_at = ? WHERE user_id = ?", (time.time(), user_id)
        )
        conn.commit()


def bump_worker_progress(day_marker: str, unfollowed_today: int, last_error: str = ""):
    with db_lock() as conn:
        conn.execute(
            "UPDATE worker_state SET day_marker = ?, unfollowed_today = ?, last_action_at = ?, last_error = ? WHERE id = 1",
            (day_marker, unfollowed_today, time.time(), last_error),
        )
        conn.commit()


def reset_daily_counter(day_marker: str):
    with db_lock() as conn:
        conn.execute(
            "UPDATE worker_state SET day_marker = ?, unfollowed_today = 0 WHERE id = 1",
            (day_marker,),
        )
        conn.commit()


# Maps a reorder mode to a SQL expression producing a low-to-high "tier"
# number for pending accounts — everything in tier 0 sorts before tier 1.
# Ties within a tier are broken randomly (added below), not left in whatever
# order SQLite happens to return them. Deliberately built only from columns
# already synced for every account (no extra Instagram lookups), unlike
# last-post-date or follows-back, which are fetched lazily per card and
# aren't available for the whole backlog up front.
REORDER_MODES = {
    "random": "0",
    "private_first": "CASE WHEN is_private = 1 THEN 0 ELSE 1 END",
    "no_name_first": "CASE WHEN TRIM(COALESCE(full_name, '')) = '' THEN 0 ELSE 1 END",
    "verified_last": "CASE WHEN is_verified = 1 THEN 1 ELSE 0 END",
}


def reorder_pending(mode: str):
    tier_expr = REORDER_MODES.get(mode, REORDER_MODES["random"])
    with db_lock() as conn:
        conn.execute(
            f"UPDATE accounts SET sort_order = ({tier_expr}) + (ABS(RANDOM()) / 9223372036854775808.0) "
            "WHERE decision = 'pending'"
        )
        conn.commit()


def all_accounts_for_export() -> list[dict]:
    with db_lock() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts ORDER BY COALESCE(decided_at, 0) DESC, username ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def search_accounts(
    query: str = "", decision: str | None = None, limit: int = 100, offset: int = 0
) -> tuple[list[dict], int]:
    clauses = []
    params: list = []
    if query:
        clauses.append("(username LIKE ? OR full_name LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if decision:
        clauses.append("decision = ?")
        params.append(decision)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db_lock() as conn:
        rows = conn.execute(
            f"SELECT * FROM accounts {where} "
            "ORDER BY COALESCE(decided_at, 0) DESC, username ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) AS n FROM accounts {where}", params).fetchone()["n"]
        return [dict(r) for r in rows], total


def update_decision_generic(user_id: str, decision: str):
    """Set a decision directly (used from History, unlike record_decision this
    doesn't append to decision_log — undo only ever applies to the swipe flow)."""
    decided_at = None if decision == "pending" else time.time()
    with db_lock() as conn:
        conn.execute(
            # Reset fail_count same as record_decision -- a fresh decision from
            # History (e.g. re-queuing something after it got skipped) gets a
            # clean slate of retries too.
            "UPDATE accounts SET decision = ?, decided_at = ?, fail_count = 0 WHERE user_id = ?",
            (decision, decided_at, user_id),
        )
        conn.commit()


def clear_unfollowed(user_id: str):
    with db_lock() as conn:
        conn.execute(
            "UPDATE accounts SET decision = 'keep', decided_at = ?, unfollowed_at = NULL WHERE user_id = ?",
            (time.time(), user_id),
        )
        conn.commit()


def reset_all_accounts():
    with db_lock() as conn:
        conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM decision_log")
        conn.execute(
            "UPDATE sync_state SET status='idle', fetched_count=0, total_count=0, "
            "last_synced_at=NULL, last_error=''"
        )
        conn.commit()


def get_setting(key: str, default: str | None = None) -> str | None:
    with db_lock() as conn:
        row = conn.execute("SELECT value FROM kv_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with db_lock() as conn:
        conn.execute(
            "INSERT INTO kv_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def add_push_subscription(endpoint: str, p256dh: str, auth: str):
    with db_lock() as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth",
            (endpoint, p256dh, auth, time.time()),
        )
        conn.commit()


def remove_push_subscription(endpoint: str):
    with db_lock() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()


def get_push_subscriptions() -> list[dict]:
    with db_lock() as conn:
        rows = conn.execute("SELECT * FROM push_subscriptions").fetchall()
        return [dict(r) for r in rows]
