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


def set_sync_status(status: str, fetched_count: int = None, total_count: int = None, error: str = None):
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
            "UPDATE accounts SET decision = ?, decided_at = ? WHERE user_id = ?",
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
            "UPDATE accounts SET decision = 'pending', decided_at = NULL WHERE user_id = ? AND unfollowed_at IS NULL",
            (row["user_id"],),
        )
        conn.commit()
        return row["user_id"]


def get_stats() -> dict:
    with db_lock() as conn:
        rows = conn.execute(
            "SELECT decision, COUNT(*) AS n FROM accounts GROUP BY decision"
        ).fetchall()
        counts = {r["decision"]: r["n"] for r in rows}
        unfollowed = conn.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE unfollowed_at IS NOT NULL"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "keep": counts.get("keep", 0),
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
            "SELECT * FROM accounts WHERE decision = 'remove' AND unfollowed_at IS NULL "
            "ORDER BY decided_at ASC LIMIT 1"
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
            "UPDATE accounts SET decision = ?, decided_at = ? WHERE user_id = ?",
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
