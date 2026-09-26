import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import RETENTION_DAYS, STATES, TRANSITION_ROLES
from src.service import Service


class RetentionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.repo = Repository(self.db_path)
        self.service = Service(self.repo)
        self.now = "2026-09-26T00:00:00+00:00"

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _create(self, severity="low", ref="RET-1"):
        return self.service.create_item({
            "title": "retention item", "description": "retention flow",
            "severity": severity, "quantity": 1, "threshold": 10,
            "external_ref": ref,
        }, "creator", "dosimetrist")

    def _close(self, item):
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_close_generates_retention_until_by_severity(self):
        from datetime import datetime, timedelta
        item = self._create("high", "RET-H")
        before = datetime.now(tz=timezone.utc).replace(microsecond=0)
        closed = self._close(item)
        after = datetime.now(tz=timezone.utc).replace(microsecond=0)
        self.assertEqual(closed["status"], "closed")
        self.assertIsNotNone(closed["retention_until"])
        until = datetime.fromisoformat(closed["retention_until"])
        expected = timedelta(days=RETENTION_DAYS["high"])
        self.assertTrue(before + expected <= until <= after + expected)
        audit = self.service.audit("viewer", closed["id"])
        placed = [e for e in audit if e["action"] == "transition"
                  and e["detail"].get("to") == "closed"]
        self.assertEqual(len(placed), 1)
        self.assertIn("retention_until", placed[0]["detail"])
        # 非结案事件不产生保留截止日
        open_item = self._create("low", "RET-O")
        self.assertIsNone(open_item.get("retention_until"))

    def test_closed_within_retention_is_not_purged(self):
        closed = self._close(self._create("critical", "RET-C"))
        result = self.service.cleanup_expired(
            "officer", "radiation_officer", self.now)
        self.assertEqual(result["deleted"], [])
        self.assertTrue(
            all(s["item_id"] != closed["id"] or s["reason"] != "active_hold"
                for s in result["skipped"]))
        fetched = self.service.get_item(closed["id"], "viewer")
        self.assertEqual(fetched["id"], closed["id"])

    def test_expired_closed_item_is_purged(self):
        closed = self._close(self._create("low", "RET-E"))
        # 将保留截止日改到过去，模拟保留期满
        with self.repo._lock, self.repo.conn:
            self.repo.conn.execute(
                "UPDATE items SET retention_until=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", closed["id"]))
        result = self.service.cleanup_expired(
            "officer", "radiation_officer", self.now)
        self.assertEqual(result["deleted"], [closed["id"]])
        with self.assertRaises(Exception):
            self.service.get_item(closed["id"], "viewer")

    def test_active_hold_blocks_cleanup(self):
        closed = self._close(self._create("low", "RET-LH"))
        with self.repo._lock, self.repo.conn:
            self.repo.conn.execute(
                "UPDATE items SET retention_until=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", closed["id"]))
        hold = self.service.place_hold(closed["id"], {
            "case_no": "LIT-2026-001",
            "reason": "医疗纠纷诉讼，需保全全部剂量材料",
            "custodian": "张保管",
            "hold_from": "2026-01-01T00:00:00+00:00",
            "hold_to": "2027-12-31T00:00:00+00:00",
        }, "officer", "radiation_officer")
        self.assertEqual(hold["case_no"], "LIT-2026-001")
        result = self.service.cleanup_expired(
            "officer", "radiation_officer", self.now)
        self.assertEqual(result["deleted"], [])
        self.assertIn({"item_id": closed["id"], "reason": "active_hold"},
                      result["skipped"])
        # 解除保全后恢复可清理
        self.service.release_hold(
            closed["id"], hold["id"], {"note": "纠纷结案"},
            "officer", "radiation_officer")
        result = self.service.cleanup_expired(
            "officer", "radiation_officer", self.now)
        self.assertEqual(result["deleted"], [closed["id"]])

    def test_recheck_recomputes_when_state_changes(self):
        closed = self._close(self._create("low", "RET-RC"))
        with self.repo._lock, self.repo.conn:
            self.repo.conn.execute(
                "UPDATE items SET retention_until=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", closed["id"]))
        candidates = self.repo.cleanup_candidates(self.now)
        self.assertIn(closed["id"], [c["id"] for c in candidates])
        # 筛选后、执行前插入保全：原子重查必须拦住删除
        self.repo.create_legal_hold(
            closed["id"], "CASE-X", "调查", "保管人",
            "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
            None, "officer")
        deleted = self.repo.delete_item_guarded(
            closed["id"], closed["version"], "2000-01-01T00:00:00+00:00",
            self.now)
        self.assertFalse(deleted)
        self.service.get_item(closed["id"], "viewer")  # 仍在库中
        # 解除保全后，保留期在筛选后被延长：服务层重查应退回重算
        self.repo.release_legal_hold(
            self.repo.list_legal_holds(closed["id"])[0]["id"], "officer")
        original = self.repo.cleanup_candidates

        def racy_candidates(now):
            rows = original(now)
            # 模拟例行清理扫描之后、执行删除之前监管要求延长期限
            with self.repo._lock, self.repo.conn:
                self.repo.conn.execute(
                    "UPDATE items SET retention_until=? WHERE id=?",
                    ("2030-01-01T00:00:00+00:00", closed["id"]))
            return rows

        self.repo.cleanup_candidates = racy_candidates
        try:
            result = self.service.cleanup_expired(
                "officer", "radiation_officer", self.now)
        finally:
            self.repo.cleanup_candidates = original
        self.assertEqual(result["deleted"], [])
        # 重查发现截止日已变动，本次退回（changed），下轮按新截止日重算
        self.assertIn({"item_id": closed["id"], "reason": "changed"},
                      result["skipped"])
        self.service.get_item(closed["id"], "viewer")  # 材料仍在库中

        # 下轮例行清理：新截止日在未来，该事件不再进入候选集合
        result = self.service.cleanup_expired(
            "officer", "radiation_officer", self.now)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["skipped"], [])
        self.service.get_item(closed["id"], "viewer")  # 材料仍在库中

    def test_hold_permissions_and_validation(self):
        closed = self._close(self._create("low", "RET-P"))
        with self.assertRaises(PermissionDenied):
            self.service.place_hold(closed["id"], {
                "case_no": "X", "reason": "r", "custodian": "c",
                "hold_from": "2026-01-01T00:00:00+00:00",
                "hold_to": "2027-01-01T00:00:00+00:00",
            }, "viewer-user", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.cleanup_expired("dosimetrist", "dosimetrist", self.now)
        with self.assertRaises(ValidationError):
            self.service.place_hold(closed["id"], {
                "case_no": "X", "reason": "r", "custodian": "c",
                "hold_from": "2027-01-01T00:00:00+00:00",
                "hold_to": "2026-01-01T00:00:00+00:00",
            }, "officer", "radiation_officer")
        self.service.place_hold(closed["id"], {
            "case_no": "DUP", "reason": "r", "custodian": "c",
            "hold_from": "2026-01-01T00:00:00+00:00",
            "hold_to": "2027-01-01T00:00:00+00:00",
        }, "officer", "radiation_officer")
        with self.assertRaises(ConflictError):
            self.service.place_hold(closed["id"], {
                "case_no": "DUP", "reason": "r", "custodian": "c",
                "hold_from": "2026-01-01T00:00:00+00:00",
                "hold_to": "2027-01-01T00:00:00+00:00",
            }, "officer", "radiation_officer")

    def test_audit_chain_intact(self):
        closed = self._close(self._create("low", "RET-A"))
        self.service.place_hold(closed["id"], {
            "case_no": "AUDIT-1", "reason": "r", "custodian": "c",
            "hold_from": "2026-01-01T00:00:00+00:00",
            "hold_to": "2027-01-01T00:00:00+00:00",
        }, "officer", "health_physicist")
        self.service.cleanup_expired(
            "officer", "radiation_officer", self.now)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
