"""Checks on images before they reach the matcher."""

from __future__ import annotations

from urllib.parse import urlsplit

import cv2
import numpy as np

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_SIDE = 64


class InvalidImage(ValueError):
    """The upload is not a usable JPEG/PNG photo. `code` is sent to the app."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def image_kind(data: bytes) -> str | None:
    if data.startswith(JPEG_MAGIC):
        return "jpeg"
    if data.startswith(PNG_MAGIC):
        return "png"
    return None


def decode(data: bytes) -> np.ndarray | None:
    if not data:
        return None
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    return image if image is not None and image.size else None


def validate_selfie(data: bytes, max_bytes: int) -> str:
    """Return "jpeg" or "png", or raise InvalidImage."""
    if not data:
        raise InvalidImage("invalid_selfie", "The selfie upload is empty.")
    if len(data) > max_bytes:
        raise InvalidImage(
            "selfie_too_large",
            f"The selfie must be at most {max_bytes // (1024 * 1024)} MB.",
        )
    kind = image_kind(data)
    if kind is None:
        raise InvalidImage("invalid_selfie", "The selfie must be a JPEG or PNG image.")
    image = decode(data)
    if image is None:
        raise InvalidImage("invalid_selfie", "The selfie image could not be read.")
    height, width = image.shape[:2]
    if min(height, width) < MIN_SIDE:
        raise InvalidImage("invalid_selfie", "The selfie image is too small.")
    return kind


def is_allowed_image_url(url: object, allowed_prefix: str) -> bool:
    """True only for https URLs under our Cloudinary account."""
    if not isinstance(url, str) or not url.startswith(allowed_prefix):
        return False
    parts = urlsplit(url)
    allowed = urlsplit(allowed_prefix)
    return (
        parts.scheme == "https"
        and parts.hostname == allowed.hostname
        and parts.port is None
        and not parts.username
        and not parts.password
        and ".." not in parts.path
    )
