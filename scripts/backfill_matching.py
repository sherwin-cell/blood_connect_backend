"""One-off backfill for the Matching Center changes.

1. request_summaries without hospitalName get it from their blood request
   (summaries published before the admin portal started copying it).
2. Offers PRC already confirmed get their requester-donor chat
   (chats/match_{requestId}_{offerId}); new confirmations get one from the
   notification worker.

Dry run by default; pass --apply to write.

    python scripts/backfill_matching.py            # show what would change
    python scripts/backfill_matching.py --apply    # write it
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin  # noqa: E402
from firebase_admin import credentials, firestore  # noqa: E402

from notifications.constants import OfferStatus  # noqa: E402
from notifications.match_chat import ensure_match_chat, match_chat_id  # noqa: E402

CONFIRMED = (OfferStatus.APPROVED, OfferStatus.LEGACY_ACCEPTED, OfferStatus.DONATED)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--key", default="serviceAccountKey.json",
                        help="service account key (default: serviceAccountKey.json)")
    args = parser.parse_args()

    firebase_admin.initialize_app(credentials.Certificate(args.key))
    db = firestore.client()

    summaries = 0
    for summary in db.collection("request_summaries").stream():
        if (summary.to_dict() or {}).get("hospitalName"):
            continue
        request = db.collection("blood_requests").document(summary.id).get()
        hospital = (request.to_dict() or {}).get("hospitalName") if request.exists else None
        if not hospital:
            continue
        summaries += 1
        print(f"summary {summary.id}: hospitalName = {hospital!r}")
        if args.apply:
            summary.reference.update({"hospitalName": hospital})

    chats = 0
    for offer in db.collection_group("offers").stream():
        data = offer.to_dict() or {}
        if str(data.get("status", "")).lower() not in CONFIRMED:
            continue
        request_ref = offer.reference.parent.parent
        chat_id = match_chat_id(request_ref.id, offer.id)
        if db.collection("chats").document(chat_id).get().exists:
            continue
        request = request_ref.get()
        request_data = (request.to_dict() or {}) if request.exists else {}
        requester_id = request_data.get("requesterId") or request_data.get("userId")
        profile = db.collection("users").document(requester_id).get() if requester_id else None
        requester_name = ((profile.to_dict() or {}).get("fullName")
                          if profile is not None and profile.exists else None) or "Requester"
        chats += 1
        print(f"chat {chat_id}: {requester_id} <-> {data.get('donorId')}")
        if args.apply:
            ensure_match_chat(db, request_ref.id, offer.id, requester_id, data.get("donorId"),
                              requester_name, str(data.get("donorName") or "Donor"))

    verb = "Updated" if args.apply else "Would update"
    print(f"{verb} {summaries} summary(ies) and {'created' if args.apply else 'create'} "
          f"{chats} chat(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
