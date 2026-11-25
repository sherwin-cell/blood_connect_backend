"""Anonymised donor codes (DNR-0001, DNR-0002, ...) for confirmed offers.

Requesters never see who donated: the mobile app shows only
blood_requests/{id}/offers/{offerId}.donorCode. PRC looks a code up in the
admin portal through blood_requests/{id}.matchedDonorCodes.

The admin portal assigns the code in the same transaction that confirms the
offer (requestService.js updateOfferStatus). This module is the safety net
for offers confirmed without one (older admin builds, backfill). Both use
the same counter, counters/donorCodes {last: <int>}, so codes stay unique
and sequential. Codes are per offer, never per donor, so one donor cannot be
linked across requests.
"""

from __future__ import annotations

import logging
from typing import Optional

from firebase_admin import firestore

from .constants import OfferStatus

log = logging.getLogger(__name__)

COUNTERS = "counters"
DONOR_CODES = "donorCodes"
CONFIRMED = (OfferStatus.APPROVED, OfferStatus.LEGACY_ACCEPTED, OfferStatus.DONATED)


def format_donor_code(number: int) -> str:
    return f"DNR-{number:04d}"


def next_donor_number(counter: Optional[dict]) -> int:
    last = (counter or {}).get("last")
    return (int(last) if isinstance(last, (int, float)) else 0) + 1


def _assign(transaction, db, request_id: str, offer_id: str) -> Optional[str]:
    request_ref = db.collection("blood_requests").document(request_id)
    offer_ref = request_ref.collection("offers").document(offer_id)
    counter_ref = db.collection(COUNTERS).document(DONOR_CODES)

    offer = offer_ref.get(transaction=transaction)
    if not offer.exists:
        return None
    data = offer.to_dict() or {}
    if data.get("donorCode"):
        return str(data["donorCode"])
    if str(data.get("status", "")).strip().lower() not in CONFIRMED:
        return None

    counter = counter_ref.get(transaction=transaction)
    number = next_donor_number(counter.to_dict() if counter.exists else None)
    code = format_donor_code(number)
    transaction.set(counter_ref, {"last": number, "updatedAt": firestore.SERVER_TIMESTAMP},
                    merge=True)
    transaction.update(offer_ref, {"donorCode": code})
    transaction.update(request_ref, {"matchedDonorCodes": firestore.ArrayUnion([code])})
    return code


def assign_donor_code(db, request_id: str, offer_id: str) -> Optional[str]:
    """The offer's donor code, assigning the next one if it has none.

    None when the offer is missing or not confirmed by PRC.
    """
    code = firestore.transactional(_assign)(db.transaction(), db, request_id, offer_id)
    if code:
        log.info("Offer %s/%s has donor code %s", request_id, offer_id, code)
    return code
