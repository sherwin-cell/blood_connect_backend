import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from account import router as account_router
from account.service import ADMIN_CANCEL_REASON, CANCEL_REASON, AccountDeletionService, Code
from account.store import collect_urls
from notifications.constants import NotificationType as T
from notifications.preferences import CATEGORY_BY_TYPE, push_allowed
from verification.storage import CloudinaryUploader

NOW = 1_800_000_000.0
PREFIX = "https://res.cloudinary.com/dlk8chosr/"


class FakeStore:
    def __init__(self, fail_on=None, admins=()):
        self.calls = []
        self.fail_on = fail_on
        self.admins = set(admins)
        self.deleted_by = None

    def _call(self, name, result):
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError("firestore down")
        return result

    def close_open_requests(self, uid, reason):
        self.reason = reason
        return self._call("close_open_requests", 2)

    def withdraw_active_offers(self, uid):
        return self._call("withdraw_active_offers", 1)

    def image_urls(self, uid):
        return self._call("image_urls", [PREFIX + "a.jpg", PREFIX + "a.jpg", PREFIX + "b.jpg"])

    def delete_personal_data(self, uid):
        return self._call("delete_personal_data", {"users": 1, "notifications": 4})

    def record_deletion(self, uid, summary, deleted_by=None):
        self.recorded = summary
        self.deleted_by = deleted_by
        return self._call("record_deletion", None)

    def is_admin(self, uid):
        return uid in self.admins


def make(store=None, delete_image=None, deleted_users=None):
    deleted_users = [] if deleted_users is None else deleted_users
    return AccountDeletionService(
        store=store or FakeStore(),
        delete_auth_user=deleted_users.append,
        delete_image=delete_image,
        max_auth_age_seconds=300,
        clock=lambda: NOW,
    )


class AccountDeletionServiceTest(unittest.TestCase):
    def test_requires_recent_sign_in(self):
        store, users = FakeStore(), []
        for auth_time in (NOW - 301, None, "abc", NOW + 3600):
            outcome = make(store, deleted_users=users).delete("u1", auth_time)
            self.assertEqual(outcome.status_code, 401)
            self.assertEqual(outcome.code, Code.REQUIRES_RECENT_LOGIN)
        self.assertEqual(store.calls, [])
        self.assertEqual(users, [])

    def test_deletes_in_order_and_auth_user_last(self):
        store, users = FakeStore(), []
        deleted_images = []
        outcome = make(store, delete_image=lambda u: deleted_images.append(u) or True,
                       deleted_users=users).delete("u1", NOW - 10)

        self.assertEqual(outcome.status_code, 200)
        self.assertTrue(outcome.body()["deleted"])
        self.assertEqual(store.calls, ["close_open_requests", "withdraw_active_offers",
                                       "image_urls", "delete_personal_data", "record_deletion"])
        self.assertEqual(store.reason, CANCEL_REASON)
        self.assertEqual(deleted_images, [PREFIX + "a.jpg", PREFIX + "b.jpg"])  # de-duplicated
        self.assertEqual(users, ["u1"])
        self.assertEqual(store.recorded["requestsCancelled"], 2)
        self.assertEqual(store.recorded["imagesDeleted"], 2)

    def test_image_failures_do_not_stop_deletion(self):
        def flaky(url):
            if url.endswith("a.jpg"):
                raise RuntimeError("cloudinary down")
            return False

        users = []
        outcome = make(delete_image=flaky, deleted_users=users).delete("u1", NOW)
        self.assertEqual(outcome.status_code, 200)
        self.assertEqual(outcome.summary["imageDeleteFailures"], 2)
        self.assertEqual(users, ["u1"])

    def test_firestore_failure_keeps_the_auth_user_for_a_retry(self):
        users = []
        store = FakeStore(fail_on="delete_personal_data")
        outcome = make(store, deleted_users=users).delete("u1", NOW)
        self.assertEqual(outcome.status_code, 500)
        self.assertEqual(outcome.code, Code.FAILED)
        self.assertEqual(users, [])
        self.assertNotIn("record_deletion", store.calls)


class AdminDeletionTest(unittest.TestCase):
    def test_admin_deletes_another_user_with_full_cleanup(self):
        store, users = FakeStore(admins={"prc"}), []
        # No recent-sign-in requirement on the admin path.
        outcome = make(store, delete_image=lambda u: True,
                       deleted_users=users).delete_by_admin("u1", {"uid": "prc"})
        self.assertEqual(outcome.status_code, 200)
        self.assertEqual(store.calls, ["close_open_requests", "withdraw_active_offers",
                                       "image_urls", "delete_personal_data", "record_deletion"])
        self.assertEqual(store.reason, ADMIN_CANCEL_REASON)
        self.assertEqual(store.deleted_by, "prc")
        self.assertEqual(users, ["u1"])

    def test_admin_custom_claim_is_enough(self):
        store = FakeStore()
        outcome = make(store).delete_by_admin("u1", {"uid": "prc", "admin": True})
        self.assertEqual(outcome.status_code, 200)

    def test_non_admin_is_forbidden(self):
        store, users = FakeStore(), []
        outcome = make(store, deleted_users=users).delete_by_admin("u1", {"uid": "u2"})
        self.assertEqual((outcome.status_code, outcome.code), (403, Code.FORBIDDEN))
        self.assertEqual(store.calls, [])
        self.assertEqual(users, [])

    def test_cannot_delete_self_or_other_admins(self):
        store = FakeStore(admins={"prc", "prc2"})
        service = make(store)
        self.assertEqual(service.delete_by_admin("prc", {"uid": "prc"}).code, Code.CANNOT_DELETE_SELF)
        self.assertEqual(service.delete_by_admin("prc2", {"uid": "prc"}).code, Code.CANNOT_DELETE_ADMIN)
        self.assertEqual(store.calls, [])

    def test_self_deletion_records_no_admin(self):
        store = FakeStore()
        make(store).delete("u1", NOW - 10)
        self.assertIsNone(store.deleted_by)


class EndpointTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(account_router.router)
        self.users = []
        account_router.configure(make(deleted_users=self.users))
        self.client = TestClient(app)

    def tearDown(self):
        account_router.configure(None)

    def test_missing_token_is_401(self):
        self.assertEqual(self.client.delete("/api/account").status_code, 401)

    def test_fresh_token_deletes(self):
        claims = {"uid": "u1", "auth_time": NOW - 5}
        with mock.patch("auth.auth.verify_id_token", return_value=claims):
            response = self.client.delete("/api/account",
                                          headers={"Authorization": "Bearer t"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], Code.DELETED)
        self.assertEqual(self.users, ["u1"])

    def test_stale_token_asks_to_sign_in_again(self):
        claims = {"uid": "u1", "auth_time": NOW - 3600}
        with mock.patch("auth.auth.verify_id_token", return_value=claims):
            response = self.client.delete("/api/account",
                                          headers={"Authorization": "Bearer t"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], Code.REQUIRES_RECENT_LOGIN)
        self.assertEqual(self.users, [])

    def test_admin_endpoint_deletes_target(self):
        account_router.configure(make(FakeStore(admins={"prc"}), deleted_users=self.users))
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": "prc"}):
            response = self.client.delete("/api/admin/users/u1",
                                          headers={"Authorization": "Bearer t"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.users, ["u1"])

    def test_admin_endpoint_rejects_non_admin(self):
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": "u2"}):
            response = self.client.delete("/api/admin/users/u1",
                                          headers={"Authorization": "Bearer t"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.users, [])

    def test_not_configured_is_503(self):
        account_router.configure(None)
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": "u1"}):
            response = self.client.delete("/api/account",
                                          headers={"Authorization": "Bearer t"})
        self.assertEqual(response.status_code, 503)


class ImageHelpersTest(unittest.TestCase):
    def test_collect_urls_walks_nested_values(self):
        doc = {
            "photoUrl": PREFIX + "image/upload/v1/p.jpg",
            "faceVerification": {"selfieImageUrl": PREFIX + "image/upload/s.jpg"},
            "list": [PREFIX + "x.png", "https://elsewhere.test/y.png"],
            "n": 3,
        }
        self.assertEqual(len(collect_urls(doc, PREFIX)), 3)

    def test_public_id_from_url(self):
        cases = {
            PREFIX + "image/upload/v1712345/face_verifications/abc.jpg": "face_verifications/abc",
            PREFIX + "image/upload/face_verifications/review/x.png": "face_verifications/review/x",
            PREFIX + "image/upload/v9/plain.jpeg?foo=1": "plain",
            "https://res.cloudinary.com/someoneelse/image/upload/v1/a.jpg": None,
            PREFIX + "video/upload/v1/a.mp4": None,
        }
        for url, expected in cases.items():
            self.assertEqual(CloudinaryUploader.public_id_from_url(url, PREFIX), expected, url)

    def test_destroy_signs_the_request(self):
        uploader = CloudinaryUploader("dlk8chosr", "key", "secret", "face_verifications", PREFIX)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"result": "ok"}
        with mock.patch("verification.storage.requests.post", return_value=response) as post, \
                mock.patch("verification.storage.time.time", return_value=1000):
            self.assertTrue(uploader.destroy(PREFIX + "image/upload/v1/f/a.jpg"))
        form = post.call_args.kwargs["data"]
        self.assertEqual(form["public_id"], "f/a")
        self.assertEqual(form["signature"], CloudinaryUploader.signature(
            {"public_id": "f/a", "timestamp": "1000"}, "secret"))
        self.assertTrue(post.call_args.args[0].endswith("/image/destroy"))

    def test_destroy_ignores_foreign_urls(self):
        uploader = CloudinaryUploader("dlk8chosr", "key", "secret", "f", PREFIX)
        with mock.patch("verification.storage.requests.post") as post:
            self.assertFalse(uploader.destroy("https://evil.test/a.jpg"))
        post.assert_not_called()


class PushPreferencesTest(unittest.TestCase):
    def test_default_is_on(self):
        self.assertTrue(push_allowed(T.REQUEST_OPEN, None))
        self.assertTrue(push_allowed(T.REQUEST_OPEN, {}))
        self.assertTrue(push_allowed(T.REQUEST_OPEN, {"notificationPreferences": "bad"}))

    def test_switched_off_category_is_muted(self):
        user = {"notificationPreferences": {"donorAlerts": False, "chatMessages": True}}
        self.assertFalse(push_allowed(T.REQUEST_OPEN, user))
        self.assertTrue(push_allowed(T.CHAT_MESSAGE, user))
        self.assertTrue(push_allowed(T.REQUEST_APPROVED, user))

    def test_account_results_are_always_pushed(self):
        user = {"notificationPreferences": {k: False for k in
                                            ("requestUpdates", "donorAlerts", "donationUpdates",
                                             "chatMessages", "announcements")}}
        self.assertTrue(push_allowed(T.ID_VERIFICATION_APPROVED, user))
        self.assertTrue(push_allowed(T.ID_VERIFICATION_REJECTED, user))
        self.assertFalse(push_allowed(T.OFFER_DONATED, user))

    def test_every_other_type_has_a_category(self):
        types = {v for k, v in vars(T).items() if k.isupper()}
        uncategorised = types - set(CATEGORY_BY_TYPE)
        self.assertEqual(uncategorised, {T.ID_VERIFICATION_APPROVED, T.ID_VERIFICATION_REJECTED})


class DeliveryMutedTest(unittest.TestCase):
    def test_muted_push_sends_nothing(self):
        from notifications.delivery import Delivery

        user_snapshot = mock.Mock(exists=True)
        user_snapshot.to_dict.return_value = {"notificationPreferences": {"donorAlerts": False}}
        db = mock.MagicMock()
        db.collection.return_value.document.return_value.get.return_value = user_snapshot

        with mock.patch("notifications.delivery.messaging.send_each_for_multicast") as send:
            result = Delivery(db)._send_to_user("n1", {"userId": "u1", "type": T.REQUEST_OPEN})
        self.assertEqual(result["pushStatus"], "muted")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
