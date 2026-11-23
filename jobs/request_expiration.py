"""Expire open blood requests that are past their needed-by date or too old.

A request that is open to donors (approved, or its legacy alias active) is
marked expired as soon as either is true:

- its needed-by date is before today in Philippines time, so a request
  needed "by Nov 23" stays open for all of Nov 23; or
- MAX_REQUEST_AGE (7 days) has passed since its createdAt.

A request with neither a needed-by date nor createdAt never expires.

The request and its donor-facing request_summaries entry are updated in one
transaction, the same way the app closes them on cancel and the admin app on
fulfil. The request is re-read inside the transaction, so a request PRC
fulfils or the requester cancels in the meantime is left alone.

The notification worker sees the status change and notifies the requester
(request_expired, opening the Activity tab) and every donor with a pending or
approved offer (request_closed_for_donor, opening My offers).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from notifications.config import _bool, _int
from notifications.constants import PH_TZ, RequestStatus

log = logging.getLogger(__name__)

REQUESTS = "blood_requests"
SUMMARIES = "request_summaries"

# Statuses that expire. Pending (not yet reviewed by PRC) and matched requests
# are left for PRC to resolve.
EXPIRABLE_STATUSES = RequestStatus.OPEN

# Where the needed-by date may be stored, newest field first. The app writes
# neededByDate (and requiredDate); older data may use the others.
NEEDED_BY_FIELDS = ("neededByDate", "neededBy", "requiredDate")

# Open requests expire this long after createdAt, even if the needed-by date
# is later (or missing).
MAX_REQUEST_AGE = timedelta(days=7)

MIN_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class ExpirationSettings:
    # Set REQUEST_EXPIRATION_ENABLED=false to stop expiring requests.
    enabled: bool = True
    # How often overdue requests are checked. Use a short value such as 15
    # when testing; once an hour is plenty in production.
    interval_seconds: int = 3600

    @staticmethod
    def from_env() -> "ExpirationSettings":
        return ExpirationSettings(
            enabled=_bool("REQUEST_EXPIRATION_ENABLED", True),
            interval_seconds=max(
                _int("REQUEST_EXPIRATION_INTERVAL_SECONDS", 3600), MIN_INTERVAL_SECONDS
            ),
        )


# --------------------------------------------------------------------------
# Pure rules (unit tested)
# --------------------------------------------------------------------------


def ph_today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(PH_TZ).date()


def _aware(value: datetime) -> datetime:
    # Firestore timestamps arrive timezone-aware (UTC); treat naive as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _to_ph_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return _aware(value).astimezone(PH_TZ).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            if len(text) == 10:  # YYYY-MM-DD: already a calendar date
                return date.fromisoformat(text)
            return _to_ph_date(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


def created_at(request: dict) -> Optional[datetime]:
    """When the request was created (timezone-aware), or None if unknown."""
    value = request.get("createdAt")
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str) and value.strip():
        try:
            return _aware(datetime.fromisoformat(value.strip()))
        except ValueError:
            return None
    return None


def needed_by_date(request: dict) -> Optional[date]:
    """The request's needed-by date in PH time, or None if it has none."""
    for field in NEEDED_BY_FIELDS:
        parsed = _to_ph_date(request.get(field))
        if parsed is not None:
            return parsed
    return None


def expiry_reason(request: Optional[dict], now: datetime) -> Optional[str]:
    """Why an open `request` should expire at `now`: "needed_by", "max_age",
    or None if it is not open or not overdue."""
    if not request:
        return None
    status = str(request.get("status") or "").strip().lower()
    if status not in EXPIRABLE_STATUSES:
        return None
    needed_by = needed_by_date(request)
    if needed_by is not None and needed_by < ph_today(now):
        return "needed_by"
    created = created_at(request)
    if created is not None and _aware(now) >= created + MAX_REQUEST_AGE:
        return "max_age"
    return None


def is_overdue(request: Optional[dict], now: datetime) -> bool:
    """True when `request` is open to donors and past its needed-by date or
    MAX_REQUEST_AGE old, whichever comes first."""
    return expiry_reason(request, now) is not None


def expiration_updates(
    request: Optional[dict], summary_exists: bool, now: datetime, timestamp: Any
) -> Optional[tuple[dict, Optional[dict]]]:
    """(request update, summary update or None), or None to leave it alone."""
    if not is_overdue(request, now):
        return None
    request_update = {
        "status": RequestStatus.EXPIRED,
        "expiredAt": timestamp,
        "updatedAt": timestamp,
    }
    summary_update = (
        {"status": RequestStatus.EXPIRED, "updatedAt": timestamp} if summary_exists else None
    )
    return request_update, summary_update


# --------------------------------------------------------------------------
# Firestore
# --------------------------------------------------------------------------


@firestore.transactional
def _expire_in_transaction(transaction, request_ref, summary_ref, now: datetime) -> bool:
    request = request_ref.get(transaction=transaction)
    summary = summary_ref.get(transaction=transaction)
    updates = expiration_updates(
        request.to_dict() if request.exists else None,
        summary.exists,
        now,
        firestore.SERVER_TIMESTAMP,
    )
    if updates is None:
        return False
    request_update, summary_update = updates
    transaction.update(request_ref, request_update)
    if summary_update is not None:
        transaction.update(summary_ref, summary_update)
    return True


class RequestExpirer:
    """Runs the expiration check on a background thread every interval."""

    def __init__(
        self,
        db,
        settings: Optional[ExpirationSettings] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._db = db
        self._settings = settings or ExpirationSettings.from_env()
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run: Optional[datetime] = None
        self._stats = {"runs": 0, "expired": 0, "errors": 0}

    def start(self) -> None:
        log.info(
            "Request expiration starting; checking every %ss", self._settings.interval_seconds
        )
        self._thread = threading.Thread(target=self._loop, name="request-expiration", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def status(self) -> dict:
        return {
            "intervalSeconds": self._settings.interval_seconds,
            "lastRunAt": self._last_run.isoformat() if self._last_run else None,
            **self._stats,
        }

    def _loop(self) -> None:
        # Check once at startup, then every interval.
        while True:
            try:
                self.run_once()
            except Exception:
                self._stats["errors"] += 1
                log.exception("Request expiration check failed")
            if self._stop.wait(self._settings.interval_seconds):
                return

    def run_once(self) -> list[str]:
        """Expire every overdue open request; returns the expired request ids."""
        now = self._clock()
        candidates = (
            self._db.collection(REQUESTS)
            .where(filter=FieldFilter("status", "in", list(EXPIRABLE_STATUSES)))
            .stream()
        )

        expired = []
        for snapshot in candidates:
            reason = expiry_reason(snapshot.to_dict(), now)
            if reason is None:
                continue
            try:
                if self._expire(snapshot.id, now):
                    expired.append(snapshot.id)
                    log.info("Expired blood request %s (%s)", snapshot.id, reason)
            except Exception:
                self._stats["errors"] += 1
                log.exception("Could not expire blood request %s", snapshot.id)

        self._stats["runs"] += 1
        self._stats["expired"] += len(expired)
        self._last_run = self._clock()
        return expired

    def _expire(self, request_id: str, now: datetime) -> bool:
        return _expire_in_transaction(
            self._db.transaction(),
            self._db.collection(REQUESTS).document(request_id),
            self._db.collection(SUMMARIES).document(request_id),
            now,
        )
