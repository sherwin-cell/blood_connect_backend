"""Requester-donor chats, created only when PRC confirms a donor.

The app cannot create these (firestore.rules); it opens
chats/match_{requestId}_{offerId} once this module has created it. Both
sides can derive the id: the requester from their request and its offers,
the donor from their own offer.
"""

from __future__ import annotations

import logging
from typing import Optional

from firebase_admin import firestore
from google.api_core import exceptions as gexc

log = logging.getLogger(__name__)

CHATS = "chats"
TYPE = "donor_match"


def match_chat_id(request_id: str, offer_id: str) -> str:
    return f"match_{request_id}_{offer_id}"


def match_chat_document(request_id: str, offer_id: str, requester_id: str, donor_id: str,
                        requester_name: str, donor_name: str) -> dict:
    return {
        "chatId": match_chat_id(request_id, offer_id),
        "type": TYPE,
        "requestId": request_id,
        "offerId": offer_id,
        # Order matters: firestore.rules compare the list on update.
        "participants": [requester_id, donor_id],
        "participantNames": {requester_id: requester_name, donor_id: donor_name},
        "createdAt": firestore.SERVER_TIMESTAMP,
        "lastMessageTime": firestore.SERVER_TIMESTAMP,
    }


def ensure_match_chat(db, request_id: str, offer_id: str, requester_id: Optional[str],
                      donor_id: Optional[str], requester_name: str, donor_name: str) -> bool:
    """Create the chat if it does not exist yet. True when created."""
    if not requester_id or not donor_id or requester_id == donor_id:
        log.warning("Not creating match chat for %s/%s: missing or same users",
                    request_id, offer_id)
        return False
    ref = db.collection(CHATS).document(match_chat_id(request_id, offer_id))
    try:
        ref.create(match_chat_document(request_id, offer_id, requester_id, donor_id,
                                       requester_name, donor_name))
    except gexc.AlreadyExists:
        return False
    log.info("Opened requester-donor chat for request %s offer %s", request_id, offer_id)
    return True
