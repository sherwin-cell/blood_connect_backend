"""POST /api/files/{kind} and GET /api/files/view"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from auth import verify_firebase_claims
from verification.config import FaceVerificationSettings

from .refs import KINDS
from .service import FileService, FileSettings
from .storage import PrivateCloudinary

router = APIRouter(prefix="/api/files", tags=["Private files"])

_service: Optional[FileService] = None


def build_private_storage(settings: Optional[FileSettings] = None) -> Optional[PrivateCloudinary]:
    """Private Cloudinary storage, or None without backend Cloudinary credentials."""
    settings = settings or FileSettings.from_env()
    cloud = FaceVerificationSettings.from_env()
    if not cloud.can_upload:
        return None
    return PrivateCloudinary(cloud.cloudinary_cloud_name, cloud.cloudinary_api_key,
                             cloud.cloudinary_api_secret, settings.folder)


def build_service(db) -> FileService:
    settings = FileSettings.from_env()

    def is_admin(uid: str) -> bool:
        # Same as firestore.rules isAdmin(); the custom claim is checked by the service.
        snapshot = db.collection("users").document(uid).get()
        return snapshot.exists and (snapshot.to_dict() or {}).get("role") == "admin"

    return FileService(build_private_storage(settings), is_admin, settings)


def configure(service: Optional[FileService]) -> None:
    global _service
    _service = service


def get_service() -> FileService:
    if _service is None:
        raise HTTPException(status_code=503, detail="Private files are not configured.")
    return _service


@router.post("/{kind}")
def upload_file(
    kind: str,
    file: UploadFile = File(..., description="id_front / id_back: JPEG or PNG. brf: PDF. At most 10 MB."),
    claims: dict = Depends(verify_firebase_claims),
    service: FileService = Depends(get_service),
):
    """Store an ID photo or Blood Request Form privately for the caller.

    Returns {"ref": "secure://..."} to save in Firestore in place of a URL.
    """
    # One byte past the limit is enough to reject an oversized file.
    limit = service.settings.max_bytes(kind) if kind in KINDS else 0
    data = file.file.read(limit + 1)
    outcome = service.upload(claims["uid"], kind, data)
    return JSONResponse(status_code=outcome.status, content=outcome.json())


@router.get("/view")
def view_file(
    ref: str = Query(..., description="A secure:// reference from Firestore."),
    claims: dict = Depends(verify_firebase_claims),
    service: FileService = Depends(get_service),
):
    """A signed URL (valid for a few minutes) for the file's owner or a PRC admin."""
    outcome = service.view(claims, ref)
    return JSONResponse(status_code=outcome.status, content=outcome.json(),
                        headers={"Cache-Control": "no-store"})
