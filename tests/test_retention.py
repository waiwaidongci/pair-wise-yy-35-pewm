import unittest
from datetime import timedelta
from src import rules
from src.domain import ValidationError
from src.rules import RETENTION_DAYS

CLOSED_AT = "2026-01-01T00:00:00+00:00"
FUTURE = "2026-06-01T00:00:00+00:00"


def make_item(retention, status="closed"):
    return {"status": status, "retention_until": retention}


def make_hold(start, end, status=rules.HOLD_ACTIVE):
    return {"status": status, "hold_from": start, "hold_to": end}


class RetentionRulesTest(unittest.TestCase):
    def test_retention_until_by_severity(self):
        for sev, days in RETENTION_DAYS.items():
            until = rules.retention_until(sev, CLOSED_AT)
            self.assertEqual(until - rules._parse_ts(CLOSED_AT, "x"),
                             timedelta(days=days))
        critical = rules.retention_until("critical", CLOSED_AT)
        low = rules.retention_until("low", CLOSED_AT)
        self.assertGreater(critical, low)
        with self.assertRaises(ValidationError):
            rules.retention_until("unknown", CLOSED_AT)

    def test_cleanup_requires_closed_and_retention(self):
        until = rules.retention_until("low", CLOSED_AT).isoformat()
        ok, reason = rules.cleanup_decision(make_item(until), [], "2028-01-02T00:00:00+00:00")
        self.assertTrue(ok)
        self.assertIsNone(reason)
        ok, reason = rules.cleanup_decision(
            make_item(until, status="investigation"), [], "2028-01-02T00:00:00+00:00")
        self.assertFalse(ok)
        self.assertEqual(reason, "not_closed")
        ok, reason = rules.cleanup_decision(
            make_item(None), [], FUTURE)
        self.assertFalse(ok)
        self.assertEqual(reason, "retention_missing")

    def test_within_retention_blocks(self):
        until = rules.retention_until("critical", CLOSED_AT).isoformat()
        ok, reason = rules.cleanup_decision(make_item(until), [], FUTURE)
        self.assertFalse(ok)
        self.assertEqual(reason, "within_retention")

    def test_active_hold_blocks(self):
        until = rules.retention_until("low", CLOSED_AT).isoformat()
        window = make_hold("2025-01-01T00:00:00+00:00", "2028-06-01T00:00:00+00:00")
        # 保留期已过（2027年后），但有效保全仍拦住清理
        ok, reason = rules.cleanup_decision(
            make_item(until), [window], "2028-01-02T00:00:00+00:00")
        self.assertFalse(ok)
        self.assertEqual(reason, "active_hold")
        # 保全到期后不再拦
        ok, reason = rules.cleanup_decision(
            make_item(until), [window], "2028-07-01T00:00:00+00:00")
        self.assertTrue(ok)

    def test_hold_window_semantics(self):
        start = "2026-01-01T00:00:00+00:00"
        end = "2026-12-31T00:00:00+00:00"
        hold = make_hold(start, end)
        self.assertTrue(rules.hold_is_active(hold, start))  # 起始时刻即生效
        self.assertTrue(rules.hold_is_active(hold, "2026-06-01T00:00:00+00:00"))
        self.assertFalse(rules.hold_is_active(hold, end))  # 截止时刻后不再拦
        self.assertFalse(rules.hold_is_active(hold, "2025-01-01T00:00:00+00:00"))
        self.assertFalse(rules.hold_is_active(
            make_hold(start, end, status=rules.HOLD_RELEASED), FUTURE))
        with self.assertRaises(ValidationError):
            rules.validate_hold_window(end, start)
        with self.assertRaises(ValidationError):
            rules.validate_hold_window(start, "not-a-time")


if __name__ == "__main__":
    unittest.main()
