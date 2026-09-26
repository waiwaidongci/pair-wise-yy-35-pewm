from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, NotFoundError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CLEANUP_ROLES, CREATE_ROLES, ENTITY,
                    HOLD_ROLES, HOLD_VIEW_ROLES, RECORD_ROLES, TERMINAL_STATES,
                    TITLE, VIEW_ROLES, cleanup_decision, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, retention_until,
                    role_for_transition, ts_normalize, validate_hold_window,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        # 结案时按严重程度生成保留截止日；非结案流转不改已有值
        retention = None
        if target in TERMINAL_STATES:
            retention = retention_until(item["severity"], utc_now()).isoformat()
        updated = self.repository.transition_item(
            item_id, target, expected_version, actor, retention)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "retention_until": retention,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def place_hold(self, item_id: int, payload: Dict[str, Any],
                   actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOLD_ROLES)
        actor = require_text(actor, "actor", 100)
        case_no = require_text(payload.get("case_no"), "case_no", 100)
        reason = require_text(payload.get("reason"), "reason")
        custodian = require_text(payload.get("custodian"), "custodian", 100)
        hold_from, hold_to = validate_hold_window(
            payload.get("hold_from"), payload.get("hold_to"))
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 2000)
        hold = self.repository.create_legal_hold(
            item_id, case_no, reason, custodian, hold_from, hold_to, note, actor)
        self.repository.append_audit("legal_hold_placed", ENTITY, item_id, actor, {
            "hold_id": hold["id"], "case_no": case_no, "custodian": custodian,
            "hold_from": hold_from, "hold_to": hold_to, "reason": reason,
        })
        return hold

    def release_hold(self, item_id: int, hold_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOLD_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        hold = self.repository.get_legal_hold(hold_id)
        if hold["item_id"] != item_id:
            raise NotFoundError("保全单不存在")
        note = (payload or {}).get("note")
        if note is not None:
            note = require_text(note, "note", 2000)
        released = self.repository.release_legal_hold(hold_id, actor, note)
        self.repository.append_audit("legal_hold_released", ENTITY, item_id, actor, {
            "hold_id": hold_id, "case_no": hold["case_no"],
        })
        return released

    def list_holds(self, item_id: int, role: str) -> list:
        ensure_role(role, HOLD_VIEW_ROLES)
        return self.repository.list_legal_holds(item_id)

    def cleanup_expired(self, actor: str, role: str,
                        now: Optional[str] = None) -> Dict[str, Any]:
        """例行清理入口：先按规则筛选，执行前对每条重查截止日与保全状态。

        重查时若版本/截止日/保全发生变动则本次跳过（changed），退回下轮重算。
        """
        ensure_role(role, CLEANUP_ROLES)
        actor = require_text(actor, "actor", 100)
        moment = utc_now() if now is None else ts_normalize(now, "now")
        deleted, skipped = [], []
        for item in self.repository.cleanup_candidates(moment):
            holds = self.repository.list_legal_holds(item["id"])
            allowed, reason = cleanup_decision(item, holds, moment)
            if not allowed:
                skipped.append({"item_id": item["id"], "reason": reason})
                continue
            done = self.repository.delete_item_guarded(
                item["id"], item["version"], item["retention_until"], moment)
            if not done:
                # 截止日、版本或保全在筛选后变动，退回重算
                skipped.append({"item_id": item["id"], "reason": "changed"})
                continue
            self.repository.append_audit("purged", ENTITY, item["id"], actor, {
                "severity": item["severity"],
                "retention_until": item["retention_until"],
            })
            deleted.append(item["id"])
        self.repository.append_audit("cleanup_run", ENTITY, 0, actor, {
            "now": moment, "deleted": deleted, "skipped": skipped,
        })
        return {"now": moment, "deleted": deleted, "skipped": skipped}

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
