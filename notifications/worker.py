"""The notification worker: Firestore listeners -> outbox -> FCM.

Threads:
    listener callbacks (owned by the Firestore client) only queue changes;
    "notification-events" turns changes into notification records;
    "notification-delivery" sends a push for each pending record;
    "notification-supervisor" writes a heartbeat and restarts dead listeners.

Missed events: each listener keeps the last version of every document it has
seen, so a status change is detected by comparing versions. Documents seen
for the first time in a listener's initial snapshot have no previous version;
for those, only events newer than the last heartbeat (minus a margin) are
processed, so a restart catches up on downtime without re-announcing old
history. On the very first run there is no heartbeat, so nothing old is
announced.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.base_query import FieldFilter

from . import content, events
from .config import Settings
from .constants import (
    PH_TZ,
    PRC_SUPPORT_ID,
    DonorStatus,
    NotificationType as T,
    OfferStatus,
    RequestStatus,
    Route,
)
from .delivery import Delivery, send_announcement
from .events import Intent, Kind
from .matching import DonorCandidate, Location, blood_type_matches, is_donor_eligible, select_donors
from .outbox import COLLECTION as NOTIFICATIONS, PUSH_PENDING, Outbox

log = logging.getLogger(__name__)

STATE_COLLECTION = "notification_worker"
STATE_DOC = "state"
BROADCASTS = "notification_broadcasts"


@dataclass
class _Change:
    collection: str
    ids: dict
    before: Optional[dict]
    after: dict
    initial: bool
    # Oldest event still processed for documents first seen in an initial snapshot.
    since: datetime


@dataclass
class _Watch:
    name: str
    start: Callable[["_Watch"], object]  # returns the Firestore Watch handle
    handle: object = None
    inactive_checks: int = 0
    # Window start for this listener's next initial snapshot.
    since: Optional[datetime] = None
    # Last time the supervisor saw this listener streaming.
    last_active: Optional[datetime] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parent_id(reference) -> str:
    """Id of the document that owns a subcollection document."""
    return reference.parent.parent.id


class NotificationWorker:
    def __init__(self, db, settings: Optional[Settings] = None):
        self._db = db
        self._settings = settings or Settings.from_env()
        self._outbox = Outbox(db)
        self._delivery = Delivery(db)

        self._changes: "queue.Queue[_Change]" = queue.Queue()
        self._pushes: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._queued_pushes: set[str] = set()
        self._push_lock = threading.Lock()

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._watches: list[_Watch] = []
        # Last seen version of each document, per listener.
        self._cache: dict[str, dict[str, dict]] = {}

        self._catchup_since = _utcnow()
        self._last_heartbeat: Optional[datetime] = None
        self._stats = {"changes": 0, "notifications": 0, "pushes": 0, "errors": 0}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._catchup_since = self._load_catchup_since()
        log.info("Notification worker starting; catching up from %s", self._catchup_since)

        for target, name in (
            (self._process_changes, "notification-events"),
            (self._process_pushes, "notification-delivery"),
            (self._supervise, "notification-supervisor"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        self._watches = self._build_watches()
        for watch in self._watches:
            watch.since = self._catchup_since
            self._start_watch(watch)

    def stop(self) -> None:
        self._stop.set()
        for watch in self._watches:
            self._close_watch(watch)
        for thread in self._threads:
            thread.join(timeout=5)
        log.info("Notification worker stopped")

    def status(self) -> dict:
        return {
            "listeners": {
                w.name: bool(w.handle is not None and getattr(w.handle, "is_active", False))
                for w in self._watches
            },
            "pendingChanges": self._changes.qsize(),
            "pendingPushes": self._pushes.qsize(),
            "lastHeartbeat": self._last_heartbeat.isoformat() if self._last_heartbeat else None,
            **self._stats,
        }

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    def _build_watches(self) -> list[_Watch]:
        db = self._db

        def entity(name, collection, query_fn, ids_fn):
            return _Watch(
                name=name,
                start=lambda watch: query_fn(watch).on_snapshot(
                    self._on_snapshot(watch, collection, ids_fn)
                ),
            )

        return [
            entity("donors", events.DONORS, lambda _w: db.collection("donors"),
                   lambda ref: {"donorId": ref.id}),
            entity("blood_requests", events.REQUESTS, lambda _w: db.collection("blood_requests"),
                   lambda ref: {"requestId": ref.id}),
            entity("offers", events.OFFERS, lambda _w: db.collection_group("offers"),
                   lambda ref: {"requestId": _parent_id(ref), "offerId": ref.id}),
            # Only recent messages: chat history can be large and old messages never matter.
            entity("messages", events.MESSAGES,
                   lambda w: db.collection_group("messages").where(
                       filter=FieldFilter("createdAt", ">=", w.since)),
                   lambda ref: {"chatId": _parent_id(ref), "messageId": ref.id}),
            entity("id_verifications", events.ID_VERIFICATIONS,
                   lambda _w: db.collection("id_verifications"),
                   lambda ref: {"submissionId": ref.id}),
            entity("announcements", events.ANNOUNCEMENTS,
                   lambda _w: db.collection("announcements"),
                   lambda ref: {"announcementId": ref.id}),
            _Watch(
                name="pending_pushes",
                start=lambda _w: db.collection(NOTIFICATIONS)
                .where(filter=FieldFilter("pushStatus", "==", PUSH_PENDING))
                .on_snapshot(self._on_pending_pushes),
            ),
        ]

    def _start_watch(self, watch: _Watch) -> None:
        try:
            watch.handle = watch.start(watch)
            watch.inactive_checks = 0
            watch.last_active = _utcnow()
            log.info("Listening to %s", watch.name)
        except Exception:
            self._stats["errors"] += 1
            log.exception("Could not start listener %s", watch.name)

    @staticmethod
    def _close_watch(watch: _Watch) -> None:
        if watch.handle is not None:
            try:
                watch.handle.unsubscribe()
            except Exception:
                log.debug("Listener %s did not close cleanly", watch.name, exc_info=True)
            watch.handle = None

    def _on_snapshot(self, watch: _Watch, collection: str, ids_fn):
        # The cache outlives a listener restart, so changes made while a
        # listener was down are still detected by comparing versions.
        cache = self._cache.setdefault(watch.name, {})
        state = {"initial": True}
        since = watch.since or self._catchup_since

        def callback(_docs, changes, _read_time):
            initial = state["initial"]
            state["initial"] = False
            for change in changes:
                snapshot = change.document
                path = snapshot.reference.path
                if change.type.name == "REMOVED":
                    cache.pop(path, None)
                    continue
                after = snapshot.to_dict() or {}
                before = cache.get(path)
                cache[path] = after
                self._changes.put(
                    _Change(collection, ids_fn(snapshot.reference), before, after, initial, since)
                )

        return callback

    def _on_pending_pushes(self, _docs, changes, _read_time) -> None:
        for change in changes:
            if change.type.name != "ADDED":
                continue
            snapshot = change.document
            with self._push_lock:
                if snapshot.id in self._queued_pushes:
                    continue
                self._queued_pushes.add(snapshot.id)
            self._pushes.put((snapshot.id, snapshot.to_dict() or {}))

    # ------------------------------------------------------------------
    # Catch-up window and heartbeat
    # ------------------------------------------------------------------

    def _state_ref(self):
        return self._db.collection(STATE_COLLECTION).document(STATE_DOC)

    def _load_catchup_since(self) -> datetime:
        margin = timedelta(seconds=self._settings.catchup_margin_seconds)
        try:
            snapshot = self._state_ref().get()
            last = (snapshot.to_dict() or {}).get("lastHeartbeatAt") if snapshot.exists else None
        except Exception:
            log.exception("Could not read worker state; not catching up")
            last = None
        if isinstance(last, datetime):
            return last - margin
        return _utcnow()  # first run: never announce old history

    def _supervise(self) -> None:
        interval = max(self._settings.heartbeat_seconds, 10)
        margin = timedelta(seconds=self._settings.catchup_margin_seconds)
        while not self._stop.wait(interval):
            try:
                self._state_ref().set(
                    {"lastHeartbeatAt": firestore.SERVER_TIMESTAMP, "status": self.status()},
                    merge=True,
                )
                self._last_heartbeat = _utcnow()
            except Exception:
                self._stats["errors"] += 1
                log.exception("Heartbeat failed")

            for watch in self._watches:
                active = watch.handle is not None and getattr(watch.handle, "is_active", False)
                if active:
                    watch.inactive_checks = 0
                    watch.last_active = _utcnow()
                    continue
                watch.inactive_checks += 1
                # Two failed checks in a row: the stream is gone, start a new one
                # that catches up from when it was last seen alive.
                if watch.inactive_checks >= 2:
                    log.warning("Listener %s stopped; restarting", watch.name)
                    self._close_watch(watch)
                    watch.since = (watch.last_active or self._catchup_since) - margin
                    self._start_watch(watch)

    # ------------------------------------------------------------------
    # Changes -> notification records
    # ------------------------------------------------------------------

    def _process_changes(self) -> None:
        while not self._stop.is_set():
            try:
                change = self._changes.get(timeout=1)
            except queue.Empty:
                continue
            self._stats["changes"] += 1
            try:
                for intent in events.detect(
                    change.collection, change.ids, change.before, change.after
                ):
                    if self._is_stale(change, intent):
                        continue
                    self._handle(intent)
            except Exception:
                self._stats["errors"] += 1
                log.exception("Could not process change %s", change.ids)

    def _is_stale(self, change: _Change, intent: Intent) -> bool:
        """Skip history: unseen documents in an initial snapshot, before the window."""
        if not (change.initial and change.before is None):
            return False
        return intent.occurred_at is None or intent.occurred_at < change.since

    def _notify(self, user_id, notification_type, intent, title_message, route, **context):
        title, message = title_message
        if not user_id or user_id == PRC_SUPPORT_ID:
            return
        if self._outbox.create(
            user_id=str(user_id),
            notification_type=notification_type,
            event_key=intent.key,
            title=title,
            message=message,
            route=route,
            context=context,
        ):
            self._stats["notifications"] += 1

    def _handle(self, intent: Intent) -> None:
        handler = {
            Kind.DONOR_APPLICATION_REVIEWED: self._donor_application_reviewed,
            Kind.DONOR_APPOINTMENT_CHANGED: self._donor_appointment_changed,
            Kind.ID_VERIFICATION_REVIEWED: self._id_verification_reviewed,
            Kind.REQUEST_SUBMITTED: self._request_submitted,
            Kind.REQUEST_APPROVED: self._request_approved,
            Kind.REQUEST_REJECTED: self._request_rejected,
            Kind.REQUEST_FULFILLED: self._request_closed,
            Kind.REQUEST_CANCELLED: self._request_closed,
            Kind.REQUEST_EXPIRED: self._request_closed,
            Kind.OFFER_CREATED: self._offer_created,
            Kind.OFFER_APPROVED: self._offer_approved,
            Kind.OFFER_REJECTED: self._offer_rejected,
            Kind.OFFER_DONATED: self._offer_donated,
            Kind.CHAT_MESSAGE: self._chat_message,
            Kind.ANNOUNCEMENT_PUBLISHED: self._announcement_published,
        }.get(intent.kind)
        if handler:
            handler(intent)

    # --- donors and identity -------------------------------------------

    def _donor_application_reviewed(self, intent: Intent) -> None:
        status = intent.ids["status"]
        notification_type = (
            T.DONOR_APPLICATION_APPROVED if status == DonorStatus.APPROVED
            else T.DONOR_APPLICATION_REJECTED
        )
        user_id = intent.data.get("userId") or intent.ids["donorId"]
        self._notify(user_id, notification_type, intent,
                     content.donor_application(status), Route.ACTIVITY)

    def _donor_appointment_changed(self, intent: Intent) -> None:
        user_id = intent.data.get("userId") or intent.ids["donorId"]
        self._notify(user_id, T.DONOR_APPOINTMENT, intent,
                     content.donor_appointment(intent.data), Route.ACTIVITY)

    def _id_verification_reviewed(self, intent: Intent) -> None:
        status = intent.ids["status"]
        notification_type = (
            T.ID_VERIFICATION_APPROVED if status == "approved" else T.ID_VERIFICATION_REJECTED
        )
        user_id = intent.data.get("userId") or intent.ids["submissionId"]
        self._notify(user_id, notification_type, intent,
                     content.id_verification(status), Route.ACTIVITY)

    # --- blood requests --------------------------------------------------

    @staticmethod
    def _requester_id(request: dict) -> Optional[str]:
        return request.get("requesterId") or request.get("userId")

    def _request_submitted(self, intent: Intent) -> None:
        self._notify(self._requester_id(intent.data), T.REQUEST_SUBMITTED, intent,
                     content.request_submitted(intent.data), Route.ACTIVITY,
                     requestId=intent.ids["requestId"])

    def _request_rejected(self, intent: Intent) -> None:
        self._notify(self._requester_id(intent.data), T.REQUEST_REJECTED, intent,
                     content.request_rejected(intent.data), Route.ACTIVITY,
                     requestId=intent.ids["requestId"])

    def _request_approved(self, intent: Intent) -> None:
        request_id = intent.ids["requestId"]
        self._notify(self._requester_id(intent.data), T.REQUEST_APPROVED, intent,
                     content.request_approved(intent.data), Route.REQUEST_OFFERS,
                     requestId=request_id)
        for donor in self._matching_donors(intent.data):
            self._notify(donor.user_id, T.REQUEST_OPEN, intent,
                         content.request_open(intent.data), Route.REQUEST,
                         requestId=request_id)

    def _request_closed(self, intent: Intent) -> None:
        request_id = intent.ids["requestId"]
        status = {
            Kind.REQUEST_FULFILLED: RequestStatus.FULFILLED,
            Kind.REQUEST_EXPIRED: RequestStatus.EXPIRED,
        }.get(intent.kind, RequestStatus.CANCELLED)

        # The requester cancelled it themselves, so they get no notice for that.
        if status == RequestStatus.FULFILLED:
            self._notify(self._requester_id(intent.data), T.REQUEST_FULFILLED, intent,
                         content.request_fulfilled(intent.data), Route.ACTIVITY,
                         requestId=request_id)
        elif status == RequestStatus.EXPIRED:
            # Activity tab shows the request as Expired; they can post a new one.
            self._notify(self._requester_id(intent.data), T.REQUEST_EXPIRED, intent,
                         content.request_expired(intent.data), Route.ACTIVITY,
                         requestId=request_id)

        # Donors with pending or approved offers learn the request closed.

        offers = (
            self._db.collection("blood_requests").document(request_id).collection("offers")
            .where(filter=FieldFilter("status", "in", list(OfferStatus.ACTIVE)))
            .stream()
        )
        # One notice per donor, keyed by how the request closed.
        closed = Intent(intent.kind, f"{status}_{intent.key}", intent.occurred_at)
        for offer in offers:
            donor_id = (offer.to_dict() or {}).get("donorId")
            self._notify(donor_id, T.REQUEST_CLOSED_FOR_DONOR, closed,
                         content.request_closed_for_donor(intent.data, status), Route.MY_OFFERS,
                         requestId=request_id, offerId=offer.id)

    def _matching_donors(self, request: dict) -> list[DonorCandidate]:
        requester_id = self._requester_id(request) or ""
        profile = self._db.collection("users").document(requester_id).get() if requester_id else None
        requester_location = Location.from_profile(profile.to_dict() if profile and profile.exists else None)
        if not requester_location.is_known:
            log.info("Request from %s has no location; donors not alerted", requester_id)
            return []

        today = datetime.now(PH_TZ).date()
        pre_filtered = []
        for snapshot in (
            self._db.collection("donors")
            .where(filter=FieldFilter("donorStatus", "==", DonorStatus.APPROVED))
            .stream()
        ):
            donor = snapshot.to_dict() or {}
            if blood_type_matches(request.get("bloodType"), donor.get("bloodType")) and \
                    is_donor_eligible(donor, today):
                pre_filtered.append((snapshot.id, donor))

        if not pre_filtered:
            return []

        user_ids = [donor.get("userId") or donor_id for donor_id, donor in pre_filtered]
        profiles = {
            snapshot.id: snapshot.to_dict()
            for snapshot in self._db.get_all(
                [self._db.collection("users").document(uid) for uid in set(user_ids)]
            )
            if snapshot.exists
        }
        candidates = [
            DonorCandidate(donor_id, uid, donor, Location.from_profile(profiles.get(uid)))
            for (donor_id, donor), uid in zip(pre_filtered, user_ids)
        ]
        selected = select_donors(
            request=request,
            requester_id=requester_id,
            requester_location=requester_location,
            candidates=candidates,
            today=today,
            limit=self._settings.max_fanout,
        )
        log.info("Alerting %d matching donor(s) about a %s request",
                 len(selected), request.get("bloodType"))
        return selected

    # --- offers ------------------------------------------------------------

    def _request_for_offer(self, intent: Intent) -> dict:
        snapshot = self._db.collection("blood_requests").document(intent.ids["requestId"]).get()
        return (snapshot.to_dict() or {}) if snapshot.exists else {}

    def _offer_requester_id(self, intent: Intent) -> Optional[str]:
        return intent.data.get("requesterId") or self._requester_id(self._request_for_offer(intent))

    def _offer_created(self, intent: Intent) -> None:
        self._notify(self._offer_requester_id(intent), T.OFFER_RECEIVED, intent,
                     content.offer_received(intent.data), Route.REQUEST_OFFERS,
                     requestId=intent.ids["requestId"], offerId=intent.ids["offerId"])

    def _offer_approved(self, intent: Intent) -> None:
        ids = {"requestId": intent.ids["requestId"], "offerId": intent.ids["offerId"]}
        self._notify(intent.data.get("donorId"), T.OFFER_APPROVED, intent,
                     content.offer_approved(intent.data), Route.MY_OFFERS, **ids)
        self._notify(self._offer_requester_id(intent), T.OFFER_CONFIRMED_FOR_REQUESTER, intent,
                     content.offer_confirmed_for_requester(intent.data), Route.REQUEST_OFFERS, **ids)

    def _offer_rejected(self, intent: Intent) -> None:
        self._notify(intent.data.get("donorId"), T.OFFER_REJECTED, intent,
                     content.offer_rejected(intent.data), Route.MY_OFFERS,
                     requestId=intent.ids["requestId"], offerId=intent.ids["offerId"])

    def _offer_donated(self, intent: Intent) -> None:
        self._notify(intent.data.get("donorId"), T.OFFER_DONATED, intent,
                     content.offer_donated(intent.data), Route.MY_OFFERS,
                     requestId=intent.ids["requestId"], offerId=intent.ids["offerId"])

    # --- chat ------------------------------------------------------------------

    def _sender_name(self, sender_id: str, chat: dict) -> str:
        if sender_id == PRC_SUPPORT_ID:
            return "PRC Support"
        names = chat.get("participantNames") or {}
        if names.get(sender_id):
            return str(names[sender_id])
        profile = self._db.collection("users").document(sender_id).get()
        data = (profile.to_dict() or {}) if profile.exists else {}
        full = " ".join(
            part for part in (data.get("firstName"), data.get("lastName")) if part
        )
        return str(data.get("fullName") or full or data.get("displayName") or "New message")

    def _chat_message(self, intent: Intent) -> None:
        chat_id = intent.ids["chatId"]
        chat_snapshot = self._db.collection("chats").document(chat_id).get()
        if not chat_snapshot.exists:
            return
        chat = chat_snapshot.to_dict() or {}
        sender_id = str(intent.data.get("senderId") or "")
        sender_name = self._sender_name(sender_id, chat) if sender_id else "New message"
        # In a PRC support chat, staff replies appear as "PRC Support".
        if chat.get("isPrcSupport") and sender_id not in (chat.get("participants") or []):
            sender_name = "PRC Support"

        for user_id in chat.get("participants") or []:
            if user_id in (sender_id, PRC_SUPPORT_ID):
                continue
            self._notify(user_id, T.CHAT_MESSAGE, intent,
                         content.chat_message(sender_name, intent.data), Route.CHAT,
                         chatId=chat_id, chatName=sender_name)

    # --- announcements ---------------------------------------------------------

    def _announcement_published(self, intent: Intent) -> None:
        announcement_id = intent.ids["announcementId"]
        ref = self._db.collection(BROADCASTS).document(f"announcement_{announcement_id}")
        try:
            ref.create({"announcementId": announcement_id, "state": "sending",
                        "createdAt": firestore.SERVER_TIMESTAMP})
        except AlreadyExists:
            return
        title, message = content.announcement(intent.data)
        try:
            message_id = send_announcement(announcement_id, title, message)
            ref.update({"state": "sent", "messageId": message_id,
                        "sentAt": firestore.SERVER_TIMESTAMP})
            log.info("Announcement %s pushed to all devices", announcement_id)
        except Exception:
            self._stats["errors"] += 1
            log.exception("Announcement %s push failed", announcement_id)
            ref.update({"state": "unknown", "sentAt": firestore.SERVER_TIMESTAMP})

    # ------------------------------------------------------------------
    # Pending records -> FCM
    # ------------------------------------------------------------------

    def _process_pushes(self) -> None:
        max_age = timedelta(hours=self._settings.push_max_age_hours)
        while not self._stop.is_set():
            try:
                notification_id, record = self._pushes.get(timeout=1)
            except queue.Empty:
                continue
            try:
                created = record.get("timestamp")
                if isinstance(created, datetime) and _utcnow() - created > max_age:
                    self._db.collection(NOTIFICATIONS).document(notification_id).update(
                        {"pushStatus": "expired"}
                    )
                    continue
                self._delivery.deliver(notification_id)
                self._stats["pushes"] += 1
            except Exception:
                self._stats["errors"] += 1
                log.exception("Could not deliver %s", notification_id)
            finally:
                with self._push_lock:
                    self._queued_pushes.discard(notification_id)
