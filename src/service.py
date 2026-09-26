from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ValidationError, ensure_role, normalize_severity,
                     require_number, require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, HOLD_ROLES, PURGE_ROLES,
                    RECORD_ROLES, TERMINAL_STATES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, hold_effective,
                    priority_score, response_deadline_hours, retention_deadline,
                    role_for_transition, validate_transition)


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
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        retention_until = None
        if target in TERMINAL_STATES:
            retention_until = retention_deadline(item["severity"], utc_now())
        updated = self.repository.transition_item(item_id, target, expected_version,
                                                  actor, retention_until)
        detail = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        if retention_until is not None:
            detail["retention_until"] = retention_until
        self.repository.append_audit("transition", ENTITY, item_id, actor, detail)
        return self.enrich(updated)

    def place_hold(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, HOLD_ROLES)
        actor = require_text(actor, "actor", 100)
        case_no = require_text(payload.get("case_no"), "case_no", 100)
        reason = require_text(payload.get("reason"), "reason")
        custodian = require_text(payload.get("custodian"), "custodian", 100)
        start_at = require_timestamp(payload.get("start_at"), "start_at")
        end_at = require_timestamp(payload.get("end_at"), "end_at")
        if end_at <= start_at:
            raise ValidationError("end_at必须晚于start_at")
        hold = self.repository.create_hold(item_id, case_no, reason, custodian,
                                           start_at, end_at, actor)
        self.repository.append_audit("hold_place", ENTITY, item_id, actor, {
            "hold_id": hold["id"], "case_no": case_no, "custodian": custodian,
            "start_at": start_at, "end_at": end_at,
        })
        return hold

    def release_hold(self, hold_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, HOLD_ROLES)
        actor = require_text(actor, "actor", 100)
        hold = self.repository.release_hold(hold_id, actor)
        self.repository.append_audit("hold_release", ENTITY, hold["item_id"], actor, {
            "hold_id": hold["id"], "case_no": hold["case_no"],
        })
        return hold

    def list_holds(self, item_id: int, role: str) -> list:
        self._view(role)
        now = utc_now()
        holds = self.repository.list_holds(item_id)
        for hold in holds:
            hold["effective"] = hold_effective(hold, now)
        return holds

    def purge_expired(self, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, PURGE_ROLES)
        actor = require_text(actor, "actor", 100)
        now = utc_now()
        candidates = self.repository.purge_candidates(now)
        if not candidates:
            return {"purged": [], "count": 0}
        purged = self.repository.purge_batch(candidates, now, actor)
        return {"purged": purged, "count": len(purged)}

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
