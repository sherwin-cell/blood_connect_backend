"""POST /api/verification/face logic, independent of FastAPI and Firestore.

Order of checks for a signed-in user:

1. Already verified      -> 200, existing result, nothing re-run.
2. No ID on file         -> 409 id_required (upload ID first).
3. ID URL not ours       -> 422 invalid_id_image_url.
4. Bad selfie upload     -> 400 invalid_selfie / 413 selfie_too_large.
5. Not configured        -> 503 not_configured.
6. Daily limit reached   -> 429 attempt_limit.
7. Download ID, compare, and on a pass upload the selfie and mark the user
   verified; on a fail record the score for PRC review.

Only attempts that reach the comparison are counted toward the daily limit,
and of those, server-side failures (ID download, matcher, upload) and an ID
photo with no detectable face give the attempt back. Every request, including
rejected ones, is logged to face_verification_attempts.

A selfie that was compared but did not pass (no match, no face found) is
uploaded to the Cloudinary "review" subfolder and its URL stored on the
attempt record only, so PRC can compare it with the ID in the admin portal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from notifications.constants import PH_TZ

from .config import FaceVerificationSettings
from .images import InvalidImage, is_allowed_image_url, validate_selfie
from .matcher import FaceMatcher, MatcherError, NoFaceFound
from .storage import (
    SERVER_TIMESTAMP_MARKER,
    AttemptLimitReached,
    StorageError,
    VerificationStore,
)

log = logging.getLogger(__name__)


class Code:
    VERIFIED = "verified"
    NOT_MATCHED = "not_matched"
    ALREADY_VERIFIED = "already_verified"
    ID_REQUIRED = "id_required"
    INVALID_ID_IMAGE_URL = "invalid_id_image_url"
    INVALID_SELFIE = "invalid_selfie"
    SELFIE_TOO_LARGE = "selfie_too_large"
    NOT_CONFIGURED = "not_configured"
    ATTEMPT_LIMIT = "attempt_limit"
    ID_IMAGE_UNAVAILABLE = "id_image_unavailable"
    NO_FACE_IN_SELFIE = "no_face_in_selfie"
    NO_FACE_IN_ID = "no_face_in_id"
    MATCHER_UNAVAILABLE = "matcher_unavailable"
    UPLOAD_FAILED = "upload_failed"


MESSAGES = {
    Code.VERIFIED: "Your face matches your ID. Your account is now verified.",
    Code.NOT_MATCHED: "Your selfie did not match the photo on your ID. "
                      "Retake it in good light, facing the camera.",
    Code.ALREADY_VERIFIED: "Your account is already verified.",
    Code.ID_REQUIRED: "Upload your valid ID before taking your selfie.",
    Code.INVALID_ID_IMAGE_URL: "Your saved ID image could not be used. PRC will review your ID.",
    Code.INVALID_SELFIE: "The selfie must be a clear JPEG or PNG photo.",
    Code.SELFIE_TOO_LARGE: "The selfie photo is too large. Please retake it.",
    Code.NOT_CONFIGURED: "Face verification is not available right now. Please try again later.",
    Code.ATTEMPT_LIMIT: "You have used all face verification attempts for today. "
                        "Try again tomorrow, or wait for PRC to review your account.",
    Code.ID_IMAGE_UNAVAILABLE: "Your ID image could not be loaded. Please try again later.",
    Code.NO_FACE_IN_SELFIE: "No face was found in your selfie. Please retake it.",
    Code.NO_FACE_IN_ID: "No face could be found on your ID photo. PRC will review your ID.",
    Code.MATCHER_UNAVAILABLE: "Face verification is not available right now. Please try again later.",
    Code.UPLOAD_FAILED: "Your selfie could not be saved. Please try again.",
}


@dataclass
class Outcome:
    status: int
    code: str
    passed: bool = False
    score: Optional[float] = None
    threshold: Optional[float] = None
    attempts_remaining: Optional[int] = None
    provider: Optional[str] = None
    already_verified: bool = False
    extra: dict = field(default_factory=dict)

    def body(self) -> dict:
        return {
            "passed": self.passed,
            "code": self.code,
            "message": MESSAGES.get(self.code, ""),
            "score": None if self.score is None else round(self.score, 4),
            "threshold": self.threshold,
            "attemptsRemaining": self.attempts_remaining,
            "provider": self.provider,
            "alreadyVerified": self.already_verified,
            **self.extra,
        }


# upload(data, filename[, subfolder]) -> https URL on our Cloudinary account
Uploader = Callable[..., str]

# Cloudinary subfolder for selfies that did not pass, kept for PRC review.
REVIEW_FOLDER = "review"
Fetcher = Callable[[str], bytes]


class FaceVerificationService:
    def __init__(
        self,
        store: VerificationStore,
        matcher: FaceMatcher,
        settings: FaceVerificationSettings,
        fetch_id_image: Fetcher,
        upload_selfie: Optional[Uploader],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._store = store
        self._matcher = matcher
        self._settings = settings
        self._fetch = fetch_id_image
        self._upload = upload_selfie
        self._clock = clock

    @property
    def settings(self) -> FaceVerificationSettings:
        return self._settings

    def _day(self) -> str:
        return self._clock().astimezone(PH_TZ).date().isoformat()

    def _remaining(self, used: int) -> int:
        return max(self._settings.max_attempts_per_day - used, 0)

    def verify(self, uid: str, selfie: bytes) -> Outcome:
        day = self._day()
        record = {"userId": uid, "day": day, "counted": False,
                  "provider": self._matcher.provider,
                  "threshold": self._settings.threshold}

        def finish(outcome: Outcome, **fields) -> Outcome:
            try:
                self._store.log_attempt({
                    **record, **fields,
                    "outcome": outcome.code,
                    "passed": outcome.passed,
                    "score": outcome.score,
                    "httpStatus": outcome.status,
                })
            except Exception:
                log.exception("Could not log face verification attempt for %s", uid)
            return outcome

        user = self._store.get_user(uid) or {}

        # 1. Already verified: never re-run, never count.
        if user.get("faceVerified") is True:
            existing = user.get("faceVerification") or {}
            return finish(Outcome(
                200, Code.ALREADY_VERIFIED, passed=True, already_verified=True,
                score=existing.get("score"), provider=existing.get("provider"),
                threshold=existing.get("threshold"),
            ))

        # 2-3. The ID photo must already be on file, on our Cloudinary account.
        submission = self._store.get_id_submission(uid) or {}
        id_url = submission.get("frontIdImageUrl")
        record["idImageUrl"] = id_url if isinstance(id_url, str) else None
        if not isinstance(id_url, str) or not id_url.strip():
            return finish(Outcome(409, Code.ID_REQUIRED))
        if not is_allowed_image_url(id_url, self._settings.allowed_image_prefix):
            return finish(Outcome(422, Code.INVALID_ID_IMAGE_URL))

        # 4. Selfie upload.
        try:
            kind = validate_selfie(selfie, self._settings.max_selfie_bytes)
        except InvalidImage as error:
            status = 413 if error.code == Code.SELFIE_TOO_LARGE else 400
            return finish(Outcome(status, error.code))

        # 5. Without upload credentials a pass could not be saved; do not burn attempts.
        if self._upload is None:
            return finish(Outcome(503, Code.NOT_CONFIGURED))

        # 6. Daily limit.
        try:
            used = self._store.reserve_attempt(uid, day, self._settings.max_attempts_per_day)
        except AttemptLimitReached:
            return finish(Outcome(429, Code.ATTEMPT_LIMIT, attempts_remaining=0))
        remaining = self._remaining(used)
        record["counted"] = True

        def refund(outcome: Outcome, **fields) -> Outcome:
            try:
                self._store.release_attempt(uid, day)
                record["counted"] = False
                outcome.attempts_remaining = remaining + 1
            except Exception:
                log.exception("Could not release face verification attempt for %s", uid)
                outcome.attempts_remaining = remaining
            return finish(outcome, **fields)

        # 7. Compare.
        try:
            id_image = self._fetch(id_url)
        except StorageError as error:
            log.warning("ID image fetch failed for %s: %s", uid, error)
            return refund(Outcome(502, Code.ID_IMAGE_UNAVAILABLE), error=str(error)[:300])

        extension = "png" if kind == "png" else "jpg"

        def store_for_review() -> dict:
            """Keep a selfie that did not pass so PRC can compare it with the ID.

            The URL goes only on the admin-only attempt record, never on
            users/{uid} (readable by every signed-in user). A failed upload
            does not change the user's result.
            """
            try:
                url = self._upload(selfie, f"review_{uid}.{extension}", REVIEW_FOLDER)
                return {"selfieImageUrl": url, "savedForReview": True}
            except StorageError as error:
                log.warning("Review selfie upload failed for %s: %s", uid, error)
                return {"savedForReview": False, "reviewUploadError": str(error)[:300]}

        def reviewed(outcome: Outcome, stored: dict) -> Outcome:
            outcome.extra["savedForReview"] = stored.get("savedForReview", False)
            return finish(outcome, **stored)

        try:
            match = self._matcher.compare(id_image, selfie)
        except NoFaceFound as error:
            stored = store_for_review()
            if error.which == "id":
                outcome = Outcome(422, Code.NO_FACE_IN_ID)
                outcome.extra["savedForReview"] = stored.get("savedForReview", False)
                return refund(outcome, **stored)
            return reviewed(Outcome(422, Code.NO_FACE_IN_SELFIE, attempts_remaining=remaining),
                            stored)
        except MatcherError as error:
            log.error("Face matcher failed for %s: %s", uid, error)
            return refund(Outcome(503, Code.MATCHER_UNAVAILABLE), error=str(error)[:300])

        threshold = self._settings.threshold
        passed = match.score >= threshold
        result = {"score": match.score, "threshold": threshold, "provider": match.provider}

        if not passed:
            stored = store_for_review()
            self._store.save_result(uid, {
                "faceVerified": False,
                "faceVerification": {
                    **result,
                    "passed": False,
                    "result": "failed",
                    "method": "automatic",
                    "checkedAt": SERVER_TIMESTAMP_MARKER,
                },
                "updatedAt": SERVER_TIMESTAMP_MARKER,
            })
            return reviewed(Outcome(200, Code.NOT_MATCHED, passed=False, score=match.score,
                                    threshold=threshold, provider=match.provider,
                                    attempts_remaining=remaining),
                            stored)

        try:
            selfie_url = self._upload(selfie, f"selfie_{uid}.{extension}")
        except StorageError as error:
            log.warning("Selfie upload failed for %s: %s", uid, error)
            return refund(Outcome(502, Code.UPLOAD_FAILED, score=match.score,
                                  threshold=threshold, provider=match.provider),
                          error=str(error)[:300])

        self._store.save_result(uid, {
            "faceVerified": True,
            "selfieImageUrl": selfie_url,
            "faceVerification": {
                **result,
                "passed": True,
                "result": "verified",
                "method": "automatic",
                "selfieImageUrl": selfie_url,
                "verifiedAt": SERVER_TIMESTAMP_MARKER,
                "checkedAt": SERVER_TIMESTAMP_MARKER,
            },
            "updatedAt": SERVER_TIMESTAMP_MARKER,
        })
        return finish(Outcome(200, Code.VERIFIED, passed=True, score=match.score,
                              threshold=threshold, provider=match.provider,
                              attempts_remaining=remaining),
                      selfieImageUrl=selfie_url)
