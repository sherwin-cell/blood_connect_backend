"""Remove donor names and contact details from existing offers.

Requesters can read the offers on their own blood request
(blood_requests/{id}/offers/{offerId}), and donors stay anonymous to
requesters: they see only the offer's DNR code. Older app builds stored the
donor's name and mobile number on each offer, so this deletes those fields.
PRC still sees the donor's details in the admin portal, which now reads them
from donors/{donorId}. firestore.rules (offerIdentityFields) stops new
offers from carrying them.

Dry run by default; pass --apply to write.

    python scripts/strip_offer_donor_identity.py            # show what would change
    python scripts/strip_offer_donor_identity.py --apply    # write it
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep in sync with offerIdentityFields() in blood_connect/firestore.rules.
IDENTITY_FIELDS = (
    "donorName",
    "donorFullName",
    "donorContact",
    "donorPhone",
    "donorMobile",
    "donorEmail",
    "donorPhotoUrl",
)


def identity_fields_present(offer: dict) -> list[str]:
    """The donor identity fields stored on this offer, in a stable order."""
    return [field for field in IDENTITY_FIELDS if field in (offer or {})]


def main() -> int:
    import firebase_admin
    from firebase_admin import credentials, firestore

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--key", default="serviceAccountKey.json",
                        help="service account key (default: serviceAccountKey.json)")
    args = parser.parse_args()

    firebase_admin.initialize_app(credentials.Certificate(args.key))
    db = firestore.client()

    affected = 0
    for offer in db.collection_group("offers").stream():
        fields = identity_fields_present(offer.to_dict() or {})
        if not fields:
            continue
        affected += 1
        request_id = offer.reference.parent.parent.id
        # Field names only: never print the donor's details.
        print(f"offer {request_id}/{offer.id}: remove {', '.join(fields)}")
        if args.apply:
            offer.reference.update({field: firestore.DELETE_FIELD for field in fields})

    verb = "Cleaned" if args.apply else "Would clean"
    print(f"{verb} {affected} offer(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
