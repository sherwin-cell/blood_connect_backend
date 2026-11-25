"""Unit tests for donor matching and notification ids."""

import unittest
from datetime import date

from notifications.matching import (
    DonorCandidate,
    Location,
    blood_type_matches,
    is_donor_eligible,
    location_matches,
    select_donors,
)
from notifications.outbox import notification_id

TODAY = date(2026, 11, 23)

ELIGIBLE = {
    "donorStatus": "approved",
    "isDonor": True,
    "isAvailableForDonation": True,
    "bloodType": "O+",
    # Activated donors always carry the date PRC set at activation.
    "nextEligibleDonationDate": "2026-01-15",
}


def candidate(uid, municipality="sorsogon city", province="sorsogon", **overrides):
    return DonorCandidate(
        donor_id=uid,
        user_id=uid,
        donor={**ELIGIBLE, **overrides},
        location=Location(municipality=municipality, province=province),
    )


class BloodType(unittest.TestCase):
    def test_exact_match_only(self):
        self.assertTrue(blood_type_matches("O+", "o+"))
        self.assertFalse(blood_type_matches("O+", "O-"))
        self.assertFalse(blood_type_matches("A+", "O-"))

    def test_any_type_is_not_a_wildcard(self):
        self.assertFalse(blood_type_matches("Any Type", "AB-"))
        self.assertFalse(blood_type_matches("Any", "O+"))

    def test_missing_type_never_matches(self):
        self.assertFalse(blood_type_matches("", "O+"))
        self.assertFalse(blood_type_matches("O+", None))


class Eligibility(unittest.TestCase):
    def test_eligible(self):
        self.assertTrue(is_donor_eligible(ELIGIBLE, TODAY))

    def test_must_be_approved_donor_and_available(self):
        self.assertFalse(is_donor_eligible({**ELIGIBLE, "donorStatus": "pending"}, TODAY))
        self.assertFalse(is_donor_eligible({**ELIGIBLE, "isDonor": False}, TODAY))
        self.assertFalse(is_donor_eligible({**ELIGIBLE, "isAvailableForDonation": False}, TODAY))

    def test_respects_next_eligible_date(self):
        self.assertFalse(
            is_donor_eligible({**ELIGIBLE, "nextEligibleDonationDate": "2026-12-01"}, TODAY)
        )
        self.assertTrue(
            is_donor_eligible({**ELIGIBLE, "nextEligibleDonationDate": "2026-11-23"}, TODAY)
        )

    def test_fails_closed_without_a_valid_next_eligible_date(self):
        missing = {k: v for k, v in ELIGIBLE.items() if k != "nextEligibleDonationDate"}
        cases = {
            "missing": missing,
            "null": {**ELIGIBLE, "nextEligibleDonationDate": None},
            "empty": {**ELIGIBLE, "nextEligibleDonationDate": ""},
            "slashes": {**ELIGIBLE, "nextEligibleDonationDate": "12/01/2026"},
            "single-digit month": {**ELIGIBLE, "nextEligibleDonationDate": "2026-1-15"},
            "leading space": {**ELIGIBLE, "nextEligibleDonationDate": " 2026-01-15"},
            "trailing newline": {**ELIGIBLE, "nextEligibleDonationDate": "2026-01-15\n"},
            "month 13": {**ELIGIBLE, "nextEligibleDonationDate": "2026-13-01"},
            "impossible Feb 31": {**ELIGIBLE, "nextEligibleDonationDate": "2026-02-31"},
            "number": {**ELIGIBLE, "nextEligibleDonationDate": 20260115},
            "date object": {**ELIGIBLE, "nextEligibleDonationDate": date(2026, 1, 15)},
        }
        for label, donor in cases.items():
            with self.subTest(label):
                self.assertFalse(is_donor_eligible(donor, TODAY))

    def test_missing_date_donor_is_not_alerted(self):
        missing = {k: v for k, v in ELIGIBLE.items() if k != "nextEligibleDonationDate"}
        candidates = [
            candidate("dated"),
            DonorCandidate(
                donor_id="undated",
                user_id="undated",
                donor=missing,
                location=Location(municipality="sorsogon city", province="sorsogon"),
            ),
        ]
        selected = select_donors(
            request={"bloodType": "O+"},
            requester_id="requester",
            requester_location=Location("sorsogon city", "sorsogon"),
            candidates=candidates,
            today=TODAY,
            limit=10,
        )
        self.assertEqual([c.donor_id for c in selected], ["dated"])


class Locations(unittest.TestCase):
    def test_from_profile_normalizes(self):
        location = Location.from_profile({"municipality": "  Sorsogon  City ", "province": "SORSOGON"})
        self.assertEqual(location, Location("sorsogon city", "sorsogon"))

    def test_same_municipality(self):
        request = Location("bulan", "sorsogon")
        self.assertTrue(location_matches(request, Location("bulan", "sorsogon")))
        self.assertTrue(location_matches(request, Location("bulan", "")))
        self.assertFalse(location_matches(request, Location("gubat", "sorsogon")))
        self.assertFalse(location_matches(request, Location("bulan", "masbate")))

    def test_province_fallback(self):
        request = Location("", "sorsogon")
        self.assertTrue(location_matches(request, Location("gubat", "sorsogon")))
        self.assertFalse(location_matches(request, Location("gubat", "albay")))

    def test_unknown_request_location_matches_nobody(self):
        self.assertFalse(location_matches(Location(), Location("bulan", "sorsogon")))


class SelectDonors(unittest.TestCase):
    def select(self, candidates, request_location=Location("sorsogon city", "sorsogon"), limit=10):
        return [
            c.user_id
            for c in select_donors(
                request={"bloodType": "O+"},
                requester_id="requester",
                requester_location=request_location,
                candidates=candidates,
                today=TODAY,
                limit=limit,
            )
        ]

    def test_filters_everything(self):
        chosen = self.select([
            candidate("match"),
            candidate("wrong-type", bloodType="A+"),
            candidate("unavailable", isAvailableForDonation=False),
            candidate("far-away", municipality="bulan"),
            candidate("requester"),
            candidate("match"),  # duplicate user
        ])
        self.assertEqual(chosen, ["match"])

    def test_limit(self):
        self.assertEqual(len(self.select([candidate(f"u{i}") for i in range(5)], limit=3)), 3)

    def test_requester_without_location_alerts_nobody(self):
        self.assertEqual(self.select([candidate("match")], request_location=Location()), [])


class NotificationIds(unittest.TestCase):
    def test_deterministic_and_safe(self):
        first = notification_id("chat_message", "abc/def", "uid 1")
        self.assertEqual(first, notification_id("chat_message", "abc/def", "uid 1"))
        self.assertNotIn("/", first)
        self.assertNotIn(" ", first)

    def test_differs_by_recipient(self):
        self.assertNotEqual(
            notification_id("request_open", "r1", "a"), notification_id("request_open", "r1", "b")
        )

    def test_never_reserved(self):
        self.assertFalse(notification_id("__x", "y", "z").startswith("__"))


if __name__ == "__main__":
    unittest.main()
