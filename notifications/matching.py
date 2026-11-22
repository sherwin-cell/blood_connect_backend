"""Pick which donors to alert about a newly opened blood request.

Pure functions only, so the rules can be unit tested. Matching follows the
mobile app's rules:

- Blood type: exact match (BloodCompatibility in the app). A request for
  "Any Type" matches every blood type.
- Eligibility: approved donor, isDonor, PRC marked isAvailableForDonation,
  and today is on or after nextEligibleDonationDate (YYYY-MM-DD, PH time).
- Location: the donor's profile (users/{uid}) must be in the requester's
  municipality; when the requester has no municipality, their province.
  Requests from users with no location are not broadcast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from .constants import DonorStatus

ANY_BLOOD_TYPE = {"ANY", "ANY TYPE", "ANY BLOOD TYPE"}
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass(frozen=True)
class Location:
    municipality: str = ""
    province: str = ""

    @staticmethod
    def from_profile(profile: Optional[dict]) -> "Location":
        profile = profile or {}
        return Location(
            municipality=_clean(profile.get("municipality") or profile.get("city")),
            province=_clean(profile.get("province")),
        )

    @property
    def is_known(self) -> bool:
        return bool(self.municipality or self.province)


@dataclass(frozen=True)
class DonorCandidate:
    donor_id: str
    user_id: str
    donor: dict
    location: Location


def _clean(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def normalize_blood_type(value) -> str:
    return " ".join(str(value or "").split()).upper()


def blood_type_matches(request_type, donor_type) -> bool:
    request = normalize_blood_type(request_type)
    donor = normalize_blood_type(donor_type)
    if not request or not donor:
        return False
    return request in ANY_BLOOD_TYPE or request == donor


def next_eligible_date(donor: dict) -> Optional[date]:
    raw = donor.get("nextEligibleDonationDate")
    if not isinstance(raw, str):
        return None
    match = _DATE.match(raw.strip())
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def is_donor_eligible(donor: dict, today: date) -> bool:
    if str(donor.get("donorStatus") or "").strip().lower() != DonorStatus.APPROVED:
        return False
    if donor.get("isDonor") is not True:
        return False
    if donor.get("isAvailableForDonation") is not True:
        return False
    next_date = next_eligible_date(donor)
    return next_date is None or today >= next_date


def location_matches(request: Location, donor: Location) -> bool:
    if request.municipality:
        if donor.municipality != request.municipality:
            return False
        # Same municipality name in a different province is a different place.
        return not (request.province and donor.province and donor.province != request.province)
    if request.province:
        return donor.province == request.province
    return False


def select_donors(
    *,
    request: dict,
    requester_id: str,
    requester_location: Location,
    candidates: Iterable[DonorCandidate],
    today: date,
    limit: int,
) -> list[DonorCandidate]:
    """Donors to alert about `request`, at most `limit` of them."""
    if not requester_location.is_known:
        return []

    selected = []
    seen_users = set()
    for candidate in candidates:
        if not candidate.user_id or candidate.user_id == requester_id:
            continue
        if candidate.user_id in seen_users:
            continue
        if not blood_type_matches(request.get("bloodType"), candidate.donor.get("bloodType")):
            continue
        if not is_donor_eligible(candidate.donor, today):
            continue
        if not location_matches(requester_location, candidate.location):
            continue
        selected.append(candidate)
        seen_users.add(candidate.user_id)
        if len(selected) >= limit:
            break
    return selected
