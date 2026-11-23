"""Tests for PRC stock notices (prc_stock/). Run: python -m unittest discover tests"""

import unittest
from datetime import datetime, timezone
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications import content
from notifications.preferences import push_allowed
from notifications.constants import NotificationType as T
from prc_stock import router as prc_router
from prc_stock.model import covered_by_prc_stock, units_needed
from prc_stock.service import Code, Notice, Outcome, PrcStockService, plan_notice

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
TS = "SERVER_TIMESTAMP"
ADMIN = {"uid": "prc", "admin": True}
GOOD = Notice(2, "Claim at PRC Sorsogon Chapter blood bank before 5 PM. Bring a valid ID.")


def request(**overrides):
    data = {"requesterId": "req1", "userId": "req1", "status": "pending", "bloodType": "O+",
            "unitsRequired": 2, "component": "Packed RBC", "hospitalName": "SPH",
            "idVerificationSubmissionId": "req1"}
    data.update(overrides)
    return data


class FakeStore:
    def __init__(self, doc=None, admins=(), chat_fails=False):
        self.doc = doc
        self.admins = set(admins)
        self.chat_fails = chat_fails
        self.plans = []
        self.messages = []
        self.chat_marked = False

    def is_admin(self, uid):
        return uid in self.admins

    def apply(self, request_id, build):
        result = build(self.doc)
        if not isinstance(result, Outcome):
            self.plans.append(result)
        return result

    def mark_chat_sent(self, request_id):
        self.chat_marked = True

    def post_support_message(self, requester_id, text, request_id):
        if self.chat_fails:
            raise RuntimeError("down")
        self.messages.append((requester_id, text, request_id))


def make(doc=None, **kwargs):
    store = FakeStore(doc, **kwargs)
    notify = mock.Mock(return_value=True)
    return PrcStockService(store, notify, clock=lambda: NOW, timestamp=TS), store, notify


class PlanTest(unittest.TestCase):
    def test_pending_request_is_approved_and_kept_from_donors_when_fully_covered(self):
        plan = plan_notice(request(id="r1"), GOOD, "prc", NOW, TS)
        self.assertTrue(plan.approve)
        self.assertEqual(plan.request_update["status"], "active")
        self.assertEqual(plan.request_update["reviewedBy"], "prc")
        self.assertEqual(plan.request_update["prcStock"]["reservedUnits"], 2)
        self.assertEqual(plan.request_update["prcStock"]["noticeCount"], 1)
        self.assertEqual(plan.notice_entry["notifiedAt"], NOW)
        self.assertIsNone(plan.summary)  # no donors needed
        self.assertEqual(plan.submission_id, "req1")

    def test_partial_cover_publishes_to_donors(self):
        plan = plan_notice(request(id="r1", unitsRequired=3), GOOD, "prc", NOW, TS)
        self.assertEqual(plan.summary["requestId"], "r1")
        self.assertEqual(plan.summary["status"], "active")
        self.assertNotIn("patientName", plan.summary)

    def test_open_request_keeps_its_status(self):
        plan = plan_notice(request(status="matched", prcStockNotices=[{}]), GOOD, "prc", NOW, TS)
        self.assertFalse(plan.approve)
        self.assertNotIn("status", plan.request_update)
        self.assertIsNone(plan.summary)
        self.assertIsNone(plan.submission_id)
        self.assertEqual(plan.notice_number, 2)

    def test_refusals(self):
        cases = [
            (None, GOOD, Code.NOT_FOUND),
            (request(archived=True), GOOD, Code.ARCHIVED),
            (request(status="archived"), GOOD, Code.ARCHIVED),
            (request(status="fulfilled"), GOOD, Code.CLOSED),
            (request(status="cancelled"), GOOD, Code.CLOSED),
            (request(requesterId=None, userId=None), GOOD, Code.CLOSED),
            (request(), Notice(3, GOOD.pickup_instructions), Code.INVALID_UNITS),
            (request(), Notice(0, GOOD.pickup_instructions), Code.INVALID_UNITS),
            (request(), Notice(True, GOOD.pickup_instructions), Code.INVALID_UNITS),
        ]
        for doc, notice, code in cases:
            self.assertEqual(plan_notice(doc, notice, "prc", NOW, TS).code, code, (doc, notice))

    def test_model_helpers(self):
        self.assertEqual(units_needed({"unitsNeeded": "3"}), 3)
        self.assertEqual(units_needed({}), 1)
        self.assertTrue(covered_by_prc_stock({"unitsRequired": 2, "prcStock": {"reservedUnits": 2}}))
        self.assertFalse(covered_by_prc_stock({"unitsRequired": 2, "prcStock": {"reservedUnits": 1}}))
        self.assertFalse(covered_by_prc_stock({"unitsRequired": 1}))


class ServiceTest(unittest.TestCase):
    def test_notifies_requester_and_posts_chat_message(self):
        service, store, notify = make(request())
        outcome = service.notify(ADMIN, "r1", Notice(2, "  Claim at   PRC  today. ", "See you at PRC."))
        self.assertEqual((outcome.status, outcome.code), (200, Code.NOTIFIED))
        self.assertTrue(outcome.body["approved"])
        self.assertTrue(outcome.body["chatMessageSent"])
        notify.assert_called_once()
        args = notify.call_args.args
        self.assertEqual(args[:3], ("req1", "r1", 1))
        self.assertEqual(args[5], "Claim at PRC today.")  # whitespace collapsed
        self.assertEqual(store.messages, [("req1", "See you at PRC.", "r1")])
        self.assertTrue(store.chat_marked)

    def test_admin_by_role(self):
        service, store, _ = make(request(), admins={"staff"})
        self.assertEqual(service.notify({"uid": "staff"}, "r1", GOOD).status, 200)

    def test_non_admin_is_refused_before_anything_is_written(self):
        service, store, notify = make(request())
        outcome = service.notify({"uid": "req1"}, "r1", GOOD)
        self.assertEqual((outcome.status, outcome.code), (403, Code.FORBIDDEN))
        self.assertEqual(store.plans, [])
        notify.assert_not_called()

    def test_bad_input(self):
        service, store, notify = make(request())
        self.assertEqual(service.notify(ADMIN, "r1", Notice(1, "short")).code, Code.INVALID_INSTRUCTIONS)
        self.assertEqual(service.notify(ADMIN, "r1", Notice(1, GOOD.pickup_instructions, "x" * 1001)).code,
                         Code.INVALID_MESSAGE)
        self.assertEqual(service.notify(ADMIN, "r1", Notice(5, GOOD.pickup_instructions)).code,
                         Code.INVALID_UNITS)
        notify.assert_not_called()

    def test_chat_failure_still_reports_success(self):
        service, store, notify = make(request(), chat_fails=True)
        outcome = service.notify(ADMIN, "r1", Notice(1, GOOD.pickup_instructions, "Hello"))
        self.assertEqual(outcome.status, 200)
        self.assertFalse(outcome.body["chatMessageSent"])
        self.assertFalse(store.chat_marked)

    def test_no_chat_message_when_blank(self):
        service, store, _ = make(request())
        service.notify(ADMIN, "r1", Notice(1, GOOD.pickup_instructions, "   "))
        self.assertEqual(store.messages, [])


class ContentTest(unittest.TestCase):
    def test_message_and_push_preferences(self):
        title, message = content.prc_stock_available({"bloodType": "O+"}, 1, "Claim it today.")
        self.assertEqual(title, "Blood available at PRC Sorsogon")
        self.assertIn("1 unit of O+", message)
        self.assertIn("Claim it today.", message)
        # Always pushed, even with request updates switched off.
        self.assertTrue(push_allowed(T.PRC_STOCK_AVAILABLE,
                                     {"notificationPreferences": {"requestUpdates": False}}))


class WorkerTest(unittest.TestCase):
    def test_fully_covered_approval_alerts_no_donors(self):
        from notifications.events import Intent, Kind
        from notifications.worker import NotificationWorker

        worker = NotificationWorker.__new__(NotificationWorker)
        worker._db = mock.MagicMock()
        worker._notify = mock.Mock()
        worker._matching_donors = mock.Mock(return_value=[mock.Mock(user_id="d1")])
        intent = Intent(Kind.REQUEST_APPROVED, "k", None, ids={"requestId": "r1"},
                        data={"requesterId": "req", "unitsRequired": 2,
                              "prcStock": {"reservedUnits": 2}})
        worker._request_approved(intent)
        worker._notify.assert_not_called()
        worker._matching_donors.assert_not_called()


class EndpointTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(prc_router.router)
        self.service, self.store, _ = make(request())
        prc_router.configure(self.service)
        self.client = TestClient(app)

    def tearDown(self):
        prc_router.configure(None)

    def post(self, body, claims=ADMIN):
        with mock.patch("auth.auth.verify_id_token", return_value=claims):
            return self.client.post("/api/admin/requests/r1/prc-stock", json=body,
                                    headers={"Authorization": "Bearer t"})

    def test_requires_sign_in(self):
        response = self.client.post("/api/admin/requests/r1/prc-stock",
                                    json={"reservedUnits": 1, "pickupInstructions": "x" * 20})
        self.assertEqual(response.status_code, 401)

    def test_notify(self):
        response = self.post({"reservedUnits": 2, "pickupInstructions": GOOD.pickup_instructions})
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["code"], Code.NOTIFIED)

    def test_requester_cannot_notify(self):
        response = self.post({"reservedUnits": 1, "pickupInstructions": GOOD.pickup_instructions},
                             claims={"uid": "req1"})
        self.assertEqual(response.status_code, 403)

    def test_missing_fields(self):
        self.assertEqual(self.post({"reservedUnits": 1}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
