"""Send FCM pushes for notification records.

The worker listens for records with pushStatus == "pending" and calls
`Delivery.deliver(doc_id)` for each. A record is claimed in a transaction
(pending -> sending) before anything is sent, so two worker instances never
push the same record twice.

pushStatus after delivery:
    sent       every registered device accepted the message
    partial    some devices accepted it
    failed     no device accepted it
    no_tokens  the user has no registered devices
    muted      the user switched this kind of push off (see preferences.py);
               the in-app notification record is kept
    unknown    sending raised unexpectedly; not retried, to avoid duplicates

Device tokens that FCM reports as unregistered or invalid are deleted from
users/{uid}/fcm_tokens.
"""

from __future__ import annotations

import logging

from firebase_admin import exceptions as firebase_exceptions
from firebase_admin import firestore, messaging
from google.cloud.firestore_v1 import transactional

from .constants import ANDROID_CHANNEL_ID, ANNOUNCEMENTS_TOPIC
from .outbox import COLLECTION, CONTEXT_FIELDS, PUSH_PENDING
from .preferences import push_allowed

log = logging.getLogger(__name__)

FCM_BATCH_LIMIT = 500  # send_each_for_multicast maximum


def is_dead_token_error(error) -> bool:
    """True when FCM says this token will never work again."""
    if isinstance(error, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
        return True
    if isinstance(error, firebase_exceptions.InvalidArgumentError):
        return "registration token" in str(error).lower()
    return False


def _data_payload(notification_id: str, record: dict) -> dict:
    """FCM data values must all be strings."""
    data = {
        "notificationId": notification_id,
        "type": str(record.get("type") or ""),
        "route": str(record.get("route") or ""),
    }
    for name in CONTEXT_FIELDS:
        if record.get(name):
            data[name] = str(record[name])
    return data


def _android_config() -> messaging.AndroidConfig:
    return messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(channel_id=ANDROID_CHANNEL_ID),
    )


@transactional
def _claim(transaction, ref):
    snapshot = ref.get(transaction=transaction)
    if not snapshot.exists:
        return None
    record = snapshot.to_dict() or {}
    if record.get("pushStatus") != PUSH_PENDING:
        return None
    transaction.update(
        ref,
        {"pushStatus": "sending", "pushClaimedAt": firestore.SERVER_TIMESTAMP},
    )
    return record


class Delivery:
    def __init__(self, db):
        self._db = db

    def deliver(self, notification_id: str) -> None:
        ref = self._db.collection(COLLECTION).document(notification_id)
        record = _claim(self._db.transaction(), ref)
        if record is None:
            return  # already handled elsewhere, or deleted

        try:
            result = self._send_to_user(notification_id, record)
        except Exception:
            log.exception("Push for %s failed unexpectedly", notification_id)
            ref.update({"pushStatus": "unknown", "pushedAt": firestore.SERVER_TIMESTAMP})
            return

        ref.update({**result, "pushedAt": firestore.SERVER_TIMESTAMP})

    def _send_to_user(self, notification_id: str, record: dict) -> dict:
        user_id = record.get("userId")
        user_ref = self._db.collection("users").document(user_id)
        user = user_ref.get()
        if not push_allowed(record.get("type"), user.to_dict() if user.exists else None):
            return {"pushStatus": "muted", "sentCount": 0, "failedCount": 0}

        token_refs = {}
        for snapshot in (
            user_ref.collection("fcm_tokens").stream()
        ):
            token = (snapshot.to_dict() or {}).get("token")
            if token:
                token_refs.setdefault(token, []).append(snapshot.reference)

        tokens = list(token_refs)
        if not tokens:
            return {"pushStatus": "no_tokens", "sentCount": 0, "failedCount": 0}

        sent = failed = pruned = 0
        for start in range(0, len(tokens), FCM_BATCH_LIMIT):
            batch_tokens = tokens[start : start + FCM_BATCH_LIMIT]
            response = messaging.send_each_for_multicast(
                messaging.MulticastMessage(
                    tokens=batch_tokens,
                    notification=messaging.Notification(
                        title=record.get("title") or "Blood-Connect",
                        body=record.get("message") or "",
                    ),
                    data=_data_payload(notification_id, record),
                    android=_android_config(),
                )
            )
            for token, item in zip(batch_tokens, response.responses):
                if item.success:
                    sent += 1
                    continue
                failed += 1
                if is_dead_token_error(item.exception):
                    for token_ref in token_refs[token]:
                        token_ref.delete()
                        pruned += 1
                else:
                    log.warning("Push to a device of %s failed: %s", user_id, item.exception)

        status = "sent" if failed == 0 else "partial" if sent > 0 else "failed"
        if pruned:
            log.info("Removed %d expired device token(s) for %s", pruned, user_id)
        return {
            "pushStatus": status,
            "sentCount": sent,
            "failedCount": failed,
            "prunedTokenCount": pruned,
        }


def send_announcement(announcement_id: str, title: str, message: str) -> str:
    """Push an announcement to every device subscribed to the topic."""
    return messaging.send(
        messaging.Message(
            topic=ANNOUNCEMENTS_TOPIC,
            notification=messaging.Notification(title=title, body=message),
            data={"type": "announcement", "route": "home", "announcementId": announcement_id},
            android=_android_config(),
        )
    )
