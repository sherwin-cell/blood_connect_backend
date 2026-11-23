"""Detect lifecycle events from Firestore document changes.

Pure functions only (no Firestore access) so they can be unit tested.
`detect()` compares a document before and after a change and returns the
events that happened. `before` is None for a document the worker has not seen
yet: either a newly created document, or one seen for the first time in the
listener's initial snapshot (the worker filters those by time, see worker.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .constants import (
    DonorStatus,
    IdVerificationStatus,
    OfferStatus,
    RequestStatus,
)

# Collections the worker watches.
DONORS = "donors"
REQUESTS = "blood_requests"
OFFERS = "offers"
MESSAGES = "messages"
ID_VERIFICATIONS = "id_verifications"
ANNOUNCEMENTS = "announcements"


class Kind:
    """Internal event names. Each maps to a handler in worker.py."""

    DONOR_APPLICATION_REVIEWED = "donor_application_reviewed"
    DONOR_APPOINTMENT_CHANGED = "donor_appointment_changed"
    REQUEST_SUBMITTED = "request_submitted"
    REQUEST_APPROVED = "request_approved"
    REQUEST_REJECTED = "request_rejected"
    REQUEST_FULFILLED = "request_fulfilled"
    REQUEST_CANCELLED = "request_cancelled"
    REQUEST_EXPIRED = "request_expired"
    OFFER_CREATED = "offer_created"
    OFFER_APPROVED = "offer_approved"
    OFFER_REJECTED = "offer_rejected"
    OFFER_DONATED = "offer_donated"
    CHAT_MESSAGE = "chat_message"
    ID_VERIFICATION_REVIEWED = "id_verification_reviewed"
    ANNOUNCEMENT_PUBLISHED = "announcement_published"


@dataclass(frozen=True)
class Intent:
    kind: str
    # Stable key for this occurrence. Combined with the recipient it forms the
    # notification document id, so the same event never notifies twice.
    key: str
    occurred_at: Optional[datetime]
    data: dict = field(default_factory=dict)
    ids: dict = field(default_factory=dict)


def norm(value: Any) -> str:
    return str(value or "").strip().lower()


def first_time(data: dict, *fields: str) -> Optional[datetime]:
    """The first Firestore timestamp found among `fields`, or None."""
    for name in fields:
        value = data.get(name)
        if isinstance(value, datetime):
            return value
    return None


def millis(value: Optional[datetime]) -> str:
    return str(int(value.timestamp() * 1000)) if value else "0"


def detect(collection: str, ids: dict, before: Optional[dict], after: dict) -> list[Intent]:
    detector = _DETECTORS.get(collection)
    if detector is None or after is None:
        return []
    return detector(ids, before, after)


# --------------------------------------------------------------------------
# donors/{donorId}
# --------------------------------------------------------------------------

def _donor_status(data: Optional[dict]) -> str:
    if not data:
        return ""
    return norm(
        data.get("applicationStatus") or data.get("donorStatus") or data.get("status")
    )


def _appointment_key(data: Optional[dict]) -> str:
    if not data:
        return ""
    status = norm(data.get("appointmentStatus"))
    if not status:
        return ""
    appointment = data.get("appointment") or {}
    return "|".join(
        [status, str(appointment.get("date") or ""), str(appointment.get("time") or "")]
    )


def _detect_donor(ids: dict, before: Optional[dict], after: dict) -> list[Intent]:
    intents = []
    donor_id = ids["donorId"]

    status = _donor_status(after)
    if status in (DonorStatus.APPROVED, DonorStatus.REJECTED) and status != _donor_status(before):
        # reviewEventId changes on every PRC decision, so a re-review notifies again.
        review_key = after.get("reviewEventId") or millis(first_time(after, "reviewedAt"))
        intents.append(
            Intent(
                kind=Kind.DONOR_APPLICATION_REVIEWED,
                key=f"{status}_{donor_id}_{review_key}",
                occurred_at=first_time(after, "reviewedAt", "updatedAt"),
                data=after,
                ids={**ids, "status": status},
            )
        )

    appointment_key = _appointment_key(after)
    if appointment_key and appointment_key != _appointment_key(before):
        intents.append(
            Intent(
                kind=Kind.DONOR_APPOINTMENT_CHANGED,
                key=f"{donor_id}_{appointment_key}",
                occurred_at=first_time(
                    after, "appointmentUpdatedAt", "appointmentScheduledAt", "updatedAt"
                ),
                data=after,
                ids=ids,
            )
        )

    return intents


# --------------------------------------------------------------------------
# blood_requests/{requestId}
# --------------------------------------------------------------------------

_REQUEST_TRANSITIONS = {
    RequestStatus.ACTIVE: (Kind.REQUEST_APPROVED, ("reviewedAt", "updatedAt")),
    RequestStatus.APPROVED: (Kind.REQUEST_APPROVED, ("reviewedAt", "updatedAt")),
    RequestStatus.REJECTED: (Kind.REQUEST_REJECTED, ("reviewedAt", "updatedAt")),
    RequestStatus.FULFILLED: (Kind.REQUEST_FULFILLED, ("fulfilledAt", "updatedAt")),
    RequestStatus.CANCELLED: (Kind.REQUEST_CANCELLED, ("cancelledAt", "updatedAt")),
    RequestStatus.EXPIRED: (Kind.REQUEST_EXPIRED, ("expiredAt", "updatedAt")),
}


def _detect_request(ids: dict, before: Optional[dict], after: dict) -> list[Intent]:
    request_id = ids["requestId"]
    status = norm(after.get("status")) or RequestStatus.PENDING
    previous = norm(before.get("status")) if before else ""

    if before is None and status == RequestStatus.PENDING:
        return [
            Intent(
                kind=Kind.REQUEST_SUBMITTED,
                key=request_id,
                occurred_at=first_time(after, "createdAt"),
                data=after,
                ids=ids,
            )
        ]

    if status == previous or status not in _REQUEST_TRANSITIONS:
        return []

    # approved and active mean the same thing; switching between them is not news.
    if status in RequestStatus.OPEN and previous in RequestStatus.OPEN:
        return []

    kind, time_fields = _REQUEST_TRANSITIONS[status]
    return [
        Intent(
            kind=kind,
            key=request_id,
            occurred_at=first_time(after, *time_fields),
            data=after,
            ids=ids,
        )
    ]


# --------------------------------------------------------------------------
# blood_requests/{requestId}/offers/{offerId}
# --------------------------------------------------------------------------

_OFFER_TRANSITIONS = {
    OfferStatus.APPROVED: (Kind.OFFER_APPROVED, ("approvedAt", "updatedAt")),
    OfferStatus.LEGACY_ACCEPTED: (Kind.OFFER_APPROVED, ("approvedAt", "updatedAt")),
    OfferStatus.REJECTED: (Kind.OFFER_REJECTED, ("rejectedAt", "updatedAt")),
    OfferStatus.DONATED: (Kind.OFFER_DONATED, ("donatedAt", "updatedAt")),
}


def _detect_offer(ids: dict, before: Optional[dict], after: dict) -> list[Intent]:
    offer_id = ids["offerId"]
    status = norm(after.get("status")) or OfferStatus.PENDING
    previous = norm(before.get("status")) if before else ""

    if before is None and status == OfferStatus.PENDING:
        return [
            Intent(
                kind=Kind.OFFER_CREATED,
                key=offer_id,
                occurred_at=first_time(after, "createdAt"),
                data=after,
                ids=ids,
            )
        ]

    if status == previous or status not in _OFFER_TRANSITIONS:
        return []

    kind, time_fields = _OFFER_TRANSITIONS[status]
    return [
        Intent(
            kind=kind,
            key=offer_id,
            occurred_at=first_time(after, *time_fields),
            data=after,
            ids=ids,
        )
    ]


# --------------------------------------------------------------------------
# chats/{chatId}/messages/{messageId}
# --------------------------------------------------------------------------

def _detect_message(ids: dict, before: Optional[dict], after: dict) -> list[Intent]:
    if before is not None:
        return []  # only new messages matter
    return [
        Intent(
            kind=Kind.CHAT_MESSAGE,
            key=ids["messageId"],
            occurred_at=first_time(after, "createdAt", "timestamp"),
            data=after,
            ids=ids,
        )
    ]


# --------------------------------------------------------------------------
# id_verifications/{userId}
# --------------------------------------------------------------------------

def _detect_id_verification(ids: dict, before: Optional[dict], after: dict) -> list[Intent]:
    status = norm(after.get("status"))
    previous = norm(before.get("status")) if before else ""
    if status not in (IdVerificationStatus.APPROVED, IdVerificationStatus.REJECTED):
        return []
    if status == previous:
        return []
    occurred_at = first_time(after, "reviewedAt", "updatedAt")
    return [
        Intent(
            kind=Kind.ID_VERIFICATION_REVIEWED,
            # A user can be rejected, resubmit and be reviewed again.
            key=f"{status}_{ids['submissionId']}_{millis(occurred_at)}",
            occurred_at=occurred_at,
            data=after,
            ids={**ids, "status": status},
        )
    ]


# --------------------------------------------------------------------------
# announcements/{announcementId}
# --------------------------------------------------------------------------

def is_announcement_published(data: Optional[dict]) -> bool:
    if not data:
        return False
    if norm(data.get("status")) in ("draft", "archived", "deleted", "scheduled"):
        return False
    if data.get("isPublished") is False or data.get("isActive") is False:
        return False
    if data.get("archivedAt") or data.get("deletedAt"):
        return False
    return bool(data.get("title") or data.get("message") or data.get("content"))


def _detect_announcement(ids: dict, before: Optional[dict], after: dict) -> list[Intent]:
    if not is_announcement_published(after) or is_announcement_published(before):
        return []
    return [
        Intent(
            kind=Kind.ANNOUNCEMENT_PUBLISHED,
            key=ids["announcementId"],
            occurred_at=first_time(after, "publishedAt", "createdAt", "updatedAt"),
            data=after,
            ids=ids,
        )
    ]


_DETECTORS = {
    DONORS: _detect_donor,
    REQUESTS: _detect_request,
    OFFERS: _detect_offer,
    MESSAGES: _detect_message,
    ID_VERIFICATIONS: _detect_id_verification,
    ANNOUNCEMENTS: _detect_announcement,
}
