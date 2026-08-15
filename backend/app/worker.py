import asyncio
import logging
import random
from datetime import date

from instagrapi.exceptions import (
    ClientThrottledError,
    FeedbackRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    SentryBlock,
)

from . import db, ig_client, notify

log = logging.getLogger("mass-unfollow.worker")

IDLE_POLL_SEC = 20
CAP_REACHED_POLL_SEC = 60

# Instagram explicitly telling us to back off, as opposed to a one-off
# network blip or something specific to a single account. Any of these means
# stop entirely, not just retry the same account again at the normal pace --
# continuing to hammer the API while blocked is exactly the pattern that
# risks escalating a temporary block into something worse.
RATE_LIMIT_EXCEPTIONS = (PleaseWaitFewMinutes, RateLimitError, FeedbackRequired, SentryBlock, ClientThrottledError)


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
    today = date.today().isoformat()
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

    async with ig_client.ig_call_lock:
        try:
            await asyncio.to_thread(ig_client.unfollow, target["user_id"])
            db.mark_unfollowed(target["user_id"])
            db.bump_worker_progress(today, state["unfollowed_today"] + 1)
            log.info("Unfollowed %s (%s/%s today)", target["username"], state["unfollowed_today"] + 1, state["daily_cap"])
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
        except Exception as e:
            db.bump_account_fail_count(target["user_id"])
            db.bump_worker_progress(today, state["unfollowed_today"], last_error=str(e))
            log.warning("Unfollow failed for %s: %s", target["username"], e)

    delay = random.uniform(state["min_delay_sec"], state["max_delay_sec"])
    await asyncio.sleep(delay)
