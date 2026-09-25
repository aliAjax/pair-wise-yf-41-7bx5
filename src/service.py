from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idem_key=idempotency_key, entity_id=entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            entity = self.repository.get_revision(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "revision":
            if action in {"approve", "reject"}:
                return self.revision_decision(actor, entity_id, action, data)
            raise InvalidTransition("unknown action %s for revision" % action)
        if entity["kind"] == "event" and action == "revise":
            return self.submit_revision(
                actor,
                entity_id,
                data,
                expected_event_version=expected_version,
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        return self._apply_transition(actor, entity, expected, next_status, merged, action)

    def _apply_transition(self, actor, entity, expected_version, next_status, merged, action):
        updated = self.repository.update_entity(
            entity["id"], expected_version, next_status, merged, actor.user_id
        )
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"previous_version": entity["version"], "version": updated["version"]},
        )
        return updated

    def submit_revision(self, actor, event_id, data=None, expected_event_version=None):
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("event not found: " + event_id)
        pending = self.repository.get_pending_revision(event_id)
        payload = self.rules.validate_revision_submit(
            actor, event, dict(data or {}), pending["id"] if pending else False
        )
        expected = (
            int(expected_event_version)
            if expected_event_version is not None
            else event["version"]
        )
        try:
            revision = self.repository.create_revision(event_id, expected, payload, actor.user_id)
        except ConflictError as exc:
            if exc.details and exc.details.get("revision_id"):
                raise ConflictError(
                    "当前已有待审修订草案：%s，不能同时排队" % exc.details["revision_id"],
                    exc.details,
                )
            raise
        self.audit.record(
            event_id,
            actor,
            "submit_revision",
            event["status"],
            "pending_review",
            {
                "revision_id": revision["id"],
                "revision_no": revision["revision_no"],
                "event_version": event["version"],
                "added_report_count": len(payload["added_reports"]),
            },
        )
        return revision

    @staticmethod
    def _apply_approved_revision(revision, event, decision):
        reports = list(event["data"].get("reports") or [])
        reports.extend(revision["data"].get("added_reports") or [])
        merged = dict(event["data"])
        merged.update(
            {
                "reports": reports,
                "magnitude": decision.get("magnitude", revision["data"].get("magnitude")),
                "latest_revision_id": revision["id"],
                "latest_revision_no": revision["revision_no"],
                "magnitude_review": decision.get("magnitude_review"),
            }
        )
        next_status = "revised"
        return next_status, merged

    def revision_decision(self, actor, revision_id, action, data=None):
        revision = self.repository.get_revision(revision_id)
        if not revision:
            raise NotFoundError("revision not found: " + revision_id)
        event = self.repository.get_entity(revision["event_id"])
        if not event:
            raise NotFoundError("event not found: " + revision["event_id"])
        next_status, patch = self.rules.validate_revision_decision(
            actor, revision, event, action, data
        )

        def apply_update(current_revision, current_event, decision):
            return self._apply_approved_revision(current_revision, current_event, decision)

        updated_revision, updated_event = self.repository.decide_revision(
            revision_id, action, patch, actor.user_id, apply_update
        )
        if action == "approve":
            self.audit.record(
                event["id"],
                actor,
                "approve_revision",
                "pending_review",
                "approved",
                {
                    "revision_id": revision_id,
                    "revision_no": revision["revision_no"],
                    "previous_version": event["version"],
                    "version": updated_event["version"],
                    "submitted_by": revision["created_by"],
                    "reviewed_by": actor.user_id,
                    "event_from_status": event["status"],
                    "event_to_status": updated_event["status"],
                },
            )
        else:
            self.audit.record(
                event["id"],
                actor,
                "reject_revision",
                "pending_review",
                "rejected",
                {
                    "revision_id": revision_id,
                    "revision_no": revision["revision_no"],
                    "submitted_by": revision["created_by"],
                    "reviewed_by": actor.user_id,
                },
            )
        return updated_revision

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if entity:
            return entity
        revision = self.repository.get_revision(entity_id)
        if revision:
            return revision
        raise NotFoundError("entity not found: " + entity_id)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        if kind == "revision":
            return self.repository.list_revisions(status=status)
        return self.repository.list_entities(kind=kind, status=status)

    def revisions(self, event_id=None, status=None):
        if event_id:
            event = self.repository.get_entity(event_id)
            if not event:
                raise NotFoundError("event not found: " + event_id)
        return self.repository.list_revisions(event_id=event_id, status=status)

    def history(self, event_id):
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("event not found: " + event_id)
        revisions = self.repository.list_revisions(event_id=event_id)
        revisions_by_version = {}
        for revision in revisions:
            revisions_by_version.setdefault(revision["event_version"], []).append(
                {
                    "id": revision["id"],
                    "revision_no": revision["revision_no"],
                    "status": revision["status"],
                    "created_by": revision["created_by"],
                    "created_at": revision["created_at"],
                    "reviewed_by": revision["reviewed_by"],
                    "reviewed_at": revision["reviewed_at"],
                    "data": revision["data"],
                }
            )
        versions = self.repository.list_entity_versions(event_id)
        history_versions = []
        for item in versions:
            history_versions.append(
                {
                    "version": item["version"],
                    "status": item["status"],
                    "data": item["data"],
                    "created_by": item["created_by"],
                    "updated_by": item["updated_by"],
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                    "draft_revisions": revisions_by_version.pop(item["version"], []),
                }
            )
        return {
            "event_id": event_id,
            "current_version": event["version"],
            "versions": history_versions,
            "orphan_revisions": [
                revision
                for items in revisions_by_version.values()
                for revision in items
            ],
            "audit_log": self.repository.list_audit(event_id),
        }

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
