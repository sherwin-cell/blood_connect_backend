"""Unit tests for scripts/backfill_admin_alignment.py planning logic."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from backfill_admin_alignment import donor_availability_update, legacy_archive_plan  # noqa: E402


class DonorAvailabilityTest(unittest.TestCase):
    def test_available_without_flag_becomes_true(self):
        self.assertEqual(
            donor_availability_update({"donationAvailabilityStatus": "available"}),
            {"isAvailableForDonation": True},
        )

    def test_interval_and_hold_become_false(self):
        for status in ("on_interval", "medical_hold"):
            self.assertEqual(
                donor_availability_update(
                    {"donationAvailabilityStatus": status, "isAvailableForDonation": True}
                ),
                {"isAvailableForDonation": False},
            )

    def test_consistent_donor_is_left_alone(self):
        self.assertIsNone(donor_availability_update(
            {"donationAvailabilityStatus": "available", "isAvailableForDonation": True}
        ))
        self.assertIsNone(donor_availability_update(
            {"donationAvailabilityStatus": "on_interval", "isAvailableForDonation": False}
        ))

    def test_never_activated_donor_is_left_alone(self):
        self.assertIsNone(donor_availability_update({"donorStatus": "approved"}))


class LegacyArchiveTest(unittest.TestCase):
    def test_closed_summary_restores_real_status_and_stays_archived(self):
        for status in ("fulfilled", "cancelled", "expired"):
            self.assertEqual(
                legacy_archive_plan({"status": "archived"}, {"status": status}),
                ("restore", {"status": status, "archived": True}),
            )

    def test_open_summary_needs_review(self):
        self.assertEqual(
            legacy_archive_plan({"status": "archived"}, {"status": "active"}), ("review", None)
        )

    def test_no_summary_needs_review(self):
        self.assertEqual(legacy_archive_plan({"status": "archived"}, None), ("review", None))

    def test_current_requests_are_skipped(self):
        self.assertEqual(
            legacy_archive_plan({"status": "fulfilled", "archived": True}, None), ("skip", None)
        )


if __name__ == "__main__":
    unittest.main()
