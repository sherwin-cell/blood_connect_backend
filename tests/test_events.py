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

    def test_matched_and_expired_are_silent(self):
        for status in ("matched", "expired"):
            self.assertEqual(
                detect(events.REQUESTS, self.ids, {"status": "active"}, {"status": status}), []
            )


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
