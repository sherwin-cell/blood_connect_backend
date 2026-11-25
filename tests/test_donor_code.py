import unittest
from unittest import mock

from notifications import content
from notifications.donor_code import _assign, format_donor_code, next_donor_number


def _snapshot(data):
    snap = mock.Mock(exists=data is not None)
    snap.to_dict.return_value = data
    return snap


class DonorCodeTest(unittest.TestCase):
    def test_format_and_next_number(self):
        self.assertEqual(format_donor_code(23), "DNR-0023")
        self.assertEqual(format_donor_code(12345), "DNR-12345")
        self.assertEqual(next_donor_number(None), 1)
        self.assertEqual(next_donor_number({"last": 22}), 23)
        self.assertEqual(next_donor_number({"last": "bad"}), 1)

    def _db(self, offer, counter):
        db = mock.MagicMock()
        offer_ref = mock.Mock()
        offer_ref.get.return_value = _snapshot(offer)
        request_ref = mock.Mock()
        request_ref.collection.return_value.document.return_value = offer_ref
        counter_ref = mock.Mock()
        counter_ref.get.return_value = _snapshot(counter)
        db.collection.side_effect = lambda name: mock.Mock(
            document=mock.Mock(
                return_value=request_ref if name == "blood_requests" else counter_ref))
        return db, request_ref, offer_ref, counter_ref

    def test_assigns_next_code_to_confirmed_offer(self):
        db, request_ref, offer_ref, counter_ref = self._db({"status": "approved"}, {"last": 22})
        tx = mock.Mock()
        self.assertEqual(_assign(tx, db, "r1", "o1"), "DNR-0023")
        tx.update.assert_any_call(offer_ref, {"donorCode": "DNR-0023"})
        self.assertEqual(tx.set.call_args.args[1]["last"], 23)
        request_update = [c for c in tx.update.call_args_list if c.args[0] is request_ref]
        self.assertEqual(len(request_update), 1)

    def test_keeps_existing_code_and_skips_unconfirmed(self):
        db, *_ = self._db({"status": "approved", "donorCode": "DNR-0007"}, {"last": 9})
        tx = mock.Mock()
        self.assertEqual(_assign(tx, db, "r1", "o1"), "DNR-0007")
        tx.update.assert_not_called()

        db, *_ = self._db({"status": "pending"}, {"last": 9})
        self.assertIsNone(_assign(tx, db, "r1", "o1"))
        db, *_ = self._db(None, None)
        self.assertIsNone(_assign(tx, db, "r1", "o1"))
        tx.update.assert_not_called()
        tx.set.assert_not_called()

    def test_requester_notice_never_names_the_donor(self):
        _, body = content.offer_confirmed_for_requester(
            {"donorName": "Maria Santos", "donorCode": "DNR-0023"})
        self.assertIn("DNR-0023", body)
        self.assertNotIn("Maria", body)
        _, body = content.offer_confirmed_for_requester({"donorName": "Maria Santos"})
        self.assertNotIn("Maria", body)


class WorkerHooksTest(unittest.TestCase):
    def _worker(self):
        from notifications.worker import NotificationWorker

        worker = NotificationWorker.__new__(NotificationWorker)
        worker._db = mock.MagicMock()
        worker._notify = mock.Mock()
        worker._offer_requester_id = mock.Mock(return_value="req")
        return worker

    def _intent(self, **data):
        from notifications.events import Intent, Kind

        return Intent(Kind.OFFER_APPROVED, "k", None,
                      data={"donorId": "don", "donorName": "Ben", **data},
                      ids={"requestId": "r1", "offerId": "o1"})

    def test_offer_approved_assigns_code_and_opens_no_chat(self):
        worker = self._worker()
        with mock.patch("notifications.worker.assign_donor_code",
                        return_value="DNR-0005") as assign:
            worker._offer_approved(self._intent())
        assign.assert_called_once_with(worker._db, "r1", "o1")
        worker._db.collection.assert_not_called()  # no chats/ write
        requester_body = worker._notify.call_args_list[1].args[3][1]
        self.assertIn("DNR-0005", requester_body)
        self.assertNotIn("Ben", requester_body)

    def test_offer_approved_keeps_admin_code(self):
        worker = self._worker()
        with mock.patch("notifications.worker.assign_donor_code") as assign:
            worker._offer_approved(self._intent(donorCode="DNR-0001"))
        assign.assert_not_called()

    def test_no_notifications_for_retired_match_chats(self):
        from notifications.events import Intent, Kind

        worker = self._worker()
        chat = _snapshot({"type": "donor_match", "participants": ["req", "don"]})
        worker._db.collection.return_value.document.return_value.get.return_value = chat
        worker._chat_message(Intent(Kind.CHAT_MESSAGE, "k", None,
                                    data={"senderId": "don", "message": "hi"},
                                    ids={"chatId": "match_r1_o1", "messageId": "m1"}))
        worker._notify.assert_not_called()

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
