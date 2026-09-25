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
        with self.repository.transaction() as connection:
            entity = self.repository.create_entity(
                entity_id, kind, status, payload, actor.user_id, connection=connection
            )
            self.audit.record(
                entity_id, actor, "create", None, status, {"kind": kind},
                connection=connection,
            )
            if idempotency_key:
                self.repository.save_idempotency(
                    actor.user_id, idempotency_key, entity_id, connection=connection
                )
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "consent" and action == "activate":
            return self._activate_consent(actor, entity, dict(data or {}), expected)
        with self.repository.transaction() as connection:
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), self._lookup
            )
            merged = dict(entity["data"])
            merged.update(patch)
            updated = self.repository.update_entity(
                entity_id, expected, next_status, merged, connection=connection
            )
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch},
                connection=connection,
            )
        return updated

    def _activate_consent(self, actor, entity, payload, expected):
        """Activate a draft consent: supersede the participant's old active
        versions, rebind their open samples, suspend samples whose registered
        purpose is not covered by the new scope, and audit all of it in one
        transaction."""
        with self.repository.transaction() as connection:
            next_status, patch = self.rules.validate_transition(
                actor, entity, "activate", payload, self._lookup
            )
            merged = dict(entity["data"])
            merged.update(patch)
            participant_id = merged.get("participant_id")
            new_scope = merged.get("scope") or []

            updated = self.repository.update_entity(
                entity["id"], expected, next_status, merged, connection=connection
            )

            superseded = []
            if participant_id:
                for other in self.repository.find_entities(
                    "consent", "participant_id", participant_id
                ):
                    if other["id"] == entity["id"] or other["status"] != "active":
                        continue
                    self.repository.update_entity(
                        other["id"], other["version"], "superseded", other["data"],
                        connection=connection,
                    )
                    self.audit.record(
                        other["id"], actor, "supersede", "active", "superseded",
                        {"replaced_by": entity["id"]}, connection=connection,
                    )
                    superseded.append(other["id"])

            rebound = []
            suspended = []
            if superseded:
                rebound, suspended = self._rebind_samples(
                    actor, participant_id, entity["id"], superseded, new_scope, connection
                )

            activation = {
                "superseded_consent_ids": superseded,
                "rebound_sample_ids": rebound,
                "suspended_samples": suspended,
            }
            self.audit.record(
                entity["id"], actor, "activate", entity["status"], next_status,
                dict(activation, patch=patch), connection=connection,
            )
        result = dict(updated)
        result["activation"] = activation
        return result

    def _rebind_samples(self, actor, participant_id, consent_id, superseded, new_scope, connection):
        rebound = []
        suspended = []
        for sample in self.repository.find_entities(
            "sample", "participant_id", participant_id
        ):
            if sample["status"] in self.rules.TERMINAL_SAMPLE_STATUS:
                continue
            old_consent = sample["data"].get("consent_id")
            if old_consent not in superseded:
                continue
            purpose = sample["data"].get("use")
            if purpose is not None and purpose in new_scope:
                sample_data = dict(sample["data"])
                sample_data["consent_id"] = consent_id
                self.repository.update_entity(
                    sample["id"], sample["version"], sample["status"], sample_data,
                    connection=connection,
                )
                self.audit.record(
                    sample["id"], actor, "rebind", sample["status"], sample["status"],
                    {"from_consent": old_consent, "to_consent": consent_id, "purpose": purpose},
                    connection=connection,
                )
                rebound.append(sample["id"])
            else:
                self.repository.update_entity(
                    sample["id"], sample["version"], "suspended", sample["data"],
                    connection=connection,
                )
                self.audit.record(
                    sample["id"], actor, "suspend", sample["status"], "suspended",
                    {"purpose": purpose, "new_scope": new_scope, "consent_id": old_consent},
                    connection=connection,
                )
                suspended.append({"id": sample["id"], "purpose": purpose})
        return rebound, suspended

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
