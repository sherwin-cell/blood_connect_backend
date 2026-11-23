"""Upload and view private files (pure logic; storage and admin check injected)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from verification.images import image_kind
from verification.storage import StorageError

from .refs import KINDS, SecureRef, parse_ref

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


class Code:
    UPLOADED = "uploaded"
    OK = "ok"
    INVALID_KIND = "invalid_kind"
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_FILE_TYPE = "invalid_file_type"
    INVALID_REF = "invalid_ref"
    FORBIDDEN = "forbidden"
    NOT_CONFIGURED = "not_configured"
    STORAGE_FAILED = "storage_failed"


@dataclass(frozen=True)
class FileSettings:
    folder: str = "blood_connect_private"
    url_ttl_seconds: int = 600
    max_image_bytes: int = 10 * 1024 * 1024
    max_pdf_bytes: int = 10 * 1024 * 1024

    @staticmethod
    def from_env() -> "FileSettings":
        def number(name: str, default: int) -> int:
            try:
                return max(int(os.getenv(name, default)), 1)
            except ValueError:
                return default

        return FileSettings(
            folder=os.getenv("CLOUDINARY_PRIVATE_FOLDER", "blood_connect_private").strip()
            or "blood_connect_private",
            url_ttl_seconds=number("PRIVATE_FILE_URL_TTL_SECONDS", 600),
        )

    def max_bytes(self, kind: str) -> int:
        return self.max_pdf_bytes if KINDS[kind][0] == "raw" else self.max_image_bytes


class PrivateStorage(Protocol):
    folder: str
    def upload(self, data: bytes, uid: str, kind: str, extension: str) -> SecureRef: ...
    def signed_url(self, ref: SecureRef, ttl_seconds: int) -> str: ...


@dataclass
class Outcome:
    status: int
    code: str
    body: dict = field(default_factory=dict)

    def json(self) -> dict:
        return {"code": self.code, **self.body}


def detect_extension(kind: str, data: bytes) -> Optional[str]:
    """The file's real type, from its first bytes (not its name)."""
    if KINDS[kind][0] == "raw":
        return "pdf" if data.startswith(PDF_MAGIC) else None
    found = image_kind(data)
    return {"jpeg": "jpg", "png": "png"}.get(found or "")


class FileService:
    def __init__(self, storage: Optional[PrivateStorage], is_admin: Callable[[str], bool],
                 settings: FileSettings):
        self._storage = storage
        self._is_admin = is_admin
        self.settings = settings

    def upload(self, uid: str, kind: str, data: bytes) -> Outcome:
        if kind not in KINDS:
            return Outcome(400, Code.INVALID_KIND, {"message": "Unknown upload type."})
        if not data:
            return Outcome(400, Code.EMPTY_FILE, {"message": "The file is empty."})
        limit = self.settings.max_bytes(kind)
        if len(data) > limit:
            return Outcome(413, Code.FILE_TOO_LARGE,
                           {"message": f"The file must be at most {limit // (1024 * 1024)} MB."})
        extension = detect_extension(kind, data)
        if extension is None:
            wanted = "a PDF" if kind == "brf" else "a JPEG or PNG photo"
            return Outcome(400, Code.INVALID_FILE_TYPE, {"message": f"The file must be {wanted}."})
        if self._storage is None:
            return Outcome(503, Code.NOT_CONFIGURED, {"message": "Secure uploads are not configured."})
        try:
            ref = self._storage.upload(data, uid, kind, extension)
        except StorageError as error:
            log.warning("Private upload failed for %s (%s): %s", uid, kind, error)
            return Outcome(502, Code.STORAGE_FAILED, {"message": "Upload failed. Please try again."})
        return Outcome(200, Code.UPLOADED, {"ref": str(ref)})

    def view(self, caller: dict, value: str) -> Outcome:
        """Signed short-lived URL for the file's owner or a PRC admin."""
        ref = parse_ref(value, self.settings.folder)
        if ref is None:
            return Outcome(400, Code.INVALID_REF, {"message": "Not a Blood-Connect file reference."})
        caller_uid = caller.get("uid")
        allowed = bool(caller_uid) and (
            caller_uid == ref.uid or caller.get("admin") is True or self._is_admin(caller_uid)
        )
        if not allowed:
            return Outcome(403, Code.FORBIDDEN, {"message": "You cannot view this file."})
        if self._storage is None:
            return Outcome(503, Code.NOT_CONFIGURED, {"message": "Secure files are not configured."})
        ttl = self.settings.url_ttl_seconds
        return Outcome(200, Code.OK, {"url": self._storage.signed_url(ref, ttl), "expiresIn": ttl})
