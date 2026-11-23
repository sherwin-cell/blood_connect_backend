import unittest
from unittest import mock

from google.api_core import exceptions as gexc

from notifications.match_chat import ensure_match_chat, match_chat_document, match_chat_id


class MatchChatTest(unittest.TestCase):
    def test_id_and_document(self):
        self.assertEqual(match_chat_id("r1", "o1"), "match_r1_o1")
        doc = match_chat_document("r1", "o1", "req", "don", "Ana", "Ben")
        self.assertEqual(doc["participants"], ["req", "don"])  # requester first
        self.assertEqual(doc["type"], "donor_match")
        self.assertEqual(doc["participantNames"], {"req": "Ana", "don": "Ben"})
        self.assertNotIn("isPrcSupport", doc)

    def test_creates_once(self):
        db = mock.MagicMock()
        ref = db.collection.return_value.document.return_value
        self.assertTrue(ensure_match_chat(db, "r1", "o1", "req", "don", "Ana", "Ben"))
        db.collection.return_value.document.assert_called_with("match_r1_o1")
        ref.create.side_effect = gexc.AlreadyExists("exists")
        self.assertFalse(ensure_match_chat(db, "r1", "o1", "req", "don", "Ana", "Ben"))

    def test_refuses_missing_or_same_users(self):
        db = mock.MagicMock()
        self.assertFalse(ensure_match_chat(db, "r", "o", None, "don", "A", "B"))
        self.assertFalse(ensure_match_chat(db, "r", "o", "u", "u", "A", "B"))
        db.collection.assert_not_called()


class WorkerHooksTest(unittest.TestCase):
    def _worker(self):
        from notifications.worker import NotificationWorker

        worker = NotificationWorker.__new__(NotificationWorker)
        worker._db = mock.MagicMock()
        worker._notify = mock.Mock()
        return worker

    def test_offer_approved_opens_the_chat(self):
        from notifications.events import Intent, Kind

        worker = self._worker()
        worker._offer_requester_id = mock.Mock(return_value="req")
        profile = mock.Mock(exists=True)
        profile.to_dict.return_value = {"fullName": "Ana Cruz"}
        worker._db.collection.return_value.document.return_value.get.return_value = profile
        intent = Intent(Kind.OFFER_APPROVED, "k", None,
                        data={"donorId": "don", "donorName": "Ben"},
                        ids={"requestId": "r1", "offerId": "o1"})

        with mock.patch("notifications.worker.ensure_match_chat") as ensure:
            worker._offer_approved(intent)
        ensure.assert_called_once_with(worker._db, "r1", "o1", "req", "don", "Ana Cruz", "Ben")

    def test_request_approved_records_only_a_count(self):
        from notifications.events import Intent, Kind

        worker = self._worker()
        worker._requester_id = mock.Mock(return_value="req")
        worker._matching_donors = mock.Mock(
            return_value=[mock.Mock(user_id="d1"), mock.Mock(user_id="d2")])
        intent = Intent(Kind.REQUEST_APPROVED, "k", None, data={"bloodType": "A+"},
                        ids={"requestId": "r1"})
        worker._request_approved(intent)
        worker._db.collection.assert_any_call("blood_requests")
        worker._db.collection.return_value.document.return_value.update.assert_called_with(
            {"matchingDonorsNotified": 2})


if __name__ == "__main__":
    unittest.main()
