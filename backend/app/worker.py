import asyncio
import logging
import random

from instagrapi.exceptions import (
    ClientThrottledError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    SentryBlock,
)

from . import config, db, ig_client, notify

log = logging.getLogger("mass-unfollow.worker")

IDLE_POLL_SEC = 20
CAP_REACHED_POLL_SEC = 60

# Instagram explicitly telling us to back off, as opposed to a one-off
# network blip or something specific to a single account. Any of these means
# stop entirely, not just retry the same account again at the normal pace --
# continuing to hammer the API while blocked is exactly the pattern that
# risks escalating a temporary block into something worse.
RATE_LIMIT_EXCEPTIONS = (PleaseWaitFewMinutes, RateLimitError, FeedbackRequired, SentryBlock, ClientThrottledError)

# Consecutive (not total) generic failures -- e.g. instagrapi's parsing
# breaking against a new Instagram app version, the kind of thing that will
# fail identically forever until someone bumps the pin (see the README's
# troubleshooting section), as opposed to per-account issues that
# bump_account_fail_count already isolates. A module-level counter is fine
# since worker_loop is a single long-lived task within one process.
CONSECUTIVE_ERROR_LIMIT = 5
_consecutive_generic_errors = 0


async def worker_loop():
    """Runs for the lifetime of the app. Processes the 'remove' queue at a
    throttled, capped rate whenever the worker is enabled."""
    while True:
        try:
            await _tick()
        except Exception:
            log.exception("Unexpected error in worker tick")
            await asyncio.sleep(IDLE_POLL_SEC)


async def _tick():
    state = db.get_worker_state()
    today = config.local_today().isoformat()
    if state["day_marker"] != today:
        db.reset_daily_counter(today)
        state = db.get_worker_state()

    if not state["enabled"]:
        await asyncio.sleep(IDLE_POLL_SEC)
        return

    if state["unfollowed_today"] >= state["daily_cap"]:
        await asyncio.sleep(CAP_REACHED_POLL_SEC)
        return

    if not ig_client.is_authenticated():
        db.bump_worker_progress(today, state["unfollowed_today"], last_error="Not logged in to Instagram")
        await asyncio.sleep(IDLE_POLL_SEC)
        return

    target = db.get_next_to_unfollow()
    if not target:
        await asyncio.sleep(IDLE_POLL_SEC)
        return

    global _consecutive_generic_errors

    async with ig_client.ig_call_lock:
        try:
            await asyncio.to_thread(ig_client.unfollow, target["user_id"])
            db.mark_unfollowed(target["user_id"])
            db.bump_worker_progress(today, state["unfollowed_today"] + 1)
            log.info("Unfollowed %s (%s/%s today)", target["username"], state["unfollowed_today"] + 1, state["daily_cap"])
            _consecutive_generic_errors = 0
        except RATE_LIMIT_EXCEPTIONS as e:
            # Instagram itself is telling us to stop -- pause instead of just
            # retrying the same account at the normal pace, which would keep
            # hitting the same wall (and risks turning a temporary block into
            # a worse one). Not this account's fault, so its fail_count is
            # left alone -- it'll be the first thing retried once resumed.
            db.update_worker_config(enabled=False)
            db.bump_worker_progress(
                today,
                state["unfollowed_today"],
                last_error=f"Paused automatically — Instagram signaled you're doing this too fast ({e}). "
                "Wait a while before resuming.",
            )
            log.warning("Rate-limit signal from Instagram, auto-pausing: %s", e)
            await asyncio.to_thread(
                notify.send_push,
                "Unfollow worker paused",
                "Instagram signaled you're doing this too fast. Wait a while before resuming from the Queue tab.",
            )
            return
        except LoginRequired:
            # Instagram invalidated this session entirely -- e.g. it force-
            # logged-out all devices for "suspicious activity". Not this (or
            # any) account's fault, so don't touch fail_count. Log out
            # properly so is_authenticated() actually goes false and the app
            # falls back to the Connect Instagram screen, instead of every
            # subsequent account silently piling up as individually "stuck"
            # while the real cause (a dead session) stays invisible.
            ig_client.logout()
            db.update_worker_config(enabled=False)
            db.bump_worker_progress(
                today,
                state["unfollowed_today"],
                last_error="Instagram logged this session out (possibly on all devices). Reconnect Instagram to resume.",
            )
            log.warning("Instagram invalidated the session (LoginRequired) -- logged out and paused")
            await asyncio.to_thread(
                notify.send_push,
                "Instagram disconnected",
                "Instagram logged this session out. Open the app and reconnect Instagram to resume.",
            )
            return
        except Exception as e:
            fail_count = db.bump_account_fail_count(target["user_id"], error=str(e))
            db.bump_worker_progress(today, state["unfollowed_today"], last_error=str(e))
            log.warning("Unfollow failed for %s: %s", target["username"], e)
            if fail_count == db.MAX_ACCOUNT_FAIL_COUNT:
                # Fires exactly once per account, right as it crosses into
                # "stuck" (get_next_to_unfollow stops picking it up from
                # here) -- not on every failure before or after that point.
                await asyncio.to_thread(
                    notify.send_push,
                    "Account skipped after repeated errors",
                    f"@{target['username']} failed {db.MAX_ACCOUNT_FAIL_COUNT} times ({e}) and won't be "
                    "retried automatically. Check the Queue tab.",
                )

            _consecutive_generic_errors += 1
            if _consecutive_generic_errors >= CONSECUTIVE_ERROR_LIMIT:
                # Different accounts failing back-to-back with generic (not
                # rate-limit, not LoginRequired) errors points at something
                # systemic -- most likely instagrapi falling behind
                # Instagram's current app version (see README) -- rather
                # than anything specific to those accounts. Auto-pause
                # instead of grinding through the rest of the queue the
                # same way.
                _consecutive_generic_errors = 0
                db.update_worker_config(enabled=False)
                db.bump_worker_progress(
                    today,
                    state["unfollowed_today"],
                    last_error=f"Paused automatically after {CONSECUTIVE_ERROR_LIMIT} failures in a row ({e}). "
                    "instagrapi may need an update -- see the README's troubleshooting section.",
                )
                log.warning("%s consecutive generic errors, auto-pausing", CONSECUTIVE_ERROR_LIMIT)
                await asyncio.to_thread(
                    notify.send_push,
                    "Worker failing repeatedly",
                    f"{CONSECUTIVE_ERROR_LIMIT} unfollows in a row failed with the same kind of error. "
                    "instagrapi may need an update. Check the Queue tab.",
                )
                return

    delay = random.uniform(state["min_delay_sec"], state["max_delay_sec"])
    await asyncio.sleep(delay)
