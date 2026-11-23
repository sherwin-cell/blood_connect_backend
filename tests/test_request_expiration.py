"""Unit tests for blood request expiration. Run: python -m unittest discover tests"""

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from jobs.request_expiration import (
    MIN_INTERVAL_SECONDS,
    ExpirationSettings,
    RequestExpirer,
    expiration_updates,
    is_overdue,
    needed_by_date,
    ph_today,
)
from notifications.constants import PH_TZ

TODAY = date(2026, 11, 23)
TS = object()  # stands in for SERVER_TIMESTAMP


def ph_midnight(day: date) -> datetime:
    """How the app stores a picked date: local (PH) midnight, read back as UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=PH_TZ).astimezone(timezone.utc)


def request(status="active", needed=TODAY - timedelta(days=1), **extra):
    data = {"status": status, **extra}
    if needed is not None:
        data["neededByDate"] = ph_midnight(needed)
    return data


class NeededByDate(unittest.TestCase):
    def test_firestore_timestamp_is_read_in_ph_time(self):
        # Nov 23 00:00 PH is Nov 22 16:00 UTC; the date must still be Nov 23.
        self.assertEqual(needed_by_date({"neededByDate": ph_midnight(TODAY)}), TODAY)

    def test_falls_back_to_older_fields(self):
        self.assertEqual(needed_by_date({"neededBy": ph_midnight(TODAY)}), TODAY)
        self.assertEqual(needed_by_date({"requiredDate": ph_midnight(TODAY)}), TODAY)

    def test_prefers_needed_by_date(self):
        data = {"neededByDate": ph_midnight(TODAY), "requiredDate": ph_midnight(date(2026, 1, 1))}
        self.assertEqual(needed_by_date(data), TODAY)

    def test_string_dates(self):
        self.assertEqual(needed_by_date({"neededByDate": "2026-11-23"}), TODAY)
        self.assertEqual(needed_by_date({"neededByDate": "2026-11-22T16:00:00Z"}), TODAY)

    def test_unparseable_value_falls_through_to_next_field(self):
        data = {"neededByDate": "soon", "requiredDate": ph_midnight(TODAY)}
        self.assertEqual(needed_by_date(data), TODAY)

    def test_missing_or_garbage(self):
        self.assertIsNone(needed_by_date({}))
        self.assertIsNone(needed_by_date({"neededByDate": None}))
        self.assertIsNone(needed_by_date({"neededByDate": "not a date"}))
        self.assertIsNone(needed_by_date({"neededByDate": 12345}))

    def test_ph_today_rolls_over_at_ph_midnight(self):
        self.assertEqual(ph_today(datetime(2026, 11, 22, 15, 59, tzinfo=timezone.utc)), date(2026, 11, 22))
        self.assertEqual(ph_today(datetime(2026, 11, 22, 16, 0, tzinfo=timezone.utc)), TODAY)


class Overdue(unittest.TestCase):
    def test_open_request_past_its_date_is_overdue(self):
        self.assertTrue(is_overdue(request("active"), TODAY))
        self.assertTrue(is_overdue(request("approved"), TODAY))
        self.assertTrue(is_overdue(request(" Approved "), TODAY))

    def test_still_open_on_the_needed_by_day(self):
        self.assertFalse(is_overdue(request(needed=TODAY), TODAY))
        self.assertFalse(is_overdue(request(needed=TODAY + timedelta(days=3)), TODAY))

    def test_only_open_requests_expire(self):
        for status in ("pending", "matched", "fulfilled", "cancelled", "rejected", "expired", ""):
            self.assertFalse(is_overdue(request(status), TODAY), status)

    def test_no_date_never_expires(self):
        self.assertFalse(is_overdue(request(needed=None), TODAY))

    def test_missing_request(self):
        self.assertFalse(is_overdue(None, TODAY))
        self.assertFalse(is_overdue({}, TODAY))


class Updates(unittest.TestCase):
    def test_expires_request_and_summary(self):
        request_update, summary_update = expiration_updates(request(), True, TODAY, TS)
        self.assertEqual(request_update, {"status": "expired", "expiredAt": TS, "updatedAt": TS})
        self.assertEqual(summary_update, {"status": "expired", "updatedAt": TS})

    def test_no_summary_update_when_summary_missing(self):
        _, summary_update = expiration_updates(request(), False, TODAY, TS)
        self.assertIsNone(summary_update)

    def test_leaves_request_closed_in_the_meantime(self):
        # Re-read inside the transaction: PRC fulfilled it after the query ran.
        self.assertIsNone(expiration_updates(request("fulfilled"), True, TODAY, TS))
        self.assertIsNone(expiration_updates(None, True, TODAY, TS))


class Settings(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = ExpirationSettings.from_env()
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.interval_seconds, 3600)

    def test_short_interval_for_testing(self):
        env = {"REQUEST_EXPIRATION_INTERVAL_SECONDS": "15", "REQUEST_EXPIRATION_ENABLED": "false"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = ExpirationSettings.from_env()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.interval_seconds, 15)

    def test_interval_has_a_floor_and_ignores_garbage(self):
        with mock.patch.dict(os.environ, {"REQUEST_EXPIRATION_INTERVAL_SECONDS": "0"}, clear=True):
            self.assertEqual(ExpirationSettings.from_env().interval_seconds, MIN_INTERVAL_SECONDS)
        with mock.patch.dict(os.environ, {"REQUEST_EXPIRATION_INTERVAL_SECONDS": "x"}, clear=True):
            self.assertEqual(ExpirationSettings.from_env().interval_seconds, 3600)


# --------------------------------------------------------------------------
# RequestExpirer.run_once against a fake Firestore query
# --------------------------------------------------------------------------


class _Snapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return self._data


class _FakeDb:
    def __init__(self, docs):
        self.docs = docs
        self.filters = []

    def collection(self, name):
        assert name == "blood_requests"
        return self

    def where(self, filter):
        self.filters.append(filter)
        return self

    def stream(self):
        return [_Snapshot(doc_id, data) for doc_id, data in self.docs.items()]


class _RecordingExpirer(RequestExpirer):
    def __init__(self, db, fail=()):
        now = datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)  # 10:00 PH
        super().__init__(db, ExpirationSettings(interval_seconds=15), clock=lambda: now)
        self.expired_calls = []
        self.fail = set(fail)

    def _expire(self, request_id, today):
        self.expired_calls.append((request_id, today))
        if request_id in self.fail:
            raise RuntimeError("transaction failed")
        return True


class RunOnce(unittest.TestCase):
    def test_expires_only_overdue_requests(self):
        db = _FakeDb({
            "old": request("active"),
            "today": request("approved", needed=TODAY),
            "future": request("active", needed=TODAY + timedelta(days=2)),
            "undated": request("active", needed=None),
        })
        expirer = _RecordingExpirer(db)

        self.assertEqual(expirer.run_once(), ["old"])
        self.assertEqual(expirer.expired_calls, [("old", TODAY)])
        self.assertEqual(expirer.status()["expired"], 1)
        self.assertEqual(expirer.status()["runs"], 1)
        self.assertEqual(expirer.status()["intervalSeconds"], 15)

        f = db.filters[0]
        self.assertEqual((f.field_path, f.op_string, list(f.value)), ("status", "in", ["approved", "active"]))

    def test_one_failure_does_not_stop_the_rest(self):
        db = _FakeDb({"a": request(), "b": request(), "c": request()})
        expirer = _RecordingExpirer(db, fail={"b"})

        self.assertEqual(expirer.run_once(), ["a", "c"])
        self.assertEqual(expirer.status()["errors"], 1)

    def test_nothing_overdue(self):
        expirer = _RecordingExpirer(_FakeDb({"x": request(needed=TODAY)}))
        self.assertEqual(expirer.run_once(), [])
        self.assertEqual(expirer.expired_calls, [])


if __name__ == "__main__":
    unittest.main()
