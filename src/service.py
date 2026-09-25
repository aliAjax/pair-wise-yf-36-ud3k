from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
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
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "consent" and action == "activate":
            return self._activate_consent(actor, entity, dict(data or {}), expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
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

    @staticmethod
    def _audit_entry(entity_id, actor, action, from_status, to_status, detail):
        return {
            "entity_id": entity_id,
            "actor_id": actor.user_id,
            "actor_role": actor.role,
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "detail": detail,
        }

    def _activate_consent(self, actor, entity, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, "activate", data, self._lookup
        )
        plan = self.rules.plan_consent_activation(entity, patch, self._lookup)
        merged = dict(entity["data"])
        merged.update(patch)
        updates = [
            {
                "id": entity["id"],
                "expected_version": expected,
                "status": next_status,
                "data": merged,
            }
        ]
        audits = [
            self._audit_entry(
                entity["id"], actor, "activate", entity["status"], next_status, {"patch": patch}
            )
        ]
        for old in plan["superseded"]:
            old_data = dict(old["data"])
            old_data["superseded_by"] = entity["id"]
            updates.append(
                {
                    "id": old["id"],
                    "expected_version": old["version"],
                    "status": "superseded",
                    "data": old_data,
                }
            )
            audits.append(
                self._audit_entry(
                    old["id"], actor, "supersede", old["status"], "superseded",
                    {"superseded_by": entity["id"]},
                )
            )
        for sample in plan["rebind"]:
            sample_data = dict(sample["data"])
            sample_data["consent_id"] = entity["id"]
            sample_data.pop("suspended_reason", None)
            new_status = "stored" if sample["status"] == "suspended" else sample["status"]
            updates.append(
                {
                    "id": sample["id"],
                    "expected_version": sample["version"],
                    "status": new_status,
                    "data": sample_data,
                }
            )
            audits.append(
                self._audit_entry(
                    sample["id"], actor, "rebind", sample["status"], new_status,
                    {
                        "consent_id": entity["id"],
                        "previous_consent_id": sample["data"].get("consent_id"),
                    },
                )
            )
        for sample in plan["suspend"]:
            sample_data = dict(sample["data"])
            sample_data["consent_id"] = entity["id"]
            sample_data["suspended_reason"] = "purpose not covered by consent scope"
            updates.append(
                {
                    "id": sample["id"],
                    "expected_version": sample["version"],
                    "status": "suspended",
                    "data": sample_data,
                }
            )
            audits.append(
                self._audit_entry(
                    sample["id"], actor, "suspend", sample["status"], "suspended",
                    {
                        "consent_id": entity["id"],
                        "purpose": sample["data"].get("purpose"),
                    },
                )
            )
        self.repository.apply_changes(updates, audits)
        result = self.repository.get_entity(entity["id"])
        result["activation"] = {
            "superseded_consents": [item["id"] for item in plan["superseded"]],
            "rebound_samples": [item["id"] for item in plan["rebind"]],
            "suspended_samples": [item["id"] for item in plan["suspend"]],
        }
        return result

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
