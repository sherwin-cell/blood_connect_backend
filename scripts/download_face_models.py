"""Download the open-source face models used by verification/matcher.py.

    python scripts/download_face_models.py

Both models come from the OpenCV Model Zoo (Apache-2.0):
- YuNet face detector
- SFace face recognizer

They are saved to models/ (git-ignored). Each file is checked against a pinned
SHA-256 so a tampered or truncated download is never used.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "face_recognition_sface_2021dec.onnx": (
        f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    MODELS_DIR.mkdir(exist_ok=True)
    for name, (url, expected) in MODELS.items():
        target = MODELS_DIR / name
        if target.exists() and sha256(target) == expected:
            print(f"ok       {name}")
            continue
        print(f"download {name}")
        partial = target.with_suffix(".part")
        urllib.request.urlretrieve(url, partial)
        actual = sha256(partial)
        if actual != expected:
            partial.unlink(missing_ok=True)
            print(f"ERROR: {name} checksum mismatch ({actual})", file=sys.stderr)
            return 1
        partial.replace(target)
        print(f"ok       {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
