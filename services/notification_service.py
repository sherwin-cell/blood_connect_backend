
from firebase_admin import messaging
from firebase_config import initialize_firebase


def send_donor_status_notification(
    user_id: str,
    donor_id: str,
    status: str
):
    """
    Send a donor application notification through FCM.

    The caller must perform authorization and
    duplicate-event checks before calling this function.
    """

    if status not in ("approved", "rejected"):
        raise ValueError("Invalid donor application status.")

    db = initialize_firebase()

    token_docs = list(
        db.collection("users")
        .document(user_id)
        .collection("fcm_tokens")
        .stream()
    )

    # Deduplicate tokens.
    tokens = list({
        data.get("token")
        for snapshot in token_docs
        if (data := snapshot.to_dict() or {}).get("token")
    })

    if not tokens:
        return {
            "success": False,
            "sent": 0,
            "failed": 0,
            "message": "No registered device tokens found."
        }

    if status == "approved":
        title = "Donor Application Approved"
        body = (
            "Your donor application has been approved by PRC. "
            "Please wait for your appointment and further screening."
        )
    else:
        title = "Donor Application Rejected"
        body = (
            "Your donor application was rejected by PRC. "
            "Open Blood-Connect to review your application status."
        )

    sent = 0
    failed = 0

    for token in tokens:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body
            ),
            data={
                "type": "donor_application_status",
                "donorId": donor_id,
                "status": status
            },
            token=token
        )

        try:
            messaging.send(message)
            sent += 1
        except Exception:
            failed += 1

    return {
        "success": failed == 0 and sent > 0,
        "sent": sent,
        "failed": failed
    }
