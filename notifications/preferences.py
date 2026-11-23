"""Per-user push preferences: users/{uid}.notificationPreferences.

The mobile app's Notification Settings screen writes a map of booleans:

    notificationPreferences: {
        requestUpdates: bool,   # your blood requests and offers on them
        donorAlerts: bool,      # new requests that match your blood type
        donationUpdates: bool,  # your donor application, offers and donations
        chatMessages: bool,     # PRC chat messages
        announcements: bool,    # PRC announcements (FCM topic, see below)
    }

A missing map or key means "on". Preferences only silence the push: the
in-app notification record is still written, so nothing is lost from the
bell list. Account and verification results are always pushed.

Announcements go to an FCM topic, not to a user, so the app unsubscribes
the device from the topic itself; the key is listed here for completeness.
Keep the keys in sync with blood_connect's NotificationPreferences.
"""

from __future__ import annotations

from typing import Optional

from .constants import NotificationType as T

FIELD = "notificationPreferences"

REQUEST_UPDATES = "requestUpdates"
DONOR_ALERTS = "donorAlerts"
DONATION_UPDATES = "donationUpdates"
CHAT_MESSAGES = "chatMessages"
ANNOUNCEMENTS = "announcements"

CATEGORIES = (REQUEST_UPDATES, DONOR_ALERTS, DONATION_UPDATES, CHAT_MESSAGES, ANNOUNCEMENTS)

CATEGORY_BY_TYPE = {
    T.REQUEST_SUBMITTED: REQUEST_UPDATES,
    T.REQUEST_APPROVED: REQUEST_UPDATES,
    T.REQUEST_REJECTED: REQUEST_UPDATES,
    T.REQUEST_FULFILLED: REQUEST_UPDATES,
    T.REQUEST_EXPIRED: REQUEST_UPDATES,
    T.OFFER_RECEIVED: REQUEST_UPDATES,
    T.OFFER_CONFIRMED_FOR_REQUESTER: REQUEST_UPDATES,
    T.REQUEST_OPEN: DONOR_ALERTS,
    T.DONOR_APPLICATION_APPROVED: DONATION_UPDATES,
    T.DONOR_APPLICATION_REJECTED: DONATION_UPDATES,
    T.DONOR_APPOINTMENT: DONATION_UPDATES,
    T.OFFER_APPROVED: DONATION_UPDATES,
    T.OFFER_REJECTED: DONATION_UPDATES,
    T.OFFER_DONATED: DONATION_UPDATES,
    T.REQUEST_CLOSED_FOR_DONOR: DONATION_UPDATES,
    T.CHAT_MESSAGE: CHAT_MESSAGES,
    T.ANNOUNCEMENT: ANNOUNCEMENTS,
    # ID_VERIFICATION_* are not listed: account results are always pushed.
}


def category_for(notification_type: Optional[str]) -> Optional[str]:
    return CATEGORY_BY_TYPE.get(str(notification_type or ""))


def push_allowed(notification_type: Optional[str], user: Optional[dict]) -> bool:
    """False only when the user switched off this type's category."""
    category = category_for(notification_type)
    if category is None:
        return True
    prefs = (user or {}).get(FIELD)
    if not isinstance(prefs, dict):
        return True
    return prefs.get(category) is not False
