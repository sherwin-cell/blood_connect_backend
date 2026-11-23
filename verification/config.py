"""Face verification settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from notifications.config import _int

ROOT = Path(__file__).resolve().parent.parent

# SFace's published cosine-similarity threshold for "same person".
DEFAULT_THRESHOLD = 0.363


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class FaceVerificationSettings:
    # Cosine similarity at or above this passes (SFace scores roughly -1..1).
    threshold: float = DEFAULT_THRESHOLD
    # Counted attempts per user per Philippines calendar day.
    max_attempts_per_day: int = 5
    max_selfie_bytes: int = 5 * 1024 * 1024
    max_id_image_bytes: int = 10 * 1024 * 1024
    # ID photos must come from our own Cloudinary account.
    cloudinary_cloud_name: str = "dlk8chosr"
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    cloudinary_folder: str = "face_verifications"
    # Private uploads (ID photos, Blood Request Forms): see files/.
    private_folder: str = "blood_connect_private"
    # YuNet detection confidence needed to accept a face (0..1).
    detection_score_threshold: float = 0.8
    models_dir: Path = ROOT / "models"

    @property
    def allowed_image_prefix(self) -> str:
        return f"https://res.cloudinary.com/{self.cloudinary_cloud_name}/"

    @property
    def can_upload(self) -> bool:
        return bool(self.cloudinary_api_key and self.cloudinary_api_secret)

    @staticmethod
    def from_env() -> "FaceVerificationSettings":
        return FaceVerificationSettings(
            threshold=_float("FACE_MATCH_THRESHOLD", DEFAULT_THRESHOLD),
            max_attempts_per_day=max(_int("FACE_MATCH_MAX_ATTEMPTS_PER_DAY", 5), 1),
            cloudinary_cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", "dlk8chosr").strip(),
            cloudinary_api_key=os.getenv("CLOUDINARY_API_KEY", "").strip(),
            cloudinary_api_secret=os.getenv("CLOUDINARY_API_SECRET", "").strip(),
            cloudinary_folder=os.getenv("CLOUDINARY_FACE_FOLDER", "face_verifications").strip(),
            private_folder=os.getenv("CLOUDINARY_PRIVATE_FOLDER", "blood_connect_private").strip()
            or "blood_connect_private",
            detection_score_threshold=_float("FACE_DETECTION_SCORE_THRESHOLD", 0.8),
            models_dir=Path(os.getenv("FACE_MODELS_DIR", str(ROOT / "models"))),
        )
