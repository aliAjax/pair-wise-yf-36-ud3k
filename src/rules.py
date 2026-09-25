from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_participant(actor, data, lookup):
    if len(data.get("name", "")) < 2:
        raise ValidationError("participant name is required")


def _validate_consent(actor, data, lookup):
    participant = _find_one(lookup, "participant", "id", data.get("participant_id"))
    if not participant or participant["status"] == "closed":
        raise ValidationError("consent requires an active participant")
    if not data.get("scope"):
        raise ValidationError("consent scope is required")


def _validate_consent_activate(actor, entity, data, lookup):
    participant_id = entity["data"].get("participant_id")
    withdrawals = lookup("withdrawal", "participant_id", participant_id) if lookup else []
    blockers = [
        item for item in (withdrawals or [])
        if item["status"] in PENDING_WITHDRAWAL_STATUSES
    ]
    if blockers:
        raise ConflictError(
            "participant %s has a pending or approved withdrawal" % participant_id
        )


def _validate_sample_store(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", data.get("consent_id"))
    if not consent or consent["status"] != "active":
        raise ValidationError("storage requires active consent")
    purpose = data.get("purpose")
    if purpose not in consent["data"].get("scope", []):
        raise ValidationError("consent scope does not cover purpose: " + str(purpose))
    return {"stored_at": "2026-09-24T00:00:00Z"}


def _validate_sample_loan(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", entity["data"].get("consent_id"))
    if not consent or consent["status"] != "active":
        raise ValidationError("loan requires active consent")
    purpose = entity["data"].get("purpose")
    if purpose and purpose not in consent["data"].get("scope", []):
        raise ValidationError("consent scope does not cover purpose: " + str(purpose))


def _validate_withdrawal_approve(actor, entity, data, lookup):
    samples = data.get("sample_ids") or []
    if len(set(samples)) != len(samples):
        raise ConflictError("sample_ids contains duplicates")
    for sample_id in samples:
        if not _find_one(lookup, "sample", "id", sample_id):
            raise ValidationError("unknown sample: " + str(sample_id))
    return {"approved_by": actor.user_id}


CUSTOM_CREATE = {'participant': _validate_participant, 'consent': _validate_consent}
CUSTOM_TRANSITIONS = {('consent', 'activate'): _validate_consent_activate, ('sample', 'store'): _validate_sample_store, ('sample', 'loan'): _validate_sample_loan, ('withdrawal', 'approve'): _validate_withdrawal_approve}

# Samples in these statuses are final and never rebound on consent activation.
TERMINAL_SAMPLE_STATUSES = ("anonymized", "destroyed")
# Non-terminal samples still linked to a consent version get rebound or suspended.
REBINDABLE_SAMPLE_STATUSES = ("stored", "on_loan", "suspended")
# Withdrawals in these statuses block activation of a new consent version.
PENDING_WITHDRAWAL_STATUSES = ("requested", "approved")


class RuleEngine:
    ALIASES = {'participants': 'participant', 'consents': 'consent', 'samples': 'sample', 'withdrawals': 'withdrawal'}
    INITIAL_STATUS = {'participant': 'registered', 'consent': 'draft', 'sample': 'collected', 'withdrawal': 'requested'}
    TRANSITIONS = {'participant': {'close_participant': (('registered',), 'closed')}, 'consent': {'activate': (('draft',), 'active'), 'supersede': (('active',), 'superseded'), 'withdraw': (('active',), 'withdrawn')}, 'sample': {'store': (('collected',), 'stored'), 'loan': (('stored',), 'on_loan'), 'return': (('on_loan',), 'stored'), 'anonymize': (('stored',), 'anonymized'), 'destroy': (('stored',), 'destroyed')}, 'withdrawal': {'approve': (('requested',), 'approved'), 'execute': (('approved',), 'executed')}}
    CREATE_REQUIRED = {'participant': ('name',), 'consent': ('participant_id', 'scope'), 'sample': ('participant_id', 'sample_code', 'collected_at'), 'withdrawal': ('participant_id', 'requested_at')}
    ACTION_REQUIRED = {('consent', 'activate'): ('scope', 'version', 'expires_at'), ('consent', 'supersede'): ('reason',), ('consent', 'withdraw'): ('reason',), ('sample', 'store'): ('freezer', 'position', 'consent_id', 'purpose'), ('sample', 'loan'): ('recipient', 'purpose', 'due_at'), ('sample', 'anonymize'): ('reason',), ('sample', 'destroy'): ('reason',), ('withdrawal', 'approve'): ('reason', 'sample_ids'), ('withdrawal', 'execute'): ('executed_at',)}
    CREATE_ROLES = {'participant': ('admin', 'biobank'), 'consent': ('admin', 'committee'), 'sample': ('admin', 'biobank'), 'withdrawal': ('admin', 'biobank')}
    ROLE_ACTIONS = {'close_participant': ('admin', 'biobank'), 'activate': ('admin', 'committee'), 'supersede': ('admin', 'committee'), 'withdraw': ('admin', 'committee'), 'store': ('admin', 'biobank'), 'loan': ('admin', 'biobank'), 'return': ('admin', 'biobank'), 'anonymize': ('admin', 'biobank'), 'destroy': ('admin', 'biobank'), 'approve': ('admin', 'committee'), 'execute': ('admin', 'biobank')}

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
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

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

    @staticmethod
    def plan_consent_activation(consent, activate_data, lookup):
        """Compute the side effects of activating a draft consent version.

        Pure rule calculation with no storage side effects: the old active
        versions of the same participant are superseded, and their non-terminal
        samples are either rebound to the new version (purpose still covered by
        the new scope) or suspended (purpose no longer covered). The service
        layer applies the returned plan in a single transaction.
        """
        participant_id = consent["data"].get("participant_id")
        new_scope = activate_data.get("scope") or []
        superseded = [
            item
            for item in (lookup("consent", "participant_id", participant_id) or [])
            if item["id"] != consent["id"] and item["status"] == "active"
        ]
        old_ids = {item["id"] for item in superseded}
        rebind = []
        suspend = []
        if old_ids:
            samples = lookup("sample", "participant_id", participant_id) or []
            for sample in samples:
                if sample["status"] not in REBINDABLE_SAMPLE_STATUSES:
                    continue
                if sample["data"].get("consent_id") not in old_ids:
                    continue
                if sample["data"].get("purpose") in new_scope:
                    rebind.append(sample)
                else:
                    suspend.append(sample)
        return {"superseded": superseded, "rebind": rebind, "suspend": suspend}


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
