"""Write notification records to the `notifications` collection.

Each record's document id is derived from the event and the recipient
({type}_{eventKey}_{uid}), and records are written with `create()`, which
fails if the id already exists. Replaying the same event (listener restart,
duplicate snapshot, second worker instance) therefore never creates a second
notification.

Record fields (read by the mobile NotificationsScreen):
    userId, title, message, type, route, isRead, timestamp,
    requestId / offerId / chatId / chatName (when relevant),
    pushStatus ("pending" until the delivery loop handles it).
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from typing import Optional

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists

log = logging.getLogger(__name__)

COLLECTION = "notifications"
PUSH_PENDING = "pending"

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_ID_LENGTH = 700  # Firestore allows 1500 bytes; stay well below it.

# Context fields copied onto the record so the app can route the tap.
CONTEXT_FIELDS = ("requestId", "offerId", "chatId", "chatName", "announcementId")


def notification_id(notification_type: str, event_key: str, user_id: str) -> str:
    """Deterministic, Firestore-safe document id."""
    raw = f"{notification_type}_{event_key}_{user_id}"
    safe = _UNSAFE.sub("-", raw).strip("-_") or "notification"
    if safe.startswith("__"):
        safe = "n" + safe
    return safe[:_MAX_ID_LENGTH]


class Outbox:
    def __init__(self, db, remembered: int = 20000):
        self._db = db
        self._known: OrderedDict[str, None] = OrderedDict()
        self._remembered = remembered
        self._lock = threading.Lock()

    def _seen(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id in self._known:
                self._known.move_to_end(doc_id)
                return True
            return False

    def _remember(self, doc_id: str) -> None:
        with self._lock:
            self._known[doc_id] = None
            self._known.move_to_end(doc_id)
            while len(self._known) > self._remembered:
                self._known.popitem(last=False)

    def create(
        self,
        *,
        user_id: str,
        notification_type: str,
        event_key: str,
        title: str,
        message: str,
        route: str,
        context: Optional[dict] = None,
    ) -> bool:
        """Create the record. Returns False if it already existed."""
        if not user_id:
            return False

        doc_id = notification_id(notification_type, event_key, user_id)
        # Skip the write attempt for ids this process already handled.
        if self._seen(doc_id):
            return False

        record = {
            "userId": user_id,
            "title": title,
            "message": message,
            "type": notification_type,
            "route": route,
            "isRead": False,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "pushStatus": PUSH_PENDING,
        }
        for name in CONTEXT_FIELDS:
            value = (context or {}).get(name)
            if value:
                record[name] = str(value)

        try:
            self._db.collection(COLLECTION).document(doc_id).create(record)
        except AlreadyExists:
            self._remember(doc_id)
            return False

        self._remember(doc_id)
        log.info("Notification %s created for %s", notification_type, user_id)
        return True
