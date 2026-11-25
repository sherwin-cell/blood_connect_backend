"""Unit tests for scripts/strip_offer_donor_identity.py."""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from strip_offer_donor_identity import IDENTITY_FIELDS, identity_fields_present  # noqa: E402

RULES = os.path.join(os.path.dirname(ROOT), "blood_connect", "firestore.rules")


class IdentityFieldsTest(unittest.TestCase):
    def test_finds_name_and_contact_only(self):
        offer = {
            "donorId": "uid-1",
            "donorBloodType": "A+",
            "donorCode": "DNR-0001",
            "status": "approved",
            "donorName": "Maria Santos",
            "donorContact": "09171234567",
        }
        self.assertEqual(identity_fields_present(offer), ["donorName", "donorContact"])

    def test_clean_offer_needs_nothing(self):
        self.assertEqual(
            identity_fields_present({"donorId": "uid-1", "donorCode": "DNR-0002"}), []
        )
        self.assertEqual(identity_fields_present({}), [])
        self.assertEqual(identity_fields_present(None), [])

    @unittest.skipUnless(os.path.exists(RULES), "blood_connect repo not alongside")
    def test_matches_firestore_rules_list(self):
        with open(RULES, encoding="utf-8") as handle:
            rules = handle.read()
        body = re.search(r"function offerIdentityFields\(\)\s*\{\s*return \[(.*?)\];", rules, re.S)
        self.assertIsNotNone(body, "offerIdentityFields() not found in firestore.rules")
        self.assertEqual(tuple(re.findall(r"'([A-Za-z]+)'", body.group(1))), IDENTITY_FIELDS)


if __name__ == "__main__":
    unittest.main()
