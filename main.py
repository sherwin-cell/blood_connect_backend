
import os
import json
import logging
from contextlib import asynccontextmanager

import firebase_admin

from firebase_admin import credentials, firestore, auth
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

# Import donor notification router
from routers.donor_notifications import router as donor_notifications_router
from notifications.config import Settings
from notifications.worker import NotificationWorker

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("blood_connect")


# ------------------------------------------------------------------------------
# 1. Firebase Admin SDK Initialization
# ------------------------------------------------------------------------------

service_account_env = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

if service_account_env:
    cert_dict = json.loads(service_account_env)
    cred = credentials.Certificate(cert_dict)

elif os.path.exists("serviceAccountKey.json"):
    cred = credentials.Certificate("serviceAccountKey.json")

else:
    cred = None


if cred and not firebase_admin._apps:
    firebase_admin.initialize_app(cred)


db = firestore.client() if cred else None


# ------------------------------------------------------------------------------
# 2. Notification worker (Firestore listeners -> notifications -> FCM)
# ------------------------------------------------------------------------------

notification_worker = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global notification_worker

    settings = Settings.from_env()
    if settings.enabled and db is not None:
        notification_worker = NotificationWorker(db, settings)
        notification_worker.start()
    elif settings.enabled:
        log.warning("Firebase credentials missing; notification worker not started.")

    yield

    if notification_worker is not None:
        notification_worker.stop()


# ------------------------------------------------------------------------------
# 3. FastAPI Setup
# ------------------------------------------------------------------------------

app = FastAPI(
    lifespan=lifespan,
    title="Blood-Connect API",
    description=(
        "Backend API for Blood-Connect. Also runs the notification worker, "
        "which turns Firestore events into in-app notifications and FCM pushes."
    ),
    version="2.2.0"
)

security = HTTPBearer()


# ------------------------------------------------------------------------------
# 4. Register API Routers
# ------------------------------------------------------------------------------

# Deprecated: the notification worker now sends donor approval/rejection
# notifications automatically. Kept so older admin builds do not error.
# POST /api/donors/notify-status
app.include_router(donor_notifications_router)


# ------------------------------------------------------------------------------
# 5. Firebase Authentication Dependency
# ------------------------------------------------------------------------------

def verify_firebase_token(
    credentials_obj: HTTPAuthorizationCredentials = Depends(security)
) -> str:
    """
    Verify a Firebase ID token and return the authenticated user's UID.
    """

    token = credentials_obj.credentials

    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token["uid"]

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Firebase authentication token."
        )


# ------------------------------------------------------------------------------
# 6. Identity Verification Request Schema
# ------------------------------------------------------------------------------

class VerificationRequest(BaseModel):
    user_id: str = Field(..., alias="userId")
    id_image_url: str = Field(..., alias="idImageUrl")
    selfie_image_url: str = Field(..., alias="selfieImageUrl")
    face_match_confidence: float = Field(..., alias="faceMatchConfidence")
    face_match_passed: bool = Field(..., alias="faceMatchPassed")
    verification_status: str = Field(..., alias="status")

    class Config:
        populate_by_name = True


# ------------------------------------------------------------------------------
# 7. Health Check Endpoints
# ------------------------------------------------------------------------------

@app.get("/health", tags=["System"])
def health_check():
    return {
        "status": "ok",
        "service": "Blood-Connect API",
        "notificationWorker": (
            notification_worker.status() if notification_worker else "disabled"
        ),
    }


@app.get("/", tags=["System"])
def root():
    return {
        "message": "Blood-Connect API is running!",
        "version": "2.1.0"
    }


# ------------------------------------------------------------------------------
# 8. Identity Verification Endpoint
# ------------------------------------------------------------------------------

@app.post("/api/verify", tags=["Identity Verification"])
async def verify_identity(
    payload: VerificationRequest,
    authenticated_uid: str = Depends(verify_firebase_token)
):
    """
    Save identity verification data for the authenticated user.

    This preserves the existing identity verification behavior.
    """

    # Ensure the authenticated user matches the requested user.
    if authenticated_uid != payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: User ID mismatch."
        )

    # Persist verification records to Firestore.
    if db:
        is_verified = payload.verification_status == "approved"

        # Update user verification status.
        user_ref = db.collection("users").document(payload.user_id)

        user_ref.set(
            {
                "isVerified": is_verified,
                "verificationStatus": payload.verification_status,
                "updatedAt": firestore.SERVER_TIMESTAMP
            },
            merge=True
        )

        # Save verification audit record.
        audit_ref = db.collection("verifications").document(payload.user_id)

        audit_ref.set(
            {
                "userId": payload.user_id,
                "idImageUrl": payload.id_image_url,
                "selfieImageUrl": payload.selfie_image_url,
                "matchScore": payload.face_match_confidence,
                "faceMatchPassed": payload.face_match_passed,
                "status": payload.verification_status,
                "provider": "local_facenet",
                "timestamp": firestore.SERVER_TIMESTAMP
            }
        )

    return {
        "success": True,
        "verified": payload.verification_status == "approved",
        "status": payload.verification_status,
        "matchScore": payload.face_match_confidence
    }
