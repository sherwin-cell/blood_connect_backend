"""Image download/upload and the Firestore reads/writes face verification needs.

FirestoreVerificationStore is a thin adapter so the service logic can be unit
tested against an in-memory fake.
"""

from __future__ import annotations

import hashlib
import time
from typing import Optional, Protocol

import requests
from firebase_admin import firestore

from .images import is_allowed_image_url

ATTEMPTS = "face_verification_attempts"
QUOTA = "face_verification_quota"
USERS = "users"
ID_VERIFICATIONS = "id_verifications"

HTTP_TIMEOUT_SECONDS = 15


class StorageError(Exception):
    """Downloading the ID image or uploading the selfie failed."""


class AttemptLimitReached(Exception):
    pass


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def fetch_image(url: str, allowed_prefix: str, max_bytes: int) -> bytes:
    """Download an image from our Cloudinary account, refusing anything else."""
    if not is_allowed_image_url(url, allowed_prefix):
        raise StorageError("Image URL is not on the allowed Cloudinary account.")
    try:
        # No redirects: a redirect could leave the allowed host.
        with requests.get(url, timeout=HTTP_TIMEOUT_SECONDS, allow_redirects=False,
                          stream=True) as response:
            if response.status_code != 200:
                raise StorageError(f"Image download returned HTTP {response.status_code}.")
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise StorageError("Image is too large.")
            return bytes(data)
    except requests.RequestException as error:
        raise StorageError(f"Image download failed: {error}") from error


class CloudinaryUploader:
    """Signed upload with backend-only credentials (never shipped in the app)."""

    def __init__(self, cloud_name: str, api_key: str, api_secret: str, folder: str,
                 allowed_prefix: str):
        self._cloud_name = cloud_name
        self._api_key = api_key
        self._api_secret = api_secret
        self._folder = folder
        self._allowed_prefix = allowed_prefix

    @staticmethod
    def signature(params: dict, api_secret: str) -> str:
        """Cloudinary signature: sorted key=value pairs joined by &, plus the secret."""
        payload = "&".join(f"{key}={params[key]}" for key in sorted(params))
        return hashlib.sha1(f"{payload}{api_secret}".encode()).hexdigest()

    def upload(self, data: bytes, filename: str, subfolder: str = "") -> str:
        folder = f"{self._folder}/{subfolder}" if subfolder else self._folder
        params = {"folder": folder, "timestamp": str(int(time.time()))}
        form = {**params, "api_key": self._api_key,
                "signature": self.signature(params, self._api_secret)}
        url = f"https://api.cloudinary.com/v1_1/{self._cloud_name}/image/upload"
        try:
            response = requests.post(url, data=form, files={"file": (filename, data)},
                                     timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as error:
            raise StorageError(f"Selfie upload failed: {error}") from error
        if response.status_code != 200:
            raise StorageError(f"Selfie upload returned HTTP {response.status_code}.")
        secure_url = (response.json() or {}).get("secure_url")
        if not is_allowed_image_url(secure_url, self._allowed_prefix):
            raise StorageError("Selfie upload returned an unexpected URL.")
        return secure_url

    @staticmethod
    def public_id_from_url(url: str, allowed_prefix: str) -> Optional[str]:
        """`.../image/upload/v123/folder/name.jpg` -> `folder/name`, or None
        for URLs outside our Cloudinary account."""
        if not is_allowed_image_url(url, allowed_prefix):
            return None
        marker = "/image/upload/"
        if marker not in url:
            return None
        path = url.split(marker, 1)[1].split("?", 1)[0]
        parts = path.split("/")
        if parts and len(parts[0]) > 1 and parts[0][0] == "v" and parts[0][1:].isdigit():
            parts = parts[1:]
        if not parts or not parts[-1]:
            return None
        name = parts[-1].rsplit(".", 1)[0]
        return "/".join([*parts[:-1], name]) or None

    def destroy(self, url: str) -> bool:
        """Delete an image we host. True when Cloudinary deleted it or it was
        already gone; False for URLs that are not ours or on failure."""
        public_id = self.public_id_from_url(url, self._allowed_prefix)
        if public_id is None:
            return False
        params = {"public_id": public_id, "timestamp": str(int(time.time()))}
        form = {**params, "api_key": self._api_key,
                "signature": self.signature(params, self._api_secret)}
        endpoint = f"https://api.cloudinary.com/v1_1/{self._cloud_name}/image/destroy"
        try:
            response = requests.post(endpoint, data=form, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as error:
            raise StorageError(f"Image delete failed: {error}") from error
        if response.status_code != 200:
            return False
        return (response.json() or {}).get("result") in ("ok", "not found")


# --------------------------------------------------------------------------
# Firestore
# --------------------------------------------------------------------------


class VerificationStore(Protocol):
    def get_user(self, uid: str) -> Optional[dict]: ...
    def get_id_submission(self, uid: str) -> Optional[dict]: ...
    def attempts_used(self, uid: str, day: str) -> int: ...
    def reserve_attempt(self, uid: str, day: str, max_attempts: int) -> int: ...
    def release_attempt(self, uid: str, day: str) -> None: ...
    def log_attempt(self, record: dict) -> str: ...
    def save_result(self, uid: str, fields: dict) -> bool: ...


def _quota_id(uid: str, day: str) -> str:
    return f"{uid}_{day}"


class FirestoreVerificationStore:
    def __init__(self, db):
        self._db = db

    def get_user(self, uid: str) -> Optional[dict]:
        snapshot = self._db.collection(USERS).document(uid).get()
        return snapshot.to_dict() if snapshot.exists else None

    def get_id_submission(self, uid: str) -> Optional[dict]:
        snapshot = self._db.collection(ID_VERIFICATIONS).document(uid).get()
        return snapshot.to_dict() if snapshot.exists else None

    def attempts_used(self, uid: str, day: str) -> int:
        snapshot = self._db.collection(QUOTA).document(_quota_id(uid, day)).get()
        return int((snapshot.to_dict() or {}).get("count", 0)) if snapshot.exists else 0

    def reserve_attempt(self, uid: str, day: str, max_attempts: int) -> int:
        """Count one attempt for today; returns attempts used. Raises AttemptLimitReached.

        A transaction, so two simultaneous requests cannot both take the last slot.
        """
        ref = self._db.collection(QUOTA).document(_quota_id(uid, day))

        @firestore.transactional
        def reserve(transaction) -> int:
            snapshot = ref.get(transaction=transaction)
            used = int((snapshot.to_dict() or {}).get("count", 0)) if snapshot.exists else 0
            if used >= max_attempts:
                raise AttemptLimitReached()
            transaction.set(ref, {
                "userId": uid,
                "day": day,
                "count": used + 1,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            })
            return used + 1

        return reserve(self._db.transaction())

    def release_attempt(self, uid: str, day: str) -> None:
        """Give back an attempt that failed for a server-side reason."""
        ref = self._db.collection(QUOTA).document(_quota_id(uid, day))

        @firestore.transactional
        def release(transaction) -> None:
            snapshot = ref.get(transaction=transaction)
            used = int((snapshot.to_dict() or {}).get("count", 0)) if snapshot.exists else 0
            if used > 0:
                transaction.update(ref, {"count": used - 1,
                                         "updatedAt": firestore.SERVER_TIMESTAMP})

        release(self._db.transaction())

    def log_attempt(self, record: dict) -> str:
        ref = self._db.collection(ATTEMPTS).document()
        ref.set({**record, "createdAt": firestore.SERVER_TIMESTAMP})
        return ref.id

    def save_result(self, uid: str, fields: dict) -> bool:
        """Merge `fields` into users/{uid} unless the user is already verified
        (e.g. PRC approved them meanwhile). Returns False if skipped."""
        ref = self._db.collection(USERS).document(uid)
        fields = _with_server_timestamps(fields)

        @firestore.transactional
        def save(transaction) -> bool:
            snapshot = ref.get(transaction=transaction)
            if snapshot.exists and (snapshot.to_dict() or {}).get("faceVerified") is True:
                return False
            transaction.set(ref, fields, merge=True)
            return True

        return save(self._db.transaction())


SERVER_TIMESTAMP_MARKER = "__server_timestamp__"


def _with_server_timestamps(value):
    """Replace SERVER_TIMESTAMP_MARKER (used by the pure service) with the sentinel."""
    if value == SERVER_TIMESTAMP_MARKER:
        return firestore.SERVER_TIMESTAMP
    if isinstance(value, dict):
        return {key: _with_server_timestamps(item) for key, item in value.items()}
    return value
