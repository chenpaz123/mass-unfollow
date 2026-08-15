import json
import logging

from pywebpush import WebPushException, webpush

from . import config, db

log = logging.getLogger("mass-unfollow.notify")


def send_push(title: str, body: str):
    """Best-effort: a failure here should never take down whatever
    triggered it (e.g. the unfollow worker). Prunes subscriptions Instagram's
    push service itself reports as gone (uninstalled PWA, revoked
    permission) so we stop retrying them; anything else is just logged."""
    for sub in db.get_push_subscriptions():
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=str(config.VAPID_PRIVATE_KEY_PATH),
                vapid_claims={"sub": "mailto:admin@localhost"},
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                db.remove_push_subscription(sub["endpoint"])
            else:
                log.warning("Push notification failed: %s", e)
        except Exception:
            log.exception("Unexpected error sending push notification")
