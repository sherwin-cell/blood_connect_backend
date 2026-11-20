
from typing import Literal
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import auth, firestore
from google.cloud.firestore_v1 import transactional
from pydantic import BaseModel, Field

from firebase_config import initialize_firebase
from services.notification_service import send_donor_status_notification


router = APIRouter(
    prefix="/api/donors",
    tags=["Donor Notifications"]
)

security = HTTPBearer()


# ---------------------------------------------------------
# Request schema
# ---------------------------------------------------------

class DonorNotificationRequest(BaseModel):
    donor_id: str = Field(..., alias="donorId", min_length=1)
    status: Literal["approved", "rejected"]
    review_event_id: str = Field(
        ...,
        alias="reviewEventId",
        min_length=1
    )


# ---------------------------------------------------------
# Verify PRC administrator
# ---------------------------------------------------------

def require_admin(
    bearer: HTTPAuthorizationCredentials = Depends(security)
):
    try:
        decoded = auth.verify_id_token(bearer.credentials)
        uid = decoded["uid"]
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired authentication token."
        )

    db = initialize_firebase()

    admin_snapshot = (
        db.collection("users")
        .document(uid)
        .get()
    )

    if not admin_snapshot.exists:
        raise HTTPException(
            status_code=403,
            detail="Administrator access required."
        )

    admin_data = admin_snapshot.to_dict() or {}

    if admin_data.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Administrator access required."
        )

    return uid


# ---------------------------------------------------------
# Atomically claim a notification event
# ---------------------------------------------------------

@transactional
def claim_notification_event(
    transaction,
    donor_ref,
    event_ref,
    requested_status,
    requested_event_id,
    admin_uid
):
    donor_snapshot = donor_ref.get(transaction=transaction)

    if not donor_snapshot.exists:
        raise HTTPException(
            status_code=404,
            detail="Donor application not found."
        )

    donor = donor_snapshot.to_dict() or {}

    saved_status = str(
        donor.get("applicationStatus")
        or donor.get("donorStatus")
        or donor.get("status")
        or ""
    ).lower()

    if saved_status != requested_status:
        raise HTTPException(
            status_code=409,
            detail="Donor application status does not match."
        )

    if donor.get("reviewEventId") != requested_event_id:
        raise HTTPException(
            status_code=409,
            detail="Review event does not match the latest decision."
        )

    if donor.get("reviewedBy") != admin_uid:
        raise HTTPException(
            status_code=403,
            detail="Only the reviewing administrator can send this event."
        )

    applicant_uid = donor.get("userId")

    if not applicant_uid:
        raise HTTPException(
            status_code=409,
            detail="Donor application has no userId."
        )

    # Read the event before writing inside the transaction.
    event_snapshot = event_ref.get(transaction=transaction)

    if event_snapshot.exists:
        return {
            "claimed": False,
            "userId": applicant_uid,
            "state": (event_snapshot.to_dict() or {}).get("state")
        }

    transaction.create(
        event_ref,
        {
            "reviewEventId": requested_event_id,
            "status": requested_status,
            "userId": applicant_uid,
            "reviewedBy": admin_uid,
            "state": "processing",
            "createdAt": firestore.SERVER_TIMESTAMP
        }
    )

    return {
        "claimed": True,
        "userId": applicant_uid,
        "state": "processing"
    }


# ---------------------------------------------------------
# Donor notification endpoint
# ---------------------------------------------------------

@router.post("/notify-status")
def notify_donor_status(
    payload: DonorNotificationRequest,
    admin_uid: str = Depends(require_admin)
):
    db = initialize_firebase()

    donor_ref = (
        db.collection("donors")
        .document(payload.donor_id)
    )

    event_ref = (
        donor_ref.collection("notification_events")
        .document(payload.review_event_id)
    )

    transaction = db.transaction()

    claim = claim_notification_event(
        transaction,
        donor_ref,
        event_ref,
        payload.status,
        payload.review_event_id,
        admin_uid
    )

    if not claim["claimed"]:
        return {
            "success": True,
            "alreadyProcessed": True,
            "eventState": claim["state"],
            "message": "This notification event was already claimed."
        }

    applicant_uid = claim["userId"]

    try:
        applicant = (
            db.collection("users")
            .document(applicant_uid)
            .get()
        )

        if not applicant.exists:
            event_ref.update({
                "state": "failed",
                "error": "Applicant account not found.",
                "updatedAt": firestore.SERVER_TIMESTAMP
            })

            raise HTTPException(
                status_code=404,
                detail="Applicant account not found."
            )

        result = send_donor_status_notification(
            user_id=applicant_uid,
            donor_id=payload.donor_id,
            status=payload.status
        )

        # FCM success means accepted for delivery,
        # not guaranteed displayed on the device.
        event_state = (
            "sent"
            if result["sent"] > 0 and result["failed"] == 0
            else "partial"
            if result["sent"] > 0
            else "failed"
        )

        event_ref.update({
            "state": event_state,
            "sentCount": result["sent"],
            "failedCount": result["failed"],
            "updatedAt": firestore.SERVER_TIMESTAMP
        })

        return {
            "success": event_state == "sent",
            "alreadyProcessed": False,
            "reviewEventId": payload.review_event_id,
            "eventState": event_state,
            "notification": result
        }

    except HTTPException:
        raise

    except Exception:
        # The event remains claimed, preventing an accidental
        # duplicate send if the FCM outcome is uncertain.
        event_ref.update({
            "state": "unknown",
            "updatedAt": firestore.SERVER_TIMESTAMP
        })

        raise HTTPException(
            status_code=502,
            detail="Notification processing failed."
        )
