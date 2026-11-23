"""Unit tests for blood request expiration. Run: python -m unittest discover tests"""

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from jobs.request_expiration import (
    MAX_REQUEST_AGE,
    MIN_INTERVAL_SECONDS,
    ExpirationSettings,
    RequestExpirer,
    created_at,
    expiration_updates,
    expiry_reason,
    is_overdue,
    needed_by_date,
    ph_today,
)
from notifications.constants import PH_TZ

TODAY = date(2026, 11, 23)
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=PH_TZ).astimezone(timezone.utc)  # 10:00 PH
TS = object()  # stands in for SERVER_TIMESTAMP


def ph_midnight(day: date) -> datetime:
    """How the app stores a picked date: local (PH) midnight, read back as UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=PH_TZ).astimezone(timezone.utc)


def request(status="active", needed=TODAY - timedelta(days=1), created=NOW - timedelta(days=1), **extra):
    data = {"status": status, **extra}
    if needed is not None:
        data["neededByDate"] = ph_midnight(needed)
    if created is not None:
        data["createdAt"] = created
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


class CreatedAt(unittest.TestCase):
    def test_timestamp_and_strings(self):
        self.assertEqual(created_at({"createdAt": NOW}), NOW)
        self.assertEqual(created_at({"createdAt": "2026-11-23T02:00:00Z"}), NOW)
        # Naive values are treated as UTC.
        self.assertEqual(created_at({"createdAt": NOW.replace(tzinfo=None)}), NOW)

    def test_missing_or_garbage(self):
        self.assertIsNone(created_at({}))
        self.assertIsNone(created_at({"createdAt": None}))
        self.assertIsNone(created_at({"createdAt": "yesterday"}))


class Overdue(unittest.TestCase):
    def test_open_request_past_its_date_is_overdue(self):
        self.assertTrue(is_overdue(request("active"), NOW))
        self.assertTrue(is_overdue(request("approved"), NOW))
        self.assertTrue(is_overdue(request(" Approved "), NOW))
        self.assertEqual(expiry_reason(request(), NOW), "needed_by")

    def test_still_open_on_the_needed_by_day(self):
        self.assertFalse(is_overdue(request(needed=TODAY), NOW))
        self.assertFalse(is_overdue(request(needed=TODAY + timedelta(days=3)), NOW))

    def test_only_open_requests_expire(self):
        old = NOW - timedelta(days=30)
        for status in ("pending", "matched", "fulfilled", "cancelled", "rejected", "expired", ""):
            self.assertFalse(is_overdue(request(status, created=old), NOW), status)

    def test_expires_seven_days_after_created(self):
        later = TODAY + timedelta(days=10)
        self.assertEqual(MAX_REQUEST_AGE, timedelta(days=7))
        one_second_short = NOW - MAX_REQUEST_AGE + timedelta(seconds=1)
        self.assertFalse(is_overdue(request(needed=later, created=one_second_short), NOW))
        self.assertEqual(expiry_reason(request(needed=later, created=NOW - MAX_REQUEST_AGE), NOW), "max_age")
        self.assertEqual(expiry_reason(request(needed=later, created=NOW - timedelta(days=9)), NOW), "max_age")

    def test_whichever_comes_first(self):
        # Needed-by date passed although the request is only a day old.
        self.assertEqual(expiry_reason(request(created=NOW - timedelta(days=1)), NOW), "needed_by")
        # Over seven days old although it is needed next month.
        next_month = TODAY + timedelta(days=30)
        self.assertEqual(
            expiry_reason(request(needed=next_month, created=NOW - timedelta(days=8)), NOW), "max_age"
        )
        # Neither yet.
        soon = TODAY + timedelta(days=2)
        self.assertIsNone(expiry_reason(request(needed=soon, created=NOW - timedelta(days=2)), NOW))

    def test_undated_request_expires_by_age(self):
        self.assertFalse(is_overdue(request(needed=None, created=NOW - timedelta(days=6)), NOW))
        self.assertEqual(expiry_reason(request(needed=None, created=NOW - timedelta(days=7)), NOW), "max_age")

    def test_no_usable_dates_never_expires(self):
        self.assertFalse(is_overdue(request(needed=None, created=None), NOW))
        self.assertFalse(is_overdue(request(needed=None, created=None, createdAt="garbage"), NOW))

    def test_missing_request(self):
        self.assertFalse(is_overdue(None, NOW))
        self.assertFalse(is_overdue({}, NOW))


class Updates(unittest.TestCase):
    def test_expires_request_and_summary(self):
        request_update, summary_update = expiration_updates(request(), True, NOW, TS)
        self.assertEqual(request_update, {"status": "expired", "expiredAt": TS, "updatedAt": TS})
        self.assertEqual(summary_update, {"status": "expired", "updatedAt": TS})

    def test_no_summary_update_when_summary_missing(self):
        _, summary_update = expiration_updates(request(), False, NOW, TS)
        self.assertIsNone(summary_update)

    def test_expiry_by_age_uses_the_same_updates(self):
        aged = request(needed=TODAY + timedelta(days=5), created=NOW - timedelta(days=8))
        request_update, summary_update = expiration_updates(aged, True, NOW, TS)
        self.assertEqual(request_update["status"], "expired")
        self.assertEqual(summary_update["status"], "expired")

    def test_leaves_request_closed_in_the_meantime(self):
        # Re-read inside the transaction: PRC fulfilled it after the query ran.
        self.assertIsNone(expiration_updates(request("fulfilled"), True, NOW, TS))
        self.assertIsNone(expiration_updates(None, True, NOW, TS))


class Settings(unittest.TestCase):
    def test_defaults_to_hourly(self):
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
        super().__init__(db, ExpirationSettings(interval_seconds=15), clock=lambda: NOW)
        self.expired_calls = []
        self.fail = set(fail)

    def _expire(self, request_id, now):
        self.expired_calls.append((request_id, now))
        if request_id in self.fail:
            raise RuntimeError("transaction failed")
        return True


class RunOnce(unittest.TestCase):
    def test_expires_only_overdue_requests(self):
        db = _FakeDb({
            "past_due": request("active"),
            "due_today": request("approved", needed=TODAY),
            "future": request("active", needed=TODAY + timedelta(days=2)),
            "undated_new": request("active", needed=None),
            "stale": request("active", needed=TODAY + timedelta(days=20), created=NOW - timedelta(days=8)),
            "undated_stale": request("active", needed=None, created=NOW - timedelta(days=7)),
        })
        expirer = _RecordingExpirer(db)

        self.assertEqual(expirer.run_once(), ["past_due", "stale", "undated_stale"])
        self.assertEqual(
            expirer.expired_calls, [("past_due", NOW), ("stale", NOW), ("undated_stale", NOW)]
        )
        self.assertEqual(expirer.status()["expired"], 3)
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
