"""Status values, notification types and routes.

Status values must match blood_connect/lib/core/constants/statuses.dart and
blood-connect-admin/src/constants/statuses.js.
"""

from datetime import timedelta, timezone

# Philippines time (UTC+8, no daylight saving). PRC stores
# nextEligibleDonationDate as a YYYY-MM-DD date in this timezone.
PH_TZ = timezone(timedelta(hours=8))


class RequestStatus:
    PENDING = "pending"
    APPROVED = "approved"
    ACTIVE = "active"  # legacy alias of approved; what the admin app writes
    MATCHED = "matched"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    OPEN = (APPROVED, ACTIVE)


class OfferStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    DONATED = "donated"
    LEGACY_ACCEPTED = "accepted"

    ACTIVE = (PENDING, APPROVED, LEGACY_ACCEPTED)


class DonorStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class IdVerificationStatus:
    APPROVED = "approved"
    REJECTED = "rejected"


# Placeholder participant used by PRC support chats; not a real user.
PRC_SUPPORT_ID = "prc_support_admin"

# FCM topic every app install subscribes to.
ANNOUNCEMENTS_TOPIC = "announcements"

# Android channel created by the mobile app (PushNotificationService).
ANDROID_CHANNEL_ID = "blood_connect_notifications"


class NotificationType:
    """Values stored in notifications/{id}.type."""

    DONOR_APPLICATION_APPROVED = "donor_application_approved"
    DONOR_APPLICATION_REJECTED = "donor_application_rejected"
    DONOR_APPOINTMENT = "donor_appointment"
    ID_VERIFICATION_APPROVED = "id_verification_approved"
    ID_VERIFICATION_REJECTED = "id_verification_rejected"

    REQUEST_SUBMITTED = "request_submitted"
    REQUEST_APPROVED = "request_approved"
    REQUEST_REJECTED = "request_rejected"
    REQUEST_FULFILLED = "request_fulfilled"
    REQUEST_EXPIRED = "request_expired"  # set by jobs/request_expiration.py
    REQUEST_OPEN = "request_open"  # to matching donors
    REQUEST_CLOSED_FOR_DONOR = "request_closed_for_donor"
    # PRC reserved units from its own blood bank (POST /api/admin/requests/{id}/prc-stock).
    PRC_STOCK_AVAILABLE = "prc_stock_available"

    OFFER_RECEIVED = "offer_received"  # to requester
    OFFER_CONFIRMED_FOR_REQUESTER = "offer_confirmed_for_requester"
    OFFER_APPROVED = "offer_approved"  # to donor
    OFFER_REJECTED = "offer_rejected"
    OFFER_DONATED = "offer_donated"

    CHAT_MESSAGE = "chat_message"
    ANNOUNCEMENT = "announcement"


class Route:
    """Where the mobile app opens a tapped notification (notifications/{id}.route)."""

    ACTIVITY = "activity"  # Activity tab: own requests and donor application
    REQUEST = "request"  # donor-facing request details (needs requestId)
    REQUEST_OFFERS = "request_offers"  # requester's offers list (needs requestId)
    MY_OFFERS = "my_offers"  # donor's "My donation offers"
    CHAT = "chat"  # chat room (needs chatId)
    HOME = "home"
