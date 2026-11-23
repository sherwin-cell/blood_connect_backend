"""DELETE /api/account and DELETE /api/admin/users/{uid}"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from firebase_admin import auth

from auth import verify_firebase_claims
from verification.config import FaceVerificationSettings
from files.refs import is_secure_ref, parse_ref
from files.router import build_private_storage
from verification.storage import CloudinaryUploader

from .service import DEFAULT_MAX_AUTH_AGE_SECONDS, AccountDeletionService
from .store import FirestoreAccountStore

router = APIRouter(prefix="/api", tags=["Account"])

_service: Optional[AccountDeletionService] = None


def _delete_auth_user(uid: str) -> None:
    try:
        auth.delete_user(uid)
    except auth.UserNotFoundError:
        pass  # already deleted: a retry after a partial failure


def build_service(db) -> AccountDeletionService:
    settings = FaceVerificationSettings.from_env()
    delete_image = None
    if settings.can_upload:
        destroy_public = CloudinaryUploader(
            settings.cloudinary_cloud_name,
            settings.cloudinary_api_key,
            settings.cloudinary_api_secret,
            settings.cloudinary_folder,
            settings.allowed_image_prefix,
        ).destroy
        private = build_private_storage()

        def delete_image(value: str) -> bool:
            """Public URL on our account, or a private secure:// upload."""
            if not is_secure_ref(value):
                return destroy_public(value)
            ref = parse_ref(value, settings.private_folder)
            return private is not None and ref is not None and private.destroy(ref)
    max_age = int(os.getenv("ACCOUNT_DELETE_MAX_AUTH_AGE_SECONDS",
                            str(DEFAULT_MAX_AUTH_AGE_SECONDS)))
    return AccountDeletionService(
        store=FirestoreAccountStore(db, settings.allowed_image_prefix),
        delete_auth_user=_delete_auth_user,
        delete_image=delete_image,
        max_auth_age_seconds=max_age,
    )


def configure(service: Optional[AccountDeletionService]) -> None:
    global _service
    _service = service


def get_service() -> AccountDeletionService:
    if _service is None:
        raise HTTPException(status_code=503, detail="Account deletion is not available.")
    return _service


@router.delete("/account")
def delete_account(
    claims: dict = Depends(verify_firebase_claims),
    service: AccountDeletionService = Depends(get_service),
):
    """Delete the caller's account and personal data. Requires a sign-in
    within the last few minutes (the app re-authenticates first)."""
    outcome = service.delete(claims["uid"], claims.get("auth_time"))
    return JSONResponse(status_code=outcome.status_code, content=outcome.body())


@router.delete("/admin/users/{uid}")
def delete_user_as_admin(
    uid: str,
    claims: dict = Depends(verify_firebase_claims),
    service: AccountDeletionService = Depends(get_service),
):
    """PRC deletes a user's account and personal data from the admin portal,
    with the same cleanup as the app's Delete Account."""
    outcome = service.delete_by_admin(uid, claims)
    return JSONResponse(status_code=outcome.status_code, content=outcome.body())
