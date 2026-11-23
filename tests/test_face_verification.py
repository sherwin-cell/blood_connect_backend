"""Tests for server-side face verification. Run: python -m unittest discover tests"""

import hashlib
import unittest
from datetime import datetime, timezone
from unittest import mock

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.constants import PH_TZ
from verification import router as face_router
from verification.config import FaceVerificationSettings
from verification.images import InvalidImage, is_allowed_image_url, validate_selfie
from verification.matcher import MatcherError, MatchResult, NoFaceFound, OpenCvSFaceMatcher
from verification.service import Code, FaceVerificationService
from verification.storage import (
    SERVER_TIMESTAMP_MARKER,
    AttemptLimitReached,
    CloudinaryUploader,
    StorageError,
)

UID = "user-1"
ID_URL = "https://res.cloudinary.com/dlk8chosr/image/upload/v1/ids/front.jpg"
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=PH_TZ)  # 10:00 PH
SETTINGS = FaceVerificationSettings(threshold=0.363, max_attempts_per_day=5,
                                    cloudinary_api_key="k", cloudinary_api_secret="s")


def jpeg(width=200, height=200) -> bytes:
    ok, data = cv2.imencode(".jpg", np.full((height, width, 3), 128, dtype=np.uint8))
    assert ok
    return data.tobytes()


def png(width=200, height=200) -> bytes:
    ok, data = cv2.imencode(".png", np.full((height, width, 3), 200, dtype=np.uint8))
    assert ok
    return data.tobytes()


SELFIE = jpeg()


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeStore:
    def __init__(self, user=None, submission=None):
        self.users = {UID: dict(user or {})}
        self.submissions = {UID: submission} if submission is not None else {}
        self.quota = {}
        self.attempts = []
        self.saved = []

    def get_user(self, uid):
        return self.users.get(uid)

    def get_id_submission(self, uid):
        return self.submissions.get(uid)

    def attempts_used(self, uid, day):
        return self.quota.get((uid, day), 0)

    def reserve_attempt(self, uid, day, max_attempts):
        used = self.quota.get((uid, day), 0)
        if used >= max_attempts:
            raise AttemptLimitReached()
        self.quota[(uid, day)] = used + 1
        return used + 1

    def release_attempt(self, uid, day):
        if self.quota.get((uid, day), 0) > 0:
            self.quota[(uid, day)] -= 1

    def log_attempt(self, record):
        self.attempts.append(record)
        return f"attempt-{len(self.attempts)}"

    def save_result(self, uid, fields):
        if self.users.get(uid, {}).get("faceVerified") is True:
            return False
        self.saved.append(fields)
        self.users.setdefault(uid, {}).update(fields)
        return True


class FakeMatcher:
    provider = "fake"

    def __init__(self, score=0.9, error=None):
        self.score = score
        self.error = error
        self.calls = 0

    def compare(self, id_image, selfie_image):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return MatchResult(score=self.score, provider=self.provider)


def make(store=None, matcher=None, fetch=None, upload="default", settings=SETTINGS, now=NOW):
    store = store or FakeStore(submission={"frontIdImageUrl": ID_URL})
    matcher = matcher or FakeMatcher()
    uploads = []

    def default_upload(data, filename, subfolder=""):
        uploads.append((filename, subfolder))
        folder = f"face_verifications/{subfolder}" if subfolder else "face_verifications"
        return f"https://res.cloudinary.com/dlk8chosr/image/upload/v1/{folder}/s.jpg"

    service = FaceVerificationService(
        store=store,
        matcher=matcher,
        settings=settings,
        fetch_id_image=fetch or (lambda url: b"id-image-bytes"),
        upload_selfie=default_upload if upload == "default" else upload,
        clock=lambda: now,
    )
    return service, store, matcher, uploads


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


class AlreadyVerified(unittest.TestCase):
    def test_returns_existing_result_without_matching_or_counting(self):
        store = FakeStore(user={"faceVerified": True,
                                "faceVerification": {"score": 0.71, "provider": "opencv_sface",
                                                     "threshold": 0.363}},
                          submission={"frontIdImageUrl": ID_URL})
        service, store, matcher, uploads = make(store=store)

        outcome = service.verify(UID, SELFIE)

        self.assertEqual((outcome.status, outcome.code), (200, Code.ALREADY_VERIFIED))
        self.assertTrue(outcome.passed and outcome.already_verified)
        self.assertEqual(outcome.score, 0.71)
        self.assertEqual(matcher.calls, 0)
        self.assertEqual(uploads, [])
        self.assertEqual(store.quota, {})
        self.assertEqual(store.attempts[-1]["outcome"], Code.ALREADY_VERIFIED)
        self.assertFalse(store.attempts[-1]["counted"])


class MissingId(unittest.TestCase):
    def test_no_submission(self):
        service, store, matcher, _ = make(store=FakeStore())
        outcome = service.verify(UID, SELFIE)
        self.assertEqual((outcome.status, outcome.code), (409, Code.ID_REQUIRED))
        self.assertEqual(matcher.calls, 0)
        self.assertEqual(store.quota, {})
        self.assertEqual(store.attempts[-1]["outcome"], Code.ID_REQUIRED)

    def test_blank_front_url(self):
        for value in ("", "   ", None, 42):
            service, store, _, _ = make(store=FakeStore(submission={"frontIdImageUrl": value}))
            self.assertEqual(service.verify(UID, SELFIE).code, Code.ID_REQUIRED, value)


class NonCloudinaryUrl(unittest.TestCase):
    BAD = [
        "https://example.com/id.jpg",
        "http://res.cloudinary.com/dlk8chosr/image/upload/id.jpg",
        "https://res.cloudinary.com/othercloud/image/upload/id.jpg",
        "https://res.cloudinary.com.evil.com/dlk8chosr/id.jpg",
        "https://user:pw@res.cloudinary.com/dlk8chosr/id.jpg",
        "https://res.cloudinary.com:8443/dlk8chosr/id.jpg",
        "https://res.cloudinary.com/dlk8chosr/../othercloud/id.jpg",
        "file:///etc/passwd",
    ]

    def test_rejected_before_any_download(self):
        for url in self.BAD:
            fetched = []
            service, store, matcher, _ = make(
                store=FakeStore(submission={"frontIdImageUrl": url}),
                fetch=lambda u: fetched.append(u) or b"",
            )
            outcome = service.verify(UID, SELFIE)
            self.assertEqual((outcome.status, outcome.code), (422, Code.INVALID_ID_IMAGE_URL), url)
            self.assertEqual(fetched, [], url)
            self.assertEqual(matcher.calls, 0, url)
            self.assertEqual(store.quota, {}, url)

    def test_url_check(self):
        prefix = SETTINGS.allowed_image_prefix
        self.assertTrue(is_allowed_image_url(ID_URL, prefix))
        for url in self.BAD + [None, 123]:
            self.assertFalse(is_allowed_image_url(url, prefix), url)


class SelfieValidation(unittest.TestCase):
    def test_bad_uploads_are_rejected_and_not_counted(self):
        cases = [
            (b"", 400, Code.INVALID_SELFIE),
            (b"GIF89a" + b"\0" * 100, 400, Code.INVALID_SELFIE),
            (b"\xff\xd8\xff" + b"not really a jpeg", 400, Code.INVALID_SELFIE),
            (jpeg(32, 32), 400, Code.INVALID_SELFIE),
            (b"\xff\xd8\xff" + b"\0" * (5 * 1024 * 1024), 413, Code.SELFIE_TOO_LARGE),
        ]
        for data, status, code in cases:
            service, store, matcher, _ = make()
            outcome = service.verify(UID, data)
            self.assertEqual((outcome.status, outcome.code), (status, code), code)
            self.assertEqual(matcher.calls, 0)
            self.assertEqual(store.quota, {})

    def test_png_and_jpeg_accepted(self):
        self.assertEqual(validate_selfie(jpeg(), SETTINGS.max_selfie_bytes), "jpeg")
        self.assertEqual(validate_selfie(png(), SETTINGS.max_selfie_bytes), "png")
        with self.assertRaises(InvalidImage):
            validate_selfie(b"\x89PNG\r\n\x1a\n" + b"junk", SETTINGS.max_selfie_bytes)


class Threshold(unittest.TestCase):
    def test_exactly_at_threshold_passes(self):
        service, store, _, uploads = make(matcher=FakeMatcher(score=0.363))
        outcome = service.verify(UID, SELFIE)
        self.assertEqual((outcome.status, outcome.code, outcome.passed), (200, Code.VERIFIED, True))
        self.assertEqual(len(uploads), 1)

    def test_just_below_threshold_fails(self):
        service, store, _, uploads = make(matcher=FakeMatcher(score=0.363 - 1e-6))
        outcome = service.verify(UID, SELFIE)
        self.assertEqual((outcome.status, outcome.code, outcome.passed), (200, Code.NOT_MATCHED, False))
        self.assertEqual(uploads, [(f"review_{UID}.jpg", "review")])  # kept for PRC review only

    def test_threshold_comes_from_settings(self):
        strict = FaceVerificationSettings(threshold=0.5, cloudinary_api_key="k",
                                          cloudinary_api_secret="s")
        service, _, _, _ = make(matcher=FakeMatcher(score=0.45), settings=strict)
        self.assertEqual(service.verify(UID, SELFIE).code, Code.NOT_MATCHED)

    def test_threshold_env(self):
        env = {"FACE_MATCH_THRESHOLD": "0.42", "FACE_MATCH_MAX_ATTEMPTS_PER_DAY": "3"}
        with mock.patch.dict("os.environ", env, clear=True):
            settings = FaceVerificationSettings.from_env()
        self.assertEqual((settings.threshold, settings.max_attempts_per_day), (0.42, 3))
        with mock.patch.dict("os.environ", {}, clear=True):
            settings = FaceVerificationSettings.from_env()
        self.assertEqual((settings.threshold, settings.max_attempts_per_day), (0.363, 5))
        self.assertFalse(settings.can_upload)


class PassAndFailWrites(unittest.TestCase):
    def test_pass_uploads_selfie_and_marks_user_verified(self):
        service, store, _, uploads = make(matcher=FakeMatcher(score=0.8))
        outcome = service.verify(UID, SELFIE)

        self.assertEqual(outcome.attempts_remaining, 4)
        self.assertEqual(uploads, [(f"selfie_{UID}.jpg", "")])
        user = store.users[UID]
        self.assertIs(user["faceVerified"], True)
        face = user["faceVerification"]
        self.assertEqual(face["result"], "verified")
        self.assertIs(face["passed"], True)
        self.assertEqual((face["score"], face["threshold"], face["provider"]), (0.8, 0.363, "fake"))
        self.assertTrue(face["selfieImageUrl"].startswith(SETTINGS.allowed_image_prefix))
        self.assertEqual(user["selfieImageUrl"], face["selfieImageUrl"])
        self.assertEqual(face["verifiedAt"], SERVER_TIMESTAMP_MARKER)

        body = outcome.body()
        self.assertEqual(body["code"], "verified")
        self.assertTrue(body["passed"])
        self.assertNotIn("selfieImageUrl", body)

    def test_fail_records_score_and_keeps_selfie_for_review(self):
        service, store, _, uploads = make(matcher=FakeMatcher(score=0.1))
        outcome = service.verify(UID, SELFIE)

        self.assertEqual(uploads, [(f"review_{UID}.jpg", "review")])
        user = store.users[UID]
        self.assertIs(user["faceVerified"], False)
        self.assertEqual(user["faceVerification"]["result"], "failed")
        self.assertEqual(user["faceVerification"]["score"], 0.1)
        self.assertEqual(outcome.attempts_remaining, 4)

        # The review selfie is only on the admin-only attempt record,
        # never on users/{uid} (readable by every signed-in user).
        self.assertNotIn("selfieImageUrl", user)
        self.assertNotIn("selfieImageUrl", user["faceVerification"])
        attempt = store.attempts[-1]
        self.assertIn("/face_verifications/review/", attempt["selfieImageUrl"])
        self.assertTrue(attempt["savedForReview"])
        self.assertTrue(outcome.body()["savedForReview"])
        self.assertNotIn("selfieImageUrl", outcome.body())

    def test_review_upload_failure_does_not_change_the_result(self):
        def fail(data, filename, subfolder=""):
            raise StorageError("HTTP 500")

        service, store, _, _ = make(matcher=FakeMatcher(score=0.1), upload=fail)
        outcome = service.verify(UID, SELFIE)

        self.assertEqual((outcome.status, outcome.code), (200, Code.NOT_MATCHED))
        self.assertEqual(outcome.attempts_remaining, 4)  # still counted
        attempt = store.attempts[-1]
        self.assertFalse(attempt["savedForReview"])
        self.assertIn("HTTP 500", attempt["reviewUploadError"])
        self.assertNotIn("selfieImageUrl", attempt)
        self.assertFalse(outcome.body()["savedForReview"])

    def test_no_face_in_selfie_is_kept_for_review(self):
        service, store, _, uploads = make(matcher=FakeMatcher(error=NoFaceFound("selfie")))
        outcome = service.verify(UID, SELFIE)
        self.assertEqual(outcome.code, Code.NO_FACE_IN_SELFIE)
        self.assertEqual(uploads, [(f"review_{UID}.jpg", "review")])
        self.assertTrue(store.attempts[-1]["counted"])
        self.assertIn("/review/", store.attempts[-1]["selfieImageUrl"])

    def test_no_face_on_id_is_kept_for_review_and_refunded(self):
        service, store, _, uploads = make(matcher=FakeMatcher(error=NoFaceFound("id")))
        outcome = service.verify(UID, SELFIE)
        self.assertEqual(outcome.code, Code.NO_FACE_IN_ID)
        self.assertEqual(outcome.attempts_remaining, 5)
        self.assertEqual(uploads, [(f"review_{UID}.jpg", "review")])
        self.assertFalse(store.attempts[-1]["counted"])
        self.assertTrue(outcome.body()["savedForReview"])

    def test_png_selfie_keeps_png_extension(self):
        service, _, _, uploads = make(matcher=FakeMatcher(score=0.1))
        service.verify(UID, png())
        self.assertEqual(uploads, [(f"review_{UID}.png", "review")])

    def test_admin_approval_in_the_meantime_is_not_overwritten(self):
        store = FakeStore(submission={"frontIdImageUrl": ID_URL})
        service, store, _, _ = make(store=store, matcher=FakeMatcher(score=0.1))
        original_reserve = store.reserve_attempt

        def approve_then_reserve(*args):
            used = original_reserve(*args)
            store.users[UID]["faceVerified"] = True  # PRC approved during the match
            return used

        store.reserve_attempt = approve_then_reserve
        service.verify(UID, SELFIE)
        self.assertIs(store.users[UID]["faceVerified"], True)
        self.assertEqual(store.saved, [])


class AttemptLimit(unittest.TestCase):
    def test_five_counted_attempts_then_429(self):
        service, store, matcher, _ = make(matcher=FakeMatcher(score=0.0))
        remaining = [service.verify(UID, SELFIE).attempts_remaining for _ in range(5)]
        self.assertEqual(remaining, [4, 3, 2, 1, 0])

        outcome = service.verify(UID, SELFIE)
        self.assertEqual((outcome.status, outcome.code), (429, Code.ATTEMPT_LIMIT))
        self.assertEqual(outcome.attempts_remaining, 0)
        self.assertEqual(matcher.calls, 5)  # the 6th never reached the matcher
        self.assertEqual(store.attempts[-1]["outcome"], Code.ATTEMPT_LIMIT)
        self.assertIn("tomorrow", outcome.body()["message"])

    def test_limit_resets_on_the_next_ph_day(self):
        store = FakeStore(submission={"frontIdImageUrl": ID_URL})
        today, _, _, _ = make(store=store, matcher=FakeMatcher(score=0.0))
        for _ in range(5):
            today.verify(UID, SELFIE)
        self.assertEqual(today.verify(UID, SELFIE).status, 429)

        # 00:00 PH the next day.
        tomorrow, _, _, _ = make(store=store, matcher=FakeMatcher(score=0.0),
                                 now=datetime(2026, 11, 24, 0, 0, tzinfo=PH_TZ))
        self.assertEqual(tomorrow.verify(UID, SELFIE).attempts_remaining, 4)

    def test_day_is_ph_calendar_day(self):
        store = FakeStore(submission={"frontIdImageUrl": ID_URL})
        late_utc = datetime(2026, 11, 23, 16, 30, tzinfo=timezone.utc)  # 00:30 PH Nov 24
        service, _, _, _ = make(store=store, matcher=FakeMatcher(score=0.0), now=late_utc)
        service.verify(UID, SELFIE)
        self.assertEqual(store.attempts[-1]["day"], "2026-11-24")

    def test_no_face_in_selfie_counts(self):
        service, store, _, _ = make(matcher=FakeMatcher(error=NoFaceFound("selfie")))
        outcome = service.verify(UID, SELFIE)
        self.assertEqual((outcome.status, outcome.code), (422, Code.NO_FACE_IN_SELFIE))
        self.assertEqual(outcome.attempts_remaining, 4)
        self.assertTrue(store.attempts[-1]["counted"])

    def test_every_request_is_logged(self):
        service, store, _, _ = make(matcher=FakeMatcher(score=0.0))
        service.verify(UID, b"")      # invalid selfie
        service.verify(UID, SELFIE)   # counted failure
        self.assertEqual([a["outcome"] for a in store.attempts],
                         [Code.INVALID_SELFIE, Code.NOT_MATCHED])
        self.assertEqual([a["counted"] for a in store.attempts], [False, True])
        for attempt in store.attempts:
            self.assertEqual((attempt["userId"], attempt["day"]), (UID, "2026-11-23"))
            self.assertIn("httpStatus", attempt)


class ServerSideFailuresAreRefunded(unittest.TestCase):
    def assert_refunded(self, outcome, store, status, code):
        self.assertEqual((outcome.status, outcome.code), (status, code))
        self.assertEqual(store.attempts_used(UID, "2026-11-23"), 0)
        self.assertEqual(outcome.attempts_remaining, 5)
        self.assertFalse(store.attempts[-1]["counted"])
        self.assertIsNot(store.users[UID].get("faceVerified"), True)

    def test_matcher_failure(self):
        service, store, _, uploads = make(matcher=FakeMatcher(error=MatcherError("models missing")))
        outcome = service.verify(UID, SELFIE)
        self.assert_refunded(outcome, store, 503, Code.MATCHER_UNAVAILABLE)
        self.assertEqual(uploads, [])
        self.assertIn("models missing", store.attempts[-1]["error"])
        self.assertNotIn("models", outcome.body()["message"])  # no internals to the app

    def test_no_face_on_id(self):
        service, store, _, _ = make(matcher=FakeMatcher(error=NoFaceFound("id")))
        self.assert_refunded(service.verify(UID, SELFIE), store, 422, Code.NO_FACE_IN_ID)

    def test_id_download_failure(self):
        def fail(url):
            raise StorageError("HTTP 404")

        service, store, matcher, _ = make(fetch=fail)
        self.assert_refunded(service.verify(UID, SELFIE), store, 502, Code.ID_IMAGE_UNAVAILABLE)
        self.assertEqual(matcher.calls, 0)

    def test_upload_failure_does_not_verify(self):
        def fail(data, filename, subfolder=""):
            raise StorageError("HTTP 500")

        service, store, _, _ = make(upload=fail)
        self.assert_refunded(service.verify(UID, SELFIE), store, 502, Code.UPLOAD_FAILED)

    def test_not_configured_is_not_counted(self):
        service, store, matcher, _ = make(upload=None)
        outcome = service.verify(UID, SELFIE)
        self.assertEqual((outcome.status, outcome.code), (503, Code.NOT_CONFIGURED))
        self.assertEqual((matcher.calls, store.quota), (0, {}))


# --------------------------------------------------------------------------
# HTTP endpoint and auth
# --------------------------------------------------------------------------


def client_for(service):
    app = FastAPI()
    app.include_router(face_router.router)
    face_router.configure(service)
    return TestClient(app)


class Endpoint(unittest.TestCase):
    def tearDown(self):
        face_router.configure(None)

    def post(self, client, headers=None, data=SELFIE):
        return client.post("/api/verification/face", headers=headers or {},
                           files={"selfie": ("selfie.jpg", data, "image/jpeg")})

    def test_missing_token_is_401(self):
        service, store, matcher, _ = make()
        response = self.post(client_for(service))
        self.assertEqual(response.status_code, 401)
        self.assertEqual((matcher.calls, store.attempts), (0, []))

    def test_invalid_token_is_401(self):
        service, _, matcher, _ = make()
        with mock.patch("auth.auth.verify_id_token", side_effect=ValueError("bad")):
            response = self.post(client_for(service), {"Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(matcher.calls, 0)

    def test_valid_token_uses_the_token_uid(self):
        service, store, _, _ = make(matcher=FakeMatcher(score=0.9))
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": UID}):
            response = self.post(client_for(service), {"Authorization": "Bearer good"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["code"], body["passed"], body["attemptsRemaining"]),
                         ("verified", True, 4))
        self.assertIs(store.users[UID]["faceVerified"], True)

    def test_other_users_get_their_own_checks(self):
        service, store, _, _ = make()
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": "someone-else"}):
            response = self.post(client_for(service), {"Authorization": "Bearer good"})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "id_required"))

    def test_oversized_upload_is_413(self):
        service, _, _, _ = make()
        big = b"\xff\xd8\xff" + b"\0" * (6 * 1024 * 1024)
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": UID}):
            response = self.post(client_for(service), {"Authorization": "Bearer good"}, big)
        self.assertEqual(response.status_code, 413)

    def test_unconfigured_service_is_503(self):
        app = FastAPI()
        app.include_router(face_router.router)
        face_router.configure(None)
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": UID}):
            response = TestClient(app).post(
                "/api/verification/face", headers={"Authorization": "Bearer good"},
                files={"selfie": ("s.jpg", SELFIE, "image/jpeg")})
        self.assertEqual(response.status_code, 503)

    def test_legacy_verify_endpoint_is_gone(self):
        import main

        response = TestClient(main.app).post("/api/verify", json={
            "userId": UID, "status": "approved", "faceMatchPassed": True,
        })
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["replacement"], "/api/verification/face")


# --------------------------------------------------------------------------
# Cloudinary signing and the OpenCV matcher
# --------------------------------------------------------------------------


class CloudinarySignature(unittest.TestCase):
    def test_sorted_params_plus_secret_sha1(self):
        params = {"timestamp": "1315060510", "public_id": "sample_image"}
        expected = hashlib.sha1(b"public_id=sample_image&timestamp=1315060510abcd").hexdigest()
        self.assertEqual(CloudinaryUploader.signature(params, "abcd"), expected)

    def _upload(self, subfolder=""):
        uploader = CloudinaryUploader("dlk8chosr", "key", "secret", "face_verifications",
                                      SETTINGS.allowed_image_prefix)
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "secure_url": "https://res.cloudinary.com/dlk8chosr/image/upload/v1/x.jpg"}
        with mock.patch("verification.storage.requests.post", return_value=response) as post:
            uploader.upload(b"data", "s.jpg", subfolder)
        return post.call_args.kwargs["data"]

    def test_review_selfies_go_to_the_review_subfolder_signed(self):
        form = self._upload("review")
        self.assertEqual(form["folder"], "face_verifications/review")
        self.assertEqual(form["api_key"], "key")
        expected = CloudinaryUploader.signature(
            {"folder": "face_verifications/review", "timestamp": form["timestamp"]}, "secret")
        self.assertEqual(form["signature"], expected)
        self.assertNotIn("api_secret", form)  # the secret is never sent

    def test_verified_selfies_go_to_the_main_folder(self):
        self.assertEqual(self._upload()["folder"], "face_verifications")


MODELS = SETTINGS.models_dir


@unittest.skipUnless((MODELS / "face_recognition_sface_2021dec.onnx").is_file(),
                     "face models not downloaded")
class OpenCvMatcher(unittest.TestCase):
    def test_blank_selfie_has_no_face(self):
        matcher = OpenCvSFaceMatcher(MODELS)
        with self.assertRaises(NoFaceFound) as caught:
            matcher.compare(jpeg(), jpeg())
        self.assertEqual(caught.exception.which, "selfie")

    def test_undecodable_image_is_matcher_error(self):
        with self.assertRaises(MatcherError):
            OpenCvSFaceMatcher(MODELS).compare(b"junk", b"junk")


class MatcherSetup(unittest.TestCase):
    def test_missing_models_is_matcher_error(self):
        with self.assertRaises(MatcherError) as caught:
            OpenCvSFaceMatcher(MODELS / "does-not-exist").compare(jpeg(), jpeg())
        self.assertIn("download_face_models", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class PrivateIdPhoto(unittest.TestCase):
    """ID front stored privately (files/): accepted only as the user's own id_front."""

    OWN = f"secure://image/blood_connect_private/{UID}/id_front/a1.jpg"

    def test_own_private_id_photo_is_fetched_and_matched(self):
        fetched = []
        service, _, matcher, _ = make(
            store=FakeStore(submission={"frontIdImageUrl": self.OWN}),
            fetch=lambda value: fetched.append(value) or b"id-image-bytes",
        )
        outcome = service.verify(UID, SELFIE)
        self.assertNotEqual(outcome.code, Code.INVALID_ID_IMAGE_URL)
        self.assertEqual(fetched, [self.OWN])
        self.assertEqual(matcher.calls, 1)

    def test_someone_elses_or_wrong_kind_is_rejected(self):
        for value in (
            "secure://image/blood_connect_private/other-user/id_front/a1.jpg",
            f"secure://image/blood_connect_private/{UID}/id_back/a1.jpg",
            f"secure://raw/blood_connect_private/{UID}/brf/a1.pdf",
            f"secure://image/elsewhere/{UID}/id_front/a1.jpg",
        ):
            service, store, matcher, _ = make(store=FakeStore(submission={"frontIdImageUrl": value}))
            outcome = service.verify(UID, SELFIE)
            self.assertEqual((outcome.status, outcome.code), (422, Code.INVALID_ID_IMAGE_URL), value)
            self.assertEqual(matcher.calls, 0, value)
            self.assertEqual(store.quota, {}, value)
