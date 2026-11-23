"""Validate and record a "Blood available at PRC" notice (pure logic; the
Firestore store, admin check and notification writer are injected)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Union

from .model import OPEN, PENDING, is_archived, status_of, units_needed

log = logging.getLogger(__name__)

APPROVED_STATUS = "active"  # what the admin portal writes when approving
MIN_INSTRUCTIONS = 10
MAX_INSTRUCTIONS = 600
MAX_CHAT_MESSAGE = 1000


class Code:
    NOTIFIED = "notified"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    ARCHIVED = "archived"
    CLOSED = "closed"
    INVALID_UNITS = "invalid_units"
    INVALID_INSTRUCTIONS = "invalid_instructions"
    INVALID_MESSAGE = "invalid_message"
    FAILED = "failed"


@dataclass(frozen=True)
class Notice:
    reserved_units: int
    pickup_instructions: str
    chat_message: Optional[str] = None


@dataclass
class Outcome:
    status: int
    code: str
    message: str
    body: dict = field(default_factory=dict)

    def json(self) -> dict:
        return {"code": self.code, "message": self.message, **self.body}


@dataclass
class Plan:
    """What to write, in one transaction."""
    request_update: dict
    notice_entry: dict
    notice_number: int
    requester_id: str
    approve: bool
    summary: Optional[dict]  # request_summaries doc to publish, if any
    submission_id: Optional[str]  # id_verifications doc to approve, if any


def clean_notice(notice: Notice) -> Union[Notice, Outcome]:
    """Checks that do not depend on the request."""
    instructions = " ".join(str(notice.pickup_instructions or "").split())
    if not MIN_INSTRUCTIONS <= len(instructions) <= MAX_INSTRUCTIONS:
        return Outcome(400, Code.INVALID_INSTRUCTIONS,
                       f"Pickup instructions must be {MIN_INSTRUCTIONS} to "
                       f"{MAX_INSTRUCTIONS} characters.")
    message = str(notice.chat_message or "").strip() or None
    if message and len(message) > MAX_CHAT_MESSAGE:
        return Outcome(400, Code.INVALID_MESSAGE,
                       f"The chat message must be at most {MAX_CHAT_MESSAGE} characters.")
    return Notice(notice.reserved_units, instructions, message)


def plan_notice(request: Optional[dict], notice: Notice, admin_uid: str, now: datetime,
                timestamp: Any) -> Union[Plan, Outcome]:
    """`timestamp` is written for server-set times (SERVER_TIMESTAMP in production)."""
    if request is None:
        return Outcome(404, Code.NOT_FOUND, "This blood request no longer exists.")
    if is_archived(request):
        return Outcome(409, Code.ARCHIVED, "Restore this archived request before notifying the requester.")
    status = status_of(request)
    if status != PENDING and status not in OPEN:
        return Outcome(409, Code.CLOSED,
                       f'This request is "{status}", so PRC stock can no longer be offered.')
    requester_id = request.get("requesterId") or request.get("userId")
    if not requester_id:
        return Outcome(409, Code.CLOSED, "This request has no requester to notify.")

    needed = units_needed(request)
    units = notice.reserved_units
    if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= needed:
        return Outcome(400, Code.INVALID_UNITS, f"Reserve between 1 and {needed} unit(s).")

    approve = status == PENDING
    number = len(request.get("prcStockNotices") or []) + 1
    stock = {
        "reservedUnits": units,
        "pickupInstructions": notice.pickup_instructions,
        "notifiedAt": timestamp,
        "notifiedBy": admin_uid,
        "noticeCount": number,
        "chatMessageSent": False,  # set after the message is posted
    }
    update = {"prcStock": stock, "updatedAt": timestamp}
    if approve:
        # Reviewing and reserving stock in one step approves the request.
        update.update({"status": APPROVED_STATUS, "reviewedAt": timestamp, "reviewedBy": admin_uid})

    summary = None
    if approve and units < needed:
        # The rest still has to come from donors: publish the request to them
        # as the portal's Approve does. Fully covered requests stay hidden.
        summary = {
            "requestId": request.get("id"),
            "bloodType": request.get("bloodType"),
            "component": request.get("component"),
            "unitsRequired": request.get("unitsRequired"),
            "urgency": request.get("urgency"),
            "hospitalName": request.get("hospitalName"),
            "neededByDate": request.get("neededByDate") or request.get("neededBy")
            or request.get("requiredDate"),
            "status": APPROVED_STATUS,
            "createdAt": request.get("createdAt") or timestamp,
            "updatedAt": timestamp,
        }

    return Plan(
        request_update=update,
        notice_entry={
            "reservedUnits": units,
            "pickupInstructions": notice.pickup_instructions,
            # Array items cannot hold server timestamps.
            "notifiedAt": now,
            "notifiedBy": admin_uid,
            "approvedRequest": approve,
        },
        notice_number=number,
        requester_id=str(requester_id),
        approve=approve,
        summary=summary,
        submission_id=(request.get("idVerificationSubmissionId") or None) if approve else None,
    )


class PrcStockStore(Protocol):
    def is_admin(self, uid: str) -> bool: ...
    def apply(self, request_id: str, build: Callable[[Optional[dict]], Union[Plan, Outcome]]
              ) -> Union[Plan, Outcome]: ...
    def mark_chat_sent(self, request_id: str) -> None: ...
    def post_support_message(self, requester_id: str, text: str, request_id: str) -> None: ...


# (requester_id, request_id, notice_number, request, reserved_units, pickup_instructions) -> created
NotifyFn = Callable[[str, str, int, dict, int, str], bool]


class PrcStockService:
    def __init__(self, store: PrcStockStore, notify: NotifyFn,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 timestamp: Any = None):
        self._store = store
        self._notify = notify
        self._clock = clock
        self._timestamp = timestamp

    def notify(self, caller: dict, request_id: str, notice: Notice) -> Outcome:
        caller_uid = caller.get("uid")
        if not caller_uid or not (caller.get("admin") is True or self._store.is_admin(caller_uid)):
            return Outcome(403, Code.FORBIDDEN, "Only PRC administrators can send this notice.")
        cleaned = clean_notice(notice)
        if isinstance(cleaned, Outcome):
            return cleaned

        seen: dict = {}

        def build(request: Optional[dict]):
            # A transaction may retry: keep only the last read.
            seen.clear()
            if request is not None:
                seen.update(request)
            return plan_notice({**request, "id": request_id} if request else None, cleaned,
                               caller_uid, self._clock(), self._timestamp)

        try:
            result = self._store.apply(request_id, build)
        except Exception:
            log.exception("Could not record PRC stock for request %s", request_id)
            return Outcome(500, Code.FAILED, "Could not save the notice. Try again.")
        if isinstance(result, Outcome):
            return result

        # The worker pushes this record to the requester's devices (FCM).
        notified = False
        try:
            notified = self._notify(result.requester_id, request_id, result.notice_number,
                                    seen, cleaned.reserved_units, cleaned.pickup_instructions)
        except Exception:
            log.exception("Could not create the PRC stock notification for %s", request_id)

        chat_sent = False
        if cleaned.chat_message:
            try:
                self._store.post_support_message(result.requester_id, cleaned.chat_message, request_id)
                self._store.mark_chat_sent(request_id)
                chat_sent = True
            except Exception:
                log.exception("Could not post the PRC support message for %s", request_id)

        log.info("PRC stock notice %s for request %s (%s unit(s))",
                 result.notice_number, request_id, cleaned.reserved_units)
        return Outcome(200, Code.NOTIFIED, "The requester was notified.", {
            "approved": result.approve,
            "noticeNumber": result.notice_number,
            "notificationCreated": notified,
            "chatMessageSent": chat_sent,
            "publishedToDonors": result.summary is not None,
        })
