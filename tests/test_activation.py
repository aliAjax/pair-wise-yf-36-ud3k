import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _participant(self):
        return self.service.create(self.actor, "participant", {"name": "Participant One"})

    def _consent(self, participant_id, scope):
        return self.service.create(
            self.actor, "consent", {"participant_id": participant_id, "scope": scope}
        )

    def _activate(self, consent_id, scope, expected_version=None):
        return self.service.transition(
            self.actor,
            consent_id,
            "activate",
            {"scope": scope, "version": "v-next", "expires_at": "2099-01-01"},
            expected_version=expected_version,
        )

    def _stored_sample(self, participant_id, consent_id, code, purpose):
        sample = self.service.create(
            self.actor,
            "sample",
            {"participant_id": participant_id, "sample_code": code, "collected_at": "2026-01-01"},
        )
        return self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent_id, "purpose": purpose},
        )

    def test_activation_supersedes_old_version_and_rebinds_open_samples(self):
        participant = self._participant()
        old = self._consent(participant["id"], ["research"])
        self._activate(old["id"], ["research"])
        stored = self._stored_sample(participant["id"], old["id"], "B-001", "research")
        on_loan = self._stored_sample(participant["id"], old["id"], "B-002", "research")
        self.service.transition(
            self.actor, on_loan["id"], "loan",
            {"recipient": "Lab", "purpose": "analysis", "due_at": "2026-12-01"},
        )
        destroyed = self._stored_sample(participant["id"], old["id"], "B-003", "research")
        self.service.transition(self.actor, destroyed["id"], "destroy", {"reason": "contaminated"})

        new = self._consent(participant["id"], ["research"])
        result = self._activate(new["id"], ["research"])

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["activation"]["superseded_consent_ids"], [old["id"]])
        self.assertEqual(result["activation"]["suspended_samples"], [])
        self.assertEqual(
            sorted(result["activation"]["rebound_sample_ids"]),
            sorted([stored["id"], on_loan["id"]]),
        )
        self.assertEqual(self.service.get(old["id"])["status"], "superseded")
        for sample_id, status in ((stored["id"], "stored"), (on_loan["id"], "on_loan")):
            sample = self.service.get(sample_id)
            self.assertEqual(sample["status"], status)
            self.assertEqual(sample["data"]["consent_id"], new["id"])
        terminal = self.service.get(destroyed["id"])
        self.assertEqual(terminal["status"], "destroyed")
        self.assertEqual(terminal["data"]["consent_id"], old["id"])

    def test_activation_suspends_samples_with_uncovered_purpose(self):
        participant = self._participant()
        old = self._consent(participant["id"], ["research", "diagnostics"])
        self._activate(old["id"], ["research", "diagnostics"])
        covered = self._stored_sample(participant["id"], old["id"], "B-001", "research")
        uncovered = self._stored_sample(participant["id"], old["id"], "B-002", "diagnostics")

        new = self._consent(participant["id"], ["research"])
        result = self._activate(new["id"], ["research"])

        self.assertEqual(result["activation"]["rebound_sample_ids"], [covered["id"]])
        self.assertEqual(
            result["activation"]["suspended_samples"],
            [{"id": uncovered["id"], "purpose": "diagnostics"}],
        )
        sample = self.service.get(uncovered["id"])
        self.assertEqual(sample["status"], "suspended")
        self.assertEqual(sample["data"]["consent_id"], old["id"])
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.actor, uncovered["id"], "loan",
                {"recipient": "Lab", "purpose": "analysis", "due_at": "2026-12-01"},
            )

    def test_activation_blocked_by_pending_withdrawal(self):
        for withdrawal_status in ("requested", "approved"):
            participant = self._participant()
            old = self._consent(participant["id"], ["research"])
            self._activate(old["id"], ["research"])
            withdrawal = self.service.create(
                self.actor, "withdrawal",
                {"participant_id": participant["id"], "requested_at": "2026-03-01"},
            )
            if withdrawal_status == "approved":
                sample = self._stored_sample(participant["id"], old["id"], "B-001", "research")
                self.service.transition(
                    self.actor, withdrawal["id"], "approve",
                    {"reason": "participant request", "sample_ids": [sample["id"]]},
                )
            new = self._consent(participant["id"], ["research"])
            with self.assertRaises(ConflictError):
                self._activate(new["id"], ["research"])
            self.assertEqual(self.service.get(new["id"])["status"], "draft")
            self.assertEqual(self.service.get(old["id"])["status"], "active")

    def test_activation_failure_rolls_back_everything(self):
        participant = self._participant()
        old = self._consent(participant["id"], ["research"])
        self._activate(old["id"], ["research"])
        sample = self._stored_sample(participant["id"], old["id"], "B-001", "research")
        new = self._consent(participant["id"], ["research"])
        audit_before = len(self.service.audit_log())

        with self.assertRaises(ConflictError):
            self._activate(new["id"], ["research"], expected_version=999)

        self.assertEqual(self.service.get(old["id"])["status"], "active")
        self.assertEqual(self.service.get(new["id"])["status"], "draft")
        rebound = self.service.get(sample["id"])
        self.assertEqual(rebound["status"], "stored")
        self.assertEqual(rebound["data"]["consent_id"], old["id"])
        self.assertEqual(len(self.service.audit_log()), audit_before)

    def test_store_requires_purpose_covered_by_consent_scope(self):
        participant = self._participant()
        consent = self._consent(participant["id"], ["research"])
        self._activate(consent["id"], ["research"])
        sample = self.service.create(
            self.actor, "sample",
            {"participant_id": participant["id"], "sample_code": "B-001", "collected_at": "2026-01-01"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, sample["id"], "store",
                {"freezer": "F1", "position": "A1", "consent_id": consent["id"]},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, sample["id"], "store",
                {"freezer": "F1", "position": "A1", "consent_id": consent["id"], "purpose": "diagnostics"},
            )
        stored = self.service.transition(
            self.actor, sample["id"], "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"], "purpose": "research"},
        )
        self.assertEqual(stored["data"]["use"], "research")


if __name__ == "__main__":
    unittest.main()
