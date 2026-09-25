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
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if entity["kind"] == "revision" and action == "approve":
            self._apply_revision(actor, entity)
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _apply_revision(self, actor, revision):
        event_id = revision["data"].get("event_id")
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("event not found: " + str(event_id))
        if event["status"] not in ("published", "revised"):
            raise InvalidTransition(
                "event %s is no longer published" % event_id
            )
        self.repository.save_entity_version(
            entity_id=event["id"],
            version=event["version"],
            status=event["status"],
            data=event["data"],
            actor_id=actor.user_id,
            revision_id=revision["id"],
        )
        new_reports = list(revision["data"].get("reports") or [])
        merged = dict(event["data"])
        merged["reports"] = list(merged.get("reports") or []) + new_reports
        merged["magnitude"] = revision["data"].get("magnitude")
        merged["last_revision_id"] = revision["id"]
        if revision["data"].get("reason"):
            merged["revision_reason"] = revision["data"]["reason"]
        updated = self.repository.update_entity(
            event["id"], event["version"], "revised", merged
        )
        self.audit.record(
            event["id"],
            actor,
            "apply_revision",
            event["status"],
            updated["status"],
            {
                "revision_id": revision["id"],
                "analyst": revision["created_by"],
                "reviewer": actor.user_id,
                "from_version": event["version"],
                "to_version": updated["version"],
                "added_reports": len(new_reports),
            },
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def history(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return {
            "entity": entity,
            "versions": self.repository.list_entity_history(entity_id),
            "audit": self.repository.list_audit(entity_id=entity_id),
        }
