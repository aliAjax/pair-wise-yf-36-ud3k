import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ConsentActivationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.participant = self.service.create(
            self.actor, "participant", {"name": "Participant One"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _activate(self, scope, version="v1", **kwargs):
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": self.participant["id"], "scope": scope},
        )
        return self.service.transition(
            self.actor,
            consent["id"],
            "activate",
            {"scope": scope, "version": version, "expires_at": "2099-01-01"},
            **kwargs,
        )

    def _store_sample(self, code, consent_id, purpose):
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": self.participant["id"],
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )
        return self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {
                "freezer": "F1",
                "position": "A1",
                "consent_id": consent_id,
                "purpose": purpose,
            },
        )

    def test_store_registers_purpose_and_checks_scope(self):
        consent = self._activate(["research"])
        sample = self._store_sample("B-001", consent["id"], "research")
        self.assertEqual(sample["data"]["purpose"], "research")
        other = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": self.participant["id"],
                "sample_code": "B-002",
                "collected_at": "2026-01-01",
            },
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                other["id"],
                "store",
                {
                    "freezer": "F1",
                    "position": "A2",
                    "consent_id": consent["id"],
                    "purpose": "teaching",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                other["id"],
                "store",
                {"freezer": "F1", "position": "A2", "consent_id": consent["id"]},
            )

    def test_activation_supersedes_old_version_and_rebinds_samples(self):
        v1 = self._activate(["research", "teaching"])
        s1 = self._store_sample("B-001", v1["id"], "research")
        s2 = self._store_sample("B-002", v1["id"], "teaching")
        v2 = self._activate(["research"], version="v2")

        old = self.service.get(v1["id"])
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["data"]["superseded_by"], v2["id"])

        covered = self.service.get(s1["id"])
        self.assertEqual(covered["status"], "stored")
        self.assertEqual(covered["data"]["consent_id"], v2["id"])

        uncovered = self.service.get(s2["id"])
        self.assertEqual(uncovered["status"], "suspended")
        self.assertEqual(uncovered["data"]["consent_id"], v2["id"])

        self.assertEqual(v2["activation"]["superseded_consents"], [v1["id"]])
        self.assertEqual(v2["activation"]["rebound_samples"], [s1["id"]])
        self.assertEqual(v2["activation"]["suspended_samples"], [s2["id"]])

        # The suspended sample can no longer be loaned out.
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.actor,
                s2["id"],
                "loan",
                {"recipient": "Lab", "purpose": "teaching", "due_at": "2026-10-01"},
            )
        # The rebound sample still can.
        loaned = self.service.transition(
            self.actor,
            s1["id"],
            "loan",
            {"recipient": "Lab", "purpose": "research", "due_at": "2026-10-01"},
        )
        self.assertEqual(loaned["status"], "on_loan")

    def test_suspended_sample_resumes_when_scope_covers_it_again(self):
        v1 = self._activate(["research", "teaching"])
        sample = self._store_sample("B-001", v1["id"], "teaching")
        v2 = self._activate(["research"], version="v2")
        self.assertEqual(self.service.get(sample["id"])["status"], "suspended")
        v3 = self._activate(["research", "teaching"], version="v3")
        resumed = self.service.get(sample["id"])
        self.assertEqual(resumed["status"], "stored")
        self.assertEqual(resumed["data"]["consent_id"], v3["id"])
        self.assertNotIn("suspended_reason", resumed["data"])
        self.assertEqual(v3["activation"]["rebound_samples"], [sample["id"]])

    def test_pending_or_approved_withdrawal_blocks_activation(self):
        v1 = self._activate(["research"])
        sample = self._store_sample("B-001", v1["id"], "research")
        withdrawal = self.service.create(
            self.actor,
            "withdrawal",
            {"participant_id": self.participant["id"], "requested_at": "2026-03-01"},
        )
        with self.assertRaises(ConflictError):
            self._activate(["research"], version="v2")
        self.service.transition(
            self.actor,
            withdrawal["id"],
            "approve",
            {"reason": "participant request", "sample_ids": [sample["id"]]},
        )
        with self.assertRaises(ConflictError):
            self._activate(["research"], version="v2")
        # The failed activations left nothing behind.
        self.assertEqual(self.service.get(v1["id"])["status"], "active")
        drafts = [
            item
            for item in self.service.list("consent")
            if item["status"] == "draft"
        ]
        self.assertEqual(len(drafts), 2)

    def test_activation_is_atomic_on_version_conflict(self):
        v1 = self._activate(["research"])
        sample = self._store_sample("B-001", v1["id"], "research")
        draft = self.service.create(
            self.actor,
            "consent",
            {"participant_id": self.participant["id"], "scope": ["research"]},
        )
        audit_before = len(self.service.audit_log())
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.actor,
                draft["id"],
                "activate",
                {"scope": ["research"], "version": "v2", "expires_at": "2099-01-01"},
                expected_version=999,
            )
        self.assertEqual(self.service.get(draft["id"])["status"], "draft")
        self.assertEqual(self.service.get(v1["id"])["status"], "active")
        rebound = self.service.get(sample["id"])
        self.assertEqual(rebound["status"], "stored")
        self.assertEqual(rebound["data"]["consent_id"], v1["id"])
        self.assertEqual(len(self.service.audit_log()), audit_before)

    def test_apply_changes_rolls_back_partial_failure(self):
        v1 = self._activate(["research"])
        sample = self._store_sample("B-001", v1["id"], "research")
        audit_before = len(self.service.audit_log())
        updates = [
            {
                "id": v1["id"],
                "expected_version": v1["version"],
                "status": "superseded",
                "data": v1["data"],
            },
            {
                "id": sample["id"],
                "expected_version": 999,
                "status": "suspended",
                "data": sample["data"],
            },
        ]
        audits = [
            {
                "entity_id": v1["id"],
                "actor_id": "admin",
                "actor_role": "admin",
                "action": "supersede",
                "from_status": "active",
                "to_status": "superseded",
                "detail": {},
            }
        ]
        with self.assertRaises(ConflictError):
            self.repo.apply_changes(updates, audits)
        self.assertEqual(self.service.get(v1["id"])["status"], "active")
        self.assertEqual(self.service.get(sample["id"])["status"], "stored")
        self.assertEqual(len(self.service.audit_log()), audit_before)

    def test_loan_requires_active_consent(self):
        v1 = self._activate(["research"])
        sample = self._store_sample("B-001", v1["id"], "research")
        self.service.transition(
            self.actor, v1["id"], "withdraw", {"reason": "participant request"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                sample["id"],
                "loan",
                {"recipient": "Lab", "purpose": "research", "due_at": "2026-10-01"},
            )


if __name__ == "__main__":
    unittest.main()
