import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class RevisionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")
        self.event_id = self._publish_event()

    def tearDown(self):
        self.tmp.cleanup()

    def _publish_event(self):
        event = self.service.create(
            self.analyst,
            "event",
            {
                "title": "Revision Event",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "Test Region",
                "reports": [
                    {"station": "STA-1", "time_offset": 1, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": -1, "distance_km": 1.4},
                ],
            },
        )
        self.service.transition(self.analyst, event["id"], "associate", {})
        self.service.transition(
            self.reviewer, event["id"], "review", {"reviewer": "reviewer-1", "magnitude": 4.1}
        )
        self.service.transition(
            self.reviewer, event["id"], "publish", {"communication_id": "C-100"}
        )
        return event["id"]

    def _revision_payload(self, **overrides):
        payload = {
            "reason": "late station report",
            "added_reports": [{"station": "STA-3", "time_offset": 0, "distance_km": 1.1}],
            "magnitude": 4.4,
            "magnitude_note": "recalculated median",
        }
        payload.update(overrides)
        return payload

    def test_analyst_submits_pending_draft_under_original_event_number(self):
        revision = self.service.submit_revision(
            self.analyst, self.event_id, self._revision_payload()
        )
        self.assertEqual(revision["kind"], "revision")
        self.assertEqual(revision["event_id"], self.event_id)
        self.assertTrue(revision["id"].startswith(self.event_id + "-R"))
        self.assertEqual(revision["revision_no"], 1)
        self.assertEqual(revision["status"], "pending_review")
        self.assertEqual(revision["created_by"], "analyst-1")

        event = self.service.get(self.event_id)
        self.assertEqual(event["status"], "published")
        self.assertEqual(event["version"], 4)
        self.assertEqual(len(event["data"]["reports"]), 2)
        self.assertEqual(event["data"]["magnitude"], 4.1)

    def test_second_submission_returns_current_draft_number_and_does_not_queue(self):
        first = self.service.submit_revision(
            self.analyst, self.event_id, self._revision_payload()
        )
        with self.assertRaises(ConflictError) as caught:
            self.service.submit_revision(
                self.analyst, self.event_id, self._revision_payload(magnitude=4.5)
            )
        self.assertEqual(caught.exception.details["revision_id"], first["id"])
        self.assertIn(first["id"], str(caught.exception))
        pending = self.service.revisions(self.event_id, "pending_review")
        self.assertEqual(len(pending), 1)

    def test_revision_without_added_report_cannot_enter_review(self):
        with self.assertRaises(ValidationError):
            self.service.submit_revision(
                self.analyst, self.event_id, self._revision_payload(added_reports=[])
            )
        with self.assertRaises(ValidationError):
            self.service.submit_revision(
                self.analyst, self.event_id, self._revision_payload(added_reports=None)
            )

    def test_revision_requires_magnitude_review_result(self):
        with self.assertRaises(ValidationError):
            self.service.submit_revision(
                self.analyst, self.event_id, self._revision_payload(magnitude=None)
            )

    def test_approval_replaces_public_event_and_preserves_old_version_and_handlers(self):
        revision = self.service.submit_revision(
            self.analyst, self.event_id, self._revision_payload()
        )
        approved = self.service.transition(
            self.reviewer,
            revision["id"],
            "approve",
            {"review_result": "updated", "magnitude": 4.5},
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["reviewed_by"], "reviewer-1")

        event = self.service.get(self.event_id)
        self.assertEqual(event["status"], "revised")
        self.assertEqual(event["version"], 5)
        self.assertEqual(len(event["data"]["reports"]), 3)
        self.assertEqual(event["data"]["reports"][-1]["station"], "STA-3")
        self.assertEqual(event["data"]["magnitude"], 4.5)
        self.assertEqual(event["data"]["latest_revision_id"], revision["id"])
        self.assertEqual(event["updated_by"], "reviewer-1")

        history = self.service.history(self.event_id)
        versions = history["versions"]
        self.assertEqual([item["version"] for item in versions], [1, 2, 3, 4, 5])
        old_public_version = versions[3]
        self.assertEqual(old_public_version["status"], "published")
        self.assertEqual(len(old_public_version["data"]["reports"]), 2)
        self.assertEqual(old_public_version["data"]["magnitude"], 4.1)
        self.assertEqual(versions[4]["updated_by"], "reviewer-1")
        self.assertEqual(versions[3]["draft_revisions"][0]["created_by"], "analyst-1")

        actions = [(item["action"], item["actor_id"]) for item in history["audit_log"]]
        self.assertIn(("submit_revision", "analyst-1"), actions)
        self.assertIn(("approve_revision", "reviewer-1"), actions)

    def test_rejection_does_not_replace_public_version_and_allows_new_draft(self):
        first = self.service.submit_revision(
            self.analyst, self.event_id, self._revision_payload()
        )
        rejected = self.service.transition(self.reviewer, first["id"], "reject", {})
        self.assertEqual(rejected["status"], "rejected")

        event = self.service.get(self.event_id)
        self.assertEqual(event["status"], "published")
        self.assertEqual(event["version"], 4)
        self.assertEqual(len(event["data"]["reports"]), 2)

        second = self.service.submit_revision(
            self.analyst, self.event_id, self._revision_payload(magnitude=4.6)
        )
        self.assertEqual(second["revision_no"], 2)
        self.assertEqual(second["status"], "pending_review")

    def test_only_analyst_can_submit_and_only_reviewer_can_decide(self):
        revision = self.service.submit_revision(
            self.analyst, self.event_id, self._revision_payload()
        )
        with self.assertRaises(PermissionDenied):
            self.service.submit_revision(
                self.reviewer, self.event_id, self._revision_payload()
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.analyst, revision["id"], "approve", {})

    def test_unpublished_event_cannot_be_revised(self):
        other = self.service.create(
            self.analyst,
            "event",
            {
                "title": "Candidate Event",
                "origin_time": "2026-01-02T00:00:00Z",
                "location": "Other Region",
                "reports": [
                    {"station": "STA-1", "time_offset": 1, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": -1, "distance_km": 1.2},
                ],
            },
        )
        with self.assertRaises(InvalidTransition):
            self.service.submit_revision(
                self.analyst, other["id"], self._revision_payload()
            )


if __name__ == "__main__":
    unittest.main()
