import os
import json
import firebase_admin
from firebase_admin import credentials, firestore, auth
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

# ------------------------------------------------------------------------------
# 1. Firebase Admin SDK Initialization
# ------------------------------------------------------------------------------
service_account_env = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

if service_account_env:
    cert_dict = json.loads(service_account_env)
    cred = credentials.Certificate(cert_dict)
else:
    if os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
    else:
        cred = None

if cred and not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client() if cred else None

# ------------------------------------------------------------------------------
# 2. FastAPI Setup & Authentication Dependency
# ------------------------------------------------------------------------------
app = FastAPI(
    title="Blood-Connect Identity Verification API",
    version="2.0.0"
)
security = HTTPBearer()

def verify_firebase_token(credentials_obj: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Verifies the incoming Bearer Firebase ID token and returns the UID."""
    token = credentials_obj.credentials
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token["uid"]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired Firebase token: {str(e)}"
        )

# ------------------------------------------------------------------------------
# 3. Request Schemas (Updated for Local FaceNet Architecture)
# ------------------------------------------------------------------------------
class VerificationRequest(BaseModel):
    user_id: str = Field(..., alias="userId")
    id_image_url: str = Field(..., alias="idImageUrl")
    selfie_image_url: str = Field(..., alias="selfieImageUrl")
    face_match_confidence: float = Field(..., alias="faceMatchConfidence")
    face_match_passed: bool = Field(..., alias="faceMatchPassed")
    verification_status: str = Field(..., alias="status")  # 'approved' or 'under_review'

    class Config:
        populate_by_name = True

# ------------------------------------------------------------------------------
# 4. Endpoints
# ------------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "ok", "service": "Blood-Connect API"}

@app.get("/")
def root():
    return {"message": "Blood-Connect API is running!"}

@app.post("/api/verify")
async def verify_identity(
    payload: VerificationRequest,
    authenticated_uid: str = Depends(verify_firebase_token)
):
    # Security Check: Ensure authenticated user matches requested user ID
    if authenticated_uid != payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: User ID mismatch."
        )

    # Persist verification records to Firestore
    if db:
        is_verified = payload.verification_status == "approved"

        # Update User Status
        user_ref = db.collection("users").document(payload.user_id)
        user_ref.set({
            "isVerified": is_verified,
            "verificationStatus": payload.verification_status,
            "updatedAt": firestore.SERVER_TIMESTAMP
        }, merge=True)

        # Record Audit Log
        audit_ref = db.collection("verifications").document(payload.user_id)
        audit_ref.set({
            "userId": payload.user_id,
            "idImageUrl": payload.id_image_url,
            "selfieImageUrl": payload.selfie_image_url,
            "matchScore": payload.face_match_confidence,
            "faceMatchPassed": payload.face_match_passed,
            "status": payload.verification_status,
            "provider": "local_facenet",
            "timestamp": firestore.SERVER_TIMESTAMP
        })

    return {
        "success": True,
        "verified": payload.verification_status == "approved",
        "status": payload.verification_status,
        "matchScore": payload.face_match_confidence
    }