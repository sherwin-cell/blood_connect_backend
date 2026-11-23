"""Firestore side of PRC stock notices."""

from __future__ import annotations

from typing import Callable, Optional, Union

from firebase_admin import firestore

from notifications import content
from notifications.constants import PRC_SUPPORT_ID, NotificationType, Route
from notifications.outbox import Outbox

from .service import NotifyFn, Outcome, Plan

REQUESTS = "blood_requests"
SUMMARIES = "request_summaries"
ID_VERIFICATIONS = "id_verifications"
CHATS = "chats"


def support_chat_id(uid: str) -> str:
    """Same id as the mobile app and firestore.rules isOwnSupportChat()."""
    return f"{uid}_prc_support"


class FirestorePrcStockStore:
    def __init__(self, db):
        self._db = db

    def is_admin(self, uid: str) -> bool:
        """Same check as firestore.rules isAdmin() (role on users/{uid})."""
        snapshot = self._db.collection("users").document(uid).get()
        return snapshot.exists and (snapshot.to_dict() or {}).get("role") == "admin"

    def apply(self, request_id: str, build: Callable[[Optional[dict]], Union[Plan, Outcome]]
              ) -> Union[Plan, Outcome]:
        request_ref = self._db.collection(REQUESTS).document(request_id)

        @firestore.transactional
        def run(transaction):
            snapshot = request_ref.get(transaction=transaction)
            result = build(snapshot.to_dict() if snapshot.exists else None)
            if isinstance(result, Outcome):
                return result
            transaction.update(request_ref, {
                **result.request_update,
                "prcStockNotices": firestore.ArrayUnion([result.notice_entry]),
            })
            if result.summary is not None:
                transaction.set(self._db.collection(SUMMARIES).document(request_id), result.summary)
            if result.submission_id:
                transaction.update(
                    self._db.collection(ID_VERIFICATIONS).document(result.submission_id),
                    {"status": "approved", "updatedAt": firestore.SERVER_TIMESTAMP},
                )
            return result

        return run(self._db.transaction())

    def mark_chat_sent(self, request_id: str) -> None:
        self._db.collection(REQUESTS).document(request_id).update(
            {"prcStock.chatMessageSent": True})

    def _user_name(self, uid: str) -> str:
        snapshot = self._db.collection("users").document(uid).get()
        data = (snapshot.to_dict() or {}) if snapshot.exists else {}
        full = " ".join(p for p in (data.get("firstName"), data.get("lastName")) if p)
        return str(data.get("fullName") or full or data.get("displayName") or "User")

    def post_support_message(self, requester_id: str, text: str, request_id: str) -> None:
        """A PRC Support message in the requester's support chat, created the
        way the mobile app creates it if they never opened it."""
        chat_ref = self._db.collection(CHATS).document(support_chat_id(requester_id))
        exists = chat_ref.get().exists
        batch = self._db.batch()
        batch.set(chat_ref.collection("messages").document(), {
            "senderId": PRC_SUPPORT_ID,
            "message": text,
            "requestId": request_id,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        preview = {"lastMessage": f"Admin: {text}", "lastMessageTime": firestore.SERVER_TIMESTAMP}
        if exists:
            batch.update(chat_ref, preview)
        else:
            name = self._user_name(requester_id)
            batch.set(chat_ref, {
                **preview,
                "chatId": chat_ref.id,
                "userId": requester_id,
                "userName": name,
                "isPrcSupport": True,
                "participants": [requester_id, PRC_SUPPORT_ID],
                "participantNames": {requester_id: name, PRC_SUPPORT_ID: "PRC Support"},
            })
        batch.commit()


def outbox_notifier(db) -> NotifyFn:
    """Writes the in-app notification; the worker's delivery loop sends the push."""
    outbox = Outbox(db)

    def notify(requester_id: str, request_id: str, notice_number: int, request: dict,
               units: int, instructions: str) -> bool:
        title, message = content.prc_stock_available(request, units, instructions)
        return outbox.create(
            user_id=requester_id,
            notification_type=NotificationType.PRC_STOCK_AVAILABLE,
            # One record per notice: a second notice (more units, new
            # instructions) notifies again; a retried call does not.
            event_key=f"{request_id}_{notice_number}",
            title=title,
            message=message,
            route=Route.ACTIVITY,
            context={"requestId": request_id},
        )

    return notify
