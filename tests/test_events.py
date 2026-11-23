"""Unit tests for event detection. Run: python -m unittest discover tests"""

import unittest
from datetime import datetime, timezone

from notifications import events
from notifications.events import Kind, detect

T1 = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


def kinds(intents):
    return [intent.kind for intent in intents]


class DonorEvents(unittest.TestCase):
    ids = {"donorId": "d1"}

    def test_approval_notifies_once(self):
        before = {"applicationStatus": "pending"}
        after = {"applicationStatus": "approved", "reviewEventId": "r1", "reviewedAt": T1}
        intents = detect(events.DONORS, self.ids, before, after)
        self.assertEqual(kinds(intents), [Kind.DONOR_APPLICATION_REVIEWED])
        self.assertEqual(intents[0].ids["status"], "approved")
        self.assertIn("r1", intents[0].key)
        # Unrelated edit afterwards: nothing new.
        self.assertEqual(detect(events.DONORS, self.ids, after, {**after, "fullName": "X"}), [])

    def test_new_review_event_has_new_key(self):
        first = detect(events.DONORS, self.ids, {"applicationStatus": "pending"},
                       {"applicationStatus": "rejected", "reviewEventId": "r1"})
        second = detect(events.DONORS, self.ids, {"applicationStatus": "pending"},
                        {"applicationStatus": "rejected", "reviewEventId": "r2"})
        self.assertNotEqual(first[0].key, second[0].key)

    def test_pending_application_is_not_news(self):
        self.assertEqual(detect(events.DONORS, self.ids, None, {"applicationStatus": "pending"}), [])

    def test_appointment_scheduled_and_rescheduled(self):
        scheduled = {
            "applicationStatus": "approved",
            "appointmentStatus": "scheduled",
            "appointment": {"date": "2026-12-01", "time": "09:00"},
            "appointmentScheduledAt": T1,
        }
        before = {"applicationStatus": "approved"}
        self.assertEqual(kinds(detect(events.DONORS, self.ids, before, scheduled)),
                         [Kind.DONOR_APPOINTMENT_CHANGED])
        moved = {**scheduled, "appointment": {"date": "2026-12-02", "time": "09:00"}}
        self.assertEqual(kinds(detect(events.DONORS, self.ids, scheduled, moved)),
                         [Kind.DONOR_APPOINTMENT_CHANGED])
        self.assertEqual(detect(events.DONORS, self.ids, moved, dict(moved)), [])


class RequestEvents(unittest.TestCase):
    ids = {"requestId": "req1"}

    def test_new_pending_request_is_submitted(self):
        intents = detect(events.REQUESTS, self.ids, None, {"status": "pending", "createdAt": T1})
        self.assertEqual(kinds(intents), [Kind.REQUEST_SUBMITTED])
        self.assertEqual(intents[0].occurred_at, T1)

    def test_status_transitions(self):
        cases = {
            "active": Kind.REQUEST_APPROVED,
            "rejected": Kind.REQUEST_REJECTED,
            "fulfilled": Kind.REQUEST_FULFILLED,
            "cancelled": Kind.REQUEST_CANCELLED,
        }
        for status, kind in cases.items():
            with self.subTest(status=status):
                intents = detect(events.REQUESTS, self.ids, {"status": "pending"},
                                 {"status": status, "updatedAt": T2})
                self.assertEqual(kinds(intents), [kind])

    def test_approved_to_active_is_not_a_second_approval(self):
        self.assertEqual(
            detect(events.REQUESTS, self.ids, {"status": "approved"}, {"status": "active"}), []
        )

    def test_matched_is_silent(self):
        self.assertEqual(
            detect(events.REQUESTS, self.ids, {"status": "active"}, {"status": "matched"}), []
        )

    def test_expiry_notifies_once(self):
        before = {"status": "active", "bloodType": "O+"}
        after = {"status": "expired", "bloodType": "O+", "expiredAt": T2, "updatedAt": T1}
        intents = detect(events.REQUESTS, self.ids, before, after)
        self.assertEqual(kinds(intents), [Kind.REQUEST_EXPIRED])
        self.assertEqual(intents[0].occurred_at, T2)
        # Later edits to the expired request are not a second expiry.
        self.assertEqual(detect(events.REQUESTS, self.ids, after, {**after, "updatedAt": T2}), [])


class OfferEvents(unittest.TestCase):
    ids = {"requestId": "req1", "offerId": "o1"}

    def test_new_offer(self):
        intents = detect(events.OFFERS, self.ids, None, {"status": "pending", "donorId": "u2"})
        self.assertEqual(kinds(intents), [Kind.OFFER_CREATED])

    def test_status_changes(self):
        cases = {
            "approved": Kind.OFFER_APPROVED,
            "accepted": Kind.OFFER_APPROVED,
            "rejected": Kind.OFFER_REJECTED,
            "donated": Kind.OFFER_DONATED,
        }
        for status, kind in cases.items():
            with self.subTest(status=status):
                self.assertEqual(
                    kinds(detect(events.OFFERS, self.ids, {"status": "pending"}, {"status": status})),
                    [kind],
                )

    def test_withdrawn_is_silent(self):
        self.assertEqual(
            detect(events.OFFERS, self.ids, {"status": "pending"}, {"status": "withdrawn"}), []
        )


class OtherEvents(unittest.TestCase):
    def test_only_new_messages(self):
        ids = {"chatId": "c1", "messageId": "m1"}
        message = {"senderId": "u1", "message": "Hi", "createdAt": T1}
        self.assertEqual(kinds(detect(events.MESSAGES, ids, None, message)), [Kind.CHAT_MESSAGE])
        self.assertEqual(detect(events.MESSAGES, ids, message, {**message, "read": True}), [])

    def test_id_verification_review(self):
        ids = {"submissionId": "u1"}
        intents = detect(events.ID_VERIFICATIONS, ids, {"status": "submitted"},
                         {"status": "approved", "updatedAt": T1})
        self.assertEqual(kinds(intents), [Kind.ID_VERIFICATION_REVIEWED])
        self.assertEqual(detect(events.ID_VERIFICATIONS, ids, None, {"status": "submitted"}), [])

    def test_announcement_published_once(self):
        ids = {"announcementId": "a1"}
        draft = {"title": "Drive", "status": "draft"}
        live = {"title": "Drive", "status": "published", "createdAt": T1}
        self.assertEqual(detect(events.ANNOUNCEMENTS, ids, None, draft), [])
        self.assertEqual(kinds(detect(events.ANNOUNCEMENTS, ids, draft, live)),
                         [Kind.ANNOUNCEMENT_PUBLISHED])
        self.assertEqual(detect(events.ANNOUNCEMENTS, ids, live, {**live, "title": "Drive!"}), [])


if __name__ == "__main__":
    unittest.main()


class RequestExpiredNotification(unittest.TestCase):
    def test_message(self):
        from notifications import content

        self.assertEqual(
            content.request_expired({"bloodType": "AB-"}),
            (
                "Blood request expired",
                "Your blood request for AB- has expired. "
                "Submit a new request if blood is still needed.",
            ),
        )

    def test_donor_message_has_expired_wording(self):
        from notifications import content

        self.assertEqual(
            content.request_closed_for_donor({"bloodType": "O+"}, "expired"),
            ("Request expired", "The O+ request you offered to help with has expired."),
        )
        # Cancelled and fulfilled wording is unchanged.
        self.assertIn("cancelled by the requester",
                      content.request_closed_for_donor({"bloodType": "O+"}, "cancelled")[1])
        self.assertIn("fulfilled", content.request_closed_for_donor({"bloodType": "O+"}, "fulfilled")[1])

    def _run_worker(self, kind, offers):
        from unittest import mock

        from notifications.events import Intent
        from notifications.worker import NotificationWorker

        db = _FakeOffersDb(offers)
        worker = NotificationWorker.__new__(NotificationWorker)  # skip Firestore setup
        worker._db = db
        worker._notify = mock.Mock()
        data = {"status": "expired", "bloodType": "O+", "requesterId": "u1", "userId": "legacy"}
        worker._handle(Intent(kind, "req1", T2, data, {"requestId": "req1"}))
        return db, [c for c in worker._notify.call_args_list]

    def test_worker_notifies_requester_and_donors_with_active_offers(self):
        from notifications.constants import NotificationType, Route

        db, calls = self._run_worker(Kind.REQUEST_EXPIRED, {
            "offer-a": {"donorId": "donor-a", "status": "pending"},
            "offer-b": {"donorId": "donor-b", "status": "approved"},
        })

        # Only pending/approved (and legacy accepted) offers are queried.
        self.assertEqual(db.path, ["blood_requests", "req1", "offers"])
        f = db.filter
        self.assertEqual((f.field_path, f.op_string, sorted(f.value)),
                         ("status", "in", ["accepted", "approved", "pending"]))

        self.assertEqual(len(calls), 3)
        (user, ntype, intent, (title, message), route), kwargs = calls[0]
        self.assertEqual((user, ntype, route), ("u1", NotificationType.REQUEST_EXPIRED, Route.ACTIVITY))
        self.assertEqual(title, "Blood request expired")
        self.assertEqual(kwargs, {"requestId": "req1"})

        donor_calls = calls[1:]
        self.assertEqual([c.args[0] for c in donor_calls], ["donor-a", "donor-b"])
        for call, offer_id in zip(donor_calls, ["offer-a", "offer-b"]):
            user, ntype, intent, (title, message), route = call.args
            self.assertEqual(ntype, NotificationType.REQUEST_CLOSED_FOR_DONOR)
            self.assertEqual(route, Route.MY_OFFERS)
            self.assertEqual(title, "Request expired")
            self.assertEqual(message, "The O+ request you offered to help with has expired.")
            # Keyed by how the request closed, so it never collides with a
            # cancelled/fulfilled notice for the same request.
            self.assertEqual(intent.key, "expired_req1")
            self.assertEqual(call.kwargs, {"requestId": "req1", "offerId": offer_id})

    def test_expiry_with_no_offers_notifies_only_the_requester(self):
        _, calls = self._run_worker(Kind.REQUEST_EXPIRED, {})
        self.assertEqual([c.args[0] for c in calls], ["u1"])

    def test_cancel_still_notifies_donors_only(self):
        _, calls = self._run_worker(Kind.REQUEST_CANCELLED, {"o": {"donorId": "donor-a"}})
        self.assertEqual([c.args[0] for c in calls], ["donor-a"])
        self.assertEqual(calls[0].args[3][0], "Request cancelled")


class _FakeOffersDb:
    """Just enough of Firestore for blood_requests/{id}/offers where(...).stream()."""

    def __init__(self, offers):
        self.offers = offers
        self.path = []
        self.filter = None

    def collection(self, name):
        self.path.append(name)
        return self

    def document(self, doc_id):
        self.path.append(doc_id)
        return self

    def where(self, filter):
        self.filter = filter
        return self

    def stream(self):
        class Snap:
            def __init__(self, doc_id, data):
                self.id, self._data = doc_id, data

            def to_dict(self):
                return self._data

        return [Snap(doc_id, data) for doc_id, data in self.offers.items()]
