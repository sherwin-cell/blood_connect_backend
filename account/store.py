"""Firestore side of account deletion (Admin SDK, so rules do not apply)."""

from __future__ import annotations

from typing import Iterable

from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from notifications.constants import OfferStatus, RequestStatus

IN_PROGRESS_REQUESTS = (
    RequestStatus.PENDING,
    RequestStatus.APPROVED,
    RequestStatus.ACTIVE,
    RequestStatus.MATCHED,
)

BATCH_LIMIT = 400


def _where(field: str, op: str, value) -> FieldFilter:
    return FieldFilter(field, op, value)


def collect_urls(value, prefix: str) -> list[str]:
    """Every string under `value` (nested dicts/lists) that starts with prefix."""
    found: list[str] = []
    if isinstance(value, str):
        if value.startswith(prefix):
            found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(collect_urls(item, prefix))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(collect_urls(item, prefix))
    return found


class FirestoreAccountStore:
    def __init__(self, db, image_prefix: str):
        self._db = db
        self._image_prefix = image_prefix

    # -- helpers --------------------------------------------------------

    def _owned_requests(self, uid: str):
        seen = {}
        for field in ("userId", "requesterId"):
            query = self._db.collection("blood_requests").where(filter=_where(field, "==", uid))
            for snapshot in query.stream():
                seen[snapshot.id] = snapshot
        return list(seen.values())

    def _commit(self, updates: Iterable[tuple]) -> int:
        """Apply (ref, fields) updates in batches; returns how many."""
        count = 0
        batch = self._db.batch()
        pending = 0
        for ref, fields in updates:
            batch.update(ref, fields)
            pending += 1
            count += 1
            if pending == BATCH_LIMIT:
                batch.commit()
                batch, pending = self._db.batch(), 0
        if pending:
            batch.commit()
        return count

    def _delete_query(self, query) -> int:
        count = 0
        for snapshot in query.stream():
            self._db.recursive_delete(snapshot.reference)
            count += 1
        return count

    # -- AccountStore ---------------------------------------------------

    def close_open_requests(self, uid: str, reason: str) -> int:
        updates = []
        cancelled = 0
        for snapshot in self._owned_requests(uid):
            data = snapshot.to_dict() or {}
            if str(data.get("status", "")).strip().lower() not in IN_PROGRESS_REQUESTS:
                continue
            fields = {
                "status": RequestStatus.CANCELLED,
                "cancellationReason": reason,
                "cancelledBy": uid,
                "cancelledAt": firestore.SERVER_TIMESTAMP,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
            updates.append((snapshot.reference, fields))
            cancelled += 1
            summary = self._db.collection("request_summaries").document(snapshot.id)
            if summary.get().exists:
                updates.append((summary, {
                    "status": RequestStatus.CANCELLED,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                }))
        self._commit(updates)
        return cancelled

    def withdraw_active_offers(self, uid: str) -> int:
        query = self._db.collection_group("offers").where(filter=_where("donorId", "==", uid))
        updates = []
        for snapshot in query.stream():
            status = str((snapshot.to_dict() or {}).get("status", "")).strip().lower()
            if status in OfferStatus.ACTIVE:
                updates.append((snapshot.reference, {
                    "status": OfferStatus.WITHDRAWN,
                    "withdrawReason": "Donor deleted their Blood-Connect account.",
                    "withdrawnAt": firestore.SERVER_TIMESTAMP,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                }))
        return self._commit(updates)

    def image_urls(self, uid: str) -> list[str]:
        urls: list[str] = []
        for collection in ("users", "donors", "id_verifications"):
            snapshot = self._db.collection(collection).document(uid).get()
            if snapshot.exists:
                urls.extend(collect_urls(snapshot.to_dict(), self._image_prefix))
        attempts = self._db.collection("face_verification_attempts").where(
            filter=_where("userId", "==", uid))
        for snapshot in attempts.stream():
            urls.extend(collect_urls(snapshot.to_dict(), self._image_prefix))
        return urls

    def delete_personal_data(self, uid: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for collection in ("users", "donors", "id_verifications"):
            ref = self._db.collection(collection).document(uid)
            existed = ref.get().exists
            self._db.recursive_delete(ref)  # includes fcm_tokens / application_history
            counts[collection] = int(existed)

        by_user = {
            "idVerificationsLegacy": ("id_verifications", "userId"),
            "faceVerificationAttempts": ("face_verification_attempts", "userId"),
            "faceVerificationQuota": ("face_verification_quota", "userId"),
            "notifications": ("notifications", "userId"),
        }
        for name, (collection, field) in by_user.items():
            query = self._db.collection(collection).where(filter=_where(field, "==", uid))
            counts[name] = self._delete_query(query)

        chats = self._db.collection("chats").where(
            filter=_where("participants", "array_contains", uid))
        counts["chats"] = self._delete_query(chats)
        return counts

    def record_deletion(self, uid: str, summary: dict) -> None:
        self._db.collection("account_deletions").document(uid).set({
            "deletedAt": firestore.SERVER_TIMESTAMP,
            "summary": summary,
        })
