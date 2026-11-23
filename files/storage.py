"""Private Cloudinary storage (type=private) with backend-only credentials."""

from __future__ import annotations

import time
import uuid
from typing import Callable
from urllib.parse import urlencode

import requests

from verification.storage import CloudinaryUploader, StorageError

from .refs import KINDS, SecureRef

HTTP_TIMEOUT_SECONDS = 30
API_ROOT = "https://api.cloudinary.com/v1_1"


def _signed(params: dict, api_key: str, api_secret: str) -> dict:
    """Drop empty values, then add api_key and the Cloudinary signature."""
    params = {key: str(value) for key, value in params.items() if value not in (None, "")}
    return {**params, "api_key": api_key,
            "signature": CloudinaryUploader.signature(params, api_secret)}


class PrivateCloudinary:
    def __init__(self, cloud_name: str, api_key: str, api_secret: str, folder: str,
                 clock: Callable[[], float] = time.time,
                 new_name: Callable[[], str] = lambda: uuid.uuid4().hex):
        self._cloud = cloud_name
        self._key = api_key
        self._secret = api_secret
        self.folder = folder
        self._clock = clock
        self._new_name = new_name

    def _endpoint(self, resource_type: str, action: str) -> str:
        return f"{API_ROOT}/{self._cloud}/{resource_type}/{action}"

    def upload(self, data: bytes, uid: str, kind: str, extension: str) -> SecureRef:
        """Upload as type=private under <folder>/<uid>/<kind>/ and return its reference."""
        resource_type = KINDS[kind][0]
        ref = SecureRef(resource_type, self.folder, uid, kind, f"{self._new_name()}.{extension}")
        params = {
            "public_id": ref.public_id,
            "type": "private",
            "overwrite": "false",
            "timestamp": int(self._clock()),
        }
        if resource_type == "image":
            params["format"] = ref.extension
        try:
            response = requests.post(
                self._endpoint(resource_type, "upload"),
                data=_signed(params, self._key, self._secret),
                files={"file": (ref.name, data)},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise StorageError(f"Private upload failed: {error}") from error
        if response.status_code != 200:
            raise StorageError(f"Private upload returned HTTP {response.status_code}.")
        body = response.json() or {}
        if body.get("type") != "private" or body.get("public_id") != ref.public_id:
            raise StorageError("Private upload returned an unexpected asset.")
        return ref

    def signed_url(self, ref: SecureRef, ttl_seconds: int) -> str:
        """A download URL for a private asset that stops working after ttl_seconds."""
        now = int(self._clock())
        params = _signed({
            "public_id": ref.public_id,
            "format": ref.format,
            "type": "private",
            "timestamp": now,
            "expires_at": now + ttl_seconds,
        }, self._key, self._secret)
        return f"{self._endpoint(ref.resource_type, 'download')}?{urlencode(params)}"

    def download(self, ref: SecureRef, max_bytes: int) -> bytes:
        """Fetch a private asset server-side (face matching reads the ID photo)."""
        url = self.signed_url(ref, 60)
        try:
            with requests.get(url, timeout=HTTP_TIMEOUT_SECONDS, stream=True) as response:
                if response.status_code != 200:
                    raise StorageError(f"Private download returned HTTP {response.status_code}.")
                data = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise StorageError("File is too large.")
                return bytes(data)
        except requests.RequestException as error:
            raise StorageError(f"Private download failed: {error}") from error

    def destroy(self, ref: SecureRef) -> bool:
        """Delete a private asset. True when deleted or already gone."""
        params = _signed({"public_id": ref.public_id, "type": "private",
                          "timestamp": int(self._clock())}, self._key, self._secret)
        try:
            response = requests.post(self._endpoint(ref.resource_type, "destroy"), data=params,
                                     timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as error:
            raise StorageError(f"Private delete failed: {error}") from error
        if response.status_code != 200:
            return False
        return (response.json() or {}).get("result") in ("ok", "not found")
