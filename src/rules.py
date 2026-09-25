import math
from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def _added_reports(data):
    reports = data.get("added_reports")
    if reports is None:
        reports = data.get("new_reports")
    return reports or []


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_revision(actor, data, lookup=None):
    added_reports = _added_reports(data)
    if not isinstance(added_reports, list) or not added_reports:
        raise ValidationError("revision requires at least one added report")
    if not all(isinstance(report, dict) and report for report in added_reports):
        raise ValidationError("added reports must be non-empty objects")
    magnitude = data.get("magnitude")
    if magnitude is None:
        raise ValidationError("magnitude review result is required")
    try:
        magnitude = float(magnitude)
    except (TypeError, ValueError):
        raise ValidationError("magnitude must be a number")
    if not _is_number(magnitude) or magnitude < 0:
        raise ValidationError("magnitude must be a non-negative number")
    return {
        "added_reports": added_reports,
        "magnitude": magnitude,
        "magnitude_review": {
            "magnitude": magnitude,
            "submitted_by": actor.user_id,
            "note": data.get("magnitude_note") or data.get("note") or "",
        },
        "reason": data.get("reason", ""),
    }


def _validate_revision_decision(action, actor, revision, event, data=None):
    if revision["status"] != "pending_review":
        raise InvalidTransition("cannot %s revision from status %s" % (action, revision["status"]))
    if action == "reject":
        return {}

    result = str((data or {}).get("review_result", "approved")).lower()
    if result not in {"approved", "confirmed", "updated"}:
        raise ValidationError("invalid review_result")

    magnitude = (data or {}).get("magnitude", revision["data"].get("magnitude"))
    try:
        magnitude = float(magnitude)
    except (TypeError, ValueError):
        raise ValidationError("magnitude must be a number")
    if not _is_number(magnitude) or magnitude < 0:
        raise ValidationError("magnitude must be a non-negative number")
    return {
        "review_result": result,
        "magnitude": magnitude,
        "magnitude_review": {
            "magnitude": magnitude,
            "submitted_by": revision["created_by"],
            "reviewed_by": actor.user_id,
            "result": result,
            "note": (data or {}).get("magnitude_note") or (data or {}).get("note") or "",
        },
    }


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event, 'revision': _validate_revision}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'revisions': 'revision', 'drafts': 'revision'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'revision': 'pending_review'}
    TRANSITIONS = {
        'station': {
            'offline': (('online',), 'offline'),
            'online': (('offline',), 'online'),
        },
        'event': {
            'associate': (('candidate',), 'associated'),
            'review': (('associated',), 'reviewed'),
            'publish': (('reviewed',), 'published'),
            'withdraw': (('published', 'revised'), 'withdrawn'),
        },
        'revision': {
            'approve': (('pending_review',), 'approved'),
            'reject': (('pending_review',), 'rejected'),
        },
    }
    CREATE_REQUIRED = {
        'station': ('code', 'lat', 'lon'),
        'event': ('title', 'origin_time', 'location', 'reports'),
        'revision': ('added_reports', 'magnitude'),
    }
    ACTION_REQUIRED = {
        ('station', 'offline'): ('reason',),
        ('event', 'review'): ('reviewer', 'magnitude'),
        ('event', 'publish'): ('communication_id',),
        ('event', 'withdraw'): ('reason',),
    }
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst'), 'revision': ('admin', 'analyst')}
    ROLE_ACTIONS = {
        'offline': ('admin', 'station'),
        'online': ('admin', 'station'),
        'associate': ('admin', 'analyst'),
        'review': ('admin', 'reviewer'),
        'publish': ('admin', 'reviewer'),
        'withdraw': ('admin', 'reviewer'),
        'approve': ('admin', 'reviewer'),
        'reject': ('admin', 'reviewer'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        if kind == "revision":
            if not data.get("added_reports") and data.get("new_reports"):
                data["added_reports"] = data["new_reports"]
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            return custom(actor, data, lookup)
        return dict(data)

    def validate_revision_submit(self, actor, event, data, pending_exists=False):
        self._ensure_role(actor, self.CREATE_ROLES["revision"])
        if event["kind"] != "event":
            raise ValidationError("revisions can only belong to events")
        if event["status"] not in {"published", "revised"}:
            raise InvalidTransition("cannot revise an event from status %s" % event["status"])
        payload = self.validate_create(actor, "revision", data)
        payload["event_id"] = event["id"]
        payload["event_version"] = event["version"]
        payload["event_status"] = event["status"]
        if pending_exists:
            raise ConflictError(
                "当前已有待审修订草案：%s，不能同时排队" % pending_exists,
                {"revision_id": pending_exists},
            )
        return payload

    def validate_revision_decision(self, actor, revision, event, action, data=None):
        transition = self.TRANSITIONS["revision"].get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for revision" % action)
        self._ensure_role(actor, self.ROLE_ACTIONS[action])
        patch = _validate_revision_decision(action, actor, revision, event, dict(data or {}))
        if event["status"] not in {"published", "revised"}:
            raise InvalidTransition("event cannot accept a revision decision from status %s" % event["status"])
        return transition[1], patch

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
