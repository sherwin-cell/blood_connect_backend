"""POST /api/verification/face"""

from __future__ import annotations

from functools import partial
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from auth import verify_firebase_token

from .config import FaceVerificationSettings
from .matcher import OpenCvSFaceMatcher
from .service import FaceVerificationService
from files.refs import is_secure_ref, parse_ref
from files.router import build_private_storage

from .storage import CloudinaryUploader, FirestoreVerificationStore, StorageError, fetch_image

router = APIRouter(prefix="/api/verification", tags=["Identity Verification"])

_service: Optional[FaceVerificationService] = None


def build_service(db, settings: Optional[FaceVerificationSettings] = None) -> FaceVerificationService:
    settings = settings or FaceVerificationSettings.from_env()
    uploader = (
        CloudinaryUploader(
            settings.cloudinary_cloud_name,
            settings.cloudinary_api_key,
            settings.cloudinary_api_secret,
            settings.cloudinary_folder,
            settings.allowed_image_prefix,
        ).upload
        if settings.can_upload else None
    )
    fetch_public = partial(fetch_image, allowed_prefix=settings.allowed_image_prefix,
                           max_bytes=settings.max_id_image_bytes)
    private = build_private_storage()

    def fetch_id_image(value: str) -> bytes:
        """ID front photo: a private secure:// upload, or a legacy public URL."""
        if not is_secure_ref(value):
            return fetch_public(value)
        ref = parse_ref(value, settings.private_folder)
        if ref is None or private is None:
            raise StorageError("Private ID photo is not readable here.")
        return private.download(ref, settings.max_id_image_bytes)

    return FaceVerificationService(
        store=FirestoreVerificationStore(db),
        matcher=OpenCvSFaceMatcher(settings.models_dir, settings.detection_score_threshold),
        settings=settings,
        fetch_id_image=fetch_id_image,
        upload_selfie=uploader,
    )


def configure(service: Optional[FaceVerificationService]) -> None:
    global _service
    _service = service


def get_service() -> FaceVerificationService:
    if _service is None:
        raise HTTPException(status_code=503, detail="Face verification is not configured.")
    return _service


@router.post("/face")
def verify_face(
    selfie: UploadFile = File(..., description="Selfie photo, JPEG or PNG, at most 5 MB."),
    uid: str = Depends(verify_firebase_token),
    service: FaceVerificationService = Depends(get_service),
):
    """Compare the caller's selfie with the front photo of their saved ID.

    A sync endpoint: FastAPI runs it in a worker thread, so the CPU-bound
    match does not block the event loop.
    """
    # Read at most one byte past the limit so an oversized upload is rejected
    # without loading all of it.
    data = selfie.file.read(service.settings.max_selfie_bytes + 1)
    outcome = service.verify(uid, data)
    return JSONResponse(status_code=outcome.status, content=outcome.body())
