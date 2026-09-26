from __future__ import annotations
from datetime import datetime, timedelta, timezone
from .domain import ConflictError, ValidationError
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
# 结案后按严重程度确定保留期限（天）：低1年、升高3年、高7年、危重30年
RETENTION_DAYS={'low': 365, 'elevated': 1095, 'high': 2555, 'critical': 10950}
HOLD_ACTIVE='active'; HOLD_RELEASED='released'; HOLD_STATES=[HOLD_ACTIVE, HOLD_RELEASED]
HOLD_ROLES=set(['radiation_officer', 'health_physicist']); CLEANUP_ROLES=set(['radiation_officer']); HOLD_VIEW_ROLES=VIEW_ROLES


def _parse_ts(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field}必须是ISO 8601时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ts_normalize(value, field):
    return _parse_ts(value, field).replace(microsecond=0).isoformat()


def validate_hold_window(hold_from, hold_to):
    start = _parse_ts(hold_from, "hold_from")
    end = _parse_ts(hold_to, "hold_to")
    if end <= start:
        raise ValidationError("hold_to必须晚于hold_from")
    return (start.replace(microsecond=0).isoformat(), end.replace(microsecond=0).isoformat())


def retention_until(severity, closed_at):
    """结案时按严重程度生成保留截止日。"""
    if severity not in RETENTION_DAYS:
        raise ValidationError("unknown severity")
    return (_parse_ts(closed_at, "closed_at").replace(microsecond=0)
            + timedelta(days=RETENTION_DAYS[severity]))


def hold_is_active(hold, now):
    """有效保全：未解除且当前时刻落在起止区间内。"""
    if hold.get("status") != HOLD_ACTIVE:
        return False
    return ts_normalize(hold["hold_from"], "hold_from") <= now < ts_normalize(hold["hold_to"], "hold_to")


def cleanup_decision(item, holds, now):
    """返回(可清理, 原因)。未结案、保留期未满、存在有效保全都拦住清理。"""
    if item.get("status") not in TERMINAL_STATES:
        return False, "not_closed"
    until = item.get("retention_until")
    if not until:
        return False, "retention_missing"
    if ts_normalize(until, "retention_until") > now:
        return False, "within_retention"
    if any(hold_is_active(hold, now) for hold in holds):
        return False, "active_hold"
    return True, None
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
