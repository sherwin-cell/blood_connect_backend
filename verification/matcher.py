"""Face matching behind a small interface, so the library can be swapped.

The default, OpenCvSFaceMatcher, uses two open-source models from the OpenCV
Model Zoo (Apache-2.0): YuNet to find faces and SFace to compare them. It
runs locally, needs no API key, and costs nothing per call. Download the
models with `python scripts/download_face_models.py`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from .images import decode

YUNET_MODEL = "face_detection_yunet_2023mar.onnx"
SFACE_MODEL = "face_recognition_sface_2021dec.onnx"

# Large photos are shrunk before detection; faces stay far above SFace's 112px.
MAX_DETECTION_SIDE = 1280


@dataclass(frozen=True)
class MatchResult:
    # Similarity of the two faces; compared with FACE_MATCH_THRESHOLD.
    score: float
    provider: str


class NoFaceFound(Exception):
    """No face could be found in one image. `which` is "selfie" or "id"."""

    def __init__(self, which: str):
        super().__init__(f"No face found in the {which} image.")
        self.which = which


class MatcherError(Exception):
    """The matcher itself failed (missing models, unreadable image, ...)."""


class FaceMatcher(Protocol):
    provider: str

    def compare(self, id_image: bytes, selfie_image: bytes) -> MatchResult:
        """Compare the main face on the ID with the main face in the selfie.

        Raises NoFaceFound or MatcherError.
        """


class OpenCvSFaceMatcher:
    provider = "opencv_sface"

    def __init__(self, models_dir: Path, detection_score_threshold: float = 0.8):
        self._models_dir = Path(models_dir)
        self._score_threshold = detection_score_threshold
        self._detector = None
        self._recognizer = None
        # OpenCV detector/recognizer objects are not safe to share across threads.
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._detector is not None:
            return
        detector_path = self._models_dir / YUNET_MODEL
        recognizer_path = self._models_dir / SFACE_MODEL
        missing = [p.name for p in (detector_path, recognizer_path) if not p.is_file()]
        if missing:
            raise MatcherError(
                f"Face models missing ({', '.join(missing)}); "
                "run python scripts/download_face_models.py"
            )
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(detector_path), "", (320, 320), self._score_threshold, 0.3, 5000
            )
            self._recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        except cv2.error as error:
            self._detector = self._recognizer = None
            raise MatcherError(f"Could not load face models: {error}") from error

    @staticmethod
    def _shrink(image: np.ndarray) -> np.ndarray:
        height, width = image.shape[:2]
        scale = MAX_DETECTION_SIDE / max(height, width)
        if scale >= 1:
            return image
        return cv2.resize(image, (round(width * scale), round(height * scale)),
                          interpolation=cv2.INTER_AREA)

    def _embedding(self, data: bytes, which: str) -> np.ndarray:
        image = decode(data)
        if image is None:
            raise MatcherError(f"The {which} image could not be decoded.")
        image = self._shrink(image)
        height, width = image.shape[:2]
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(image)
        if faces is None or len(faces) == 0:
            raise NoFaceFound(which)
        # The largest face is the subject (IDs can carry a small ghost photo).
        face = max(faces, key=lambda f: float(f[2]) * float(f[3]))
        aligned = self._recognizer.alignCrop(image, face)
        return self._recognizer.feature(aligned)

    def compare(self, id_image: bytes, selfie_image: bytes) -> MatchResult:
        with self._lock:
            self._load()
            try:
                selfie = self._embedding(selfie_image, "selfie")
                id_face = self._embedding(id_image, "id")
                score = self._recognizer.match(selfie, id_face, cv2.FaceRecognizerSF_FR_COSINE)
            except (NoFaceFound, MatcherError):
                raise
            except cv2.error as error:
                raise MatcherError(f"Face comparison failed: {error}") from error
        return MatchResult(score=float(score), provider=self.provider)
