"""One-off backfill for the admin portal's alignment fixes.

1. Donors: set isAvailableForDonation from donationAvailabilityStatus.
   Older admin builds only wrote donationAvailabilityStatus, but the mobile
   app, donor matching (is_donor_eligible) and firestore.rules
   (isEligibleDonor) read isAvailableForDonation, so donors PRC marked
   "available" still could not offer or be alerted. Only donors that have a
   donationAvailabilityStatus are touched: true for "available", false for
   "on_interval" / "medical_hold".

2. Legacy archived requests: older admin builds overwrote
   blood_requests.status with "archived", which the app and backend do not
   know. The admin portal now keeps the real status and sets archived: true.
   Requests whose request_summaries entry is closed (fulfilled / cancelled /
   expired) get that status back and stay archived. The worker removes duplicates by
   notification id, so nobody is notified again.

   Requests whose summary is still open to donors, or that never had a
   summary, are only listed: their real outcome is unknown, so PRC decides
   from Archived Requests in the admin portal (Restore, then fulfil, reject
   or let it expire).

3. Orphaned request_summaries: summaries whose blood request was deleted
   (older admin builds deleted the request but not its summary) are deleted,
   so donors stop seeing requests they cannot offer on.

Dry run by default; pass --apply to write.

    python scripts/backfill_admin_alignment.py            # show what would change
    python scripts/backfill_admin_alignment.py --apply    # write it
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notifications.constants import RequestStatus  # noqa: E402

LEGACY_ARCHIVED = "archived"

# Summary statuses that tell us how a legacy-archived request really ended.
RESTORABLE_CLOSED = (RequestStatus.FULFILLED, RequestStatus.CANCELLED, RequestStatus.EXPIRED)


def _norm(value) -> str:
    return str(value or "").strip().lower()


def donor_availability_update(donor: dict) -> Optional[dict]:
    """The update a donor needs, or None if it is already consistent."""
    status = _norm(donor.get("donationAvailabilityStatus"))
    if not status:
        return None  # never activated by PRC: leave it alone
    wanted = status == "available"
    if donor.get("isAvailableForDonation") is wanted:
        return None
    return {"isAvailableForDonation": wanted}


def legacy_archive_plan(request: dict, summary: Optional[dict]) -> tuple[str, Optional[dict]]:
    """("restore" | "review" | "skip", request update or None) for one request.

    "restore": convert to the archived flag with the real status.
    "review": legacy archive PRC must resolve; nothing is written.
    "skip": not a legacy archive.
    """
    if _norm(request.get("status")) != LEGACY_ARCHIVED:
        return "skip", None
    summary_status = _norm((summary or {}).get("status"))
    if summary_status in RESTORABLE_CLOSED:
        return "restore", {"status": summary_status, "archived": True}
    return "review", None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--key", default="serviceAccountKey.json",
                        help="service account key (default: serviceAccountKey.json)")
    args = parser.parse_args()

    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    firebase_admin.initialize_app(credentials.Certificate(args.key))
    db = firestore.client()

    donors = 0
    for donor in db.collection("donors").stream():
        update = donor_availability_update(donor.to_dict() or {})
        if update is None:
            continue
        donors += 1
        print(f"donor {donor.id}: isAvailableForDonation = {update['isAvailableForDonation']}")
        if args.apply:
            donor.reference.update({**update, "updatedAt": firestore.SERVER_TIMESTAMP})

    restored, review = 0, []
    legacy = (
        db.collection("blood_requests")
        .where(filter=FieldFilter("status", "==", LEGACY_ARCHIVED))
        .stream()
    )
    for request in legacy:
        summary_snap = db.collection("request_summaries").document(request.id).get()
        summary = summary_snap.to_dict() if summary_snap.exists else None
        action, update = legacy_archive_plan(request.to_dict() or {}, summary)
        if action == "restore":
            restored += 1
            print(f"request {request.id}: status archived -> {update['status']} (stays archived)")
            if args.apply:
                request.reference.update({**update, "updatedAt": firestore.SERVER_TIMESTAMP})
        elif action == "review":
            review.append((request.id, _norm((summary or {}).get("status")) or "no summary"))

    # Older admin builds deleted blood_requests without their summary, which
    # left donors looking at requests nobody can offer on (firestore.rules
    # reads the request to allow an offer).
    orphans = 0
    for summary in db.collection("request_summaries").stream():
        if db.collection("blood_requests").document(summary.id).get().exists:
            continue
        orphans += 1
        data = summary.to_dict() or {}
        print(f"summary {summary.id}: no blood request (status {data.get('status')}, "
              f"{data.get('bloodType')}) -> delete")
        if args.apply:
            summary.reference.delete()

    verb = "Updated" if args.apply else "Would update"
    print(f"{verb} {donors} donor(s) and {restored} legacy archived request(s); "
          f"{'deleted' if args.apply else 'would delete'} {orphans} orphaned summary(ies).")
    if review:
        print(f"\n{len(review)} legacy archived request(s) need PRC review in the admin "
              "portal (Archived Requests -> Restore):")
        for request_id, summary_status in review:
            note = " <- still visible to donors" if summary_status in RequestStatus.OPEN else ""
            print(f"  {request_id}: summary {summary_status}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
