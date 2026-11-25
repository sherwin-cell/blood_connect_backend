"""One-off backfill for the Matching Center changes.

1. request_summaries without hospitalName get it from their blood request
   (summaries published before the admin portal started copying it).
2. Offers PRC already confirmed get an anonymised donor code (DNR-0001...),
   also listed on the request's matchedDonorCodes so PRC can look it up.
   New confirmations get one from the admin portal.

Requester-donor chats are no longer created: requesters see only the donor
code and PRC coordinates every donation.

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

from notifications.donor_code import CONFIRMED, assign_donor_code  # noqa: E402


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

    # Oldest confirmations first, so codes follow the order PRC confirmed.
    pending = []
    for offer in db.collection_group("offers").stream():
        data = offer.to_dict() or {}
        if data.get("donorCode") or str(data.get("status", "")).lower() not in CONFIRMED:
            continue
        stamp = data.get("approvedAt") or data.get("updatedAt") or data.get("createdAt")
        pending.append((stamp.timestamp() if hasattr(stamp, "timestamp") else 0.0, offer))
    pending.sort(key=lambda item: item[0])

    for _, offer in pending:
        request_id = offer.reference.parent.parent.id
        if args.apply:
            code = assign_donor_code(db, request_id, offer.id)
            print(f"offer {request_id}/{offer.id}: donorCode = {code}")
        else:
            print(f"offer {request_id}/{offer.id}: needs a donor code")

    verb = "Updated" if args.apply else "Would update"
    print(f"{verb} {summaries} summary(ies) and {len(pending)} confirmed offer(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
