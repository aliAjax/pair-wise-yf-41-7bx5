import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class RevisionFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _published_event(self):
        event = self.service.create(self.admin, "event", {
            "title": "Event-A",
            "origin_time": "2026-01-01T00:00:00Z",
            "location": "Region-A",
            "reports": [
                {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
            ],
        })
        self.service.transition(self.admin, event["id"], "associate", {})
        self.service.transition(
            self.admin, event["id"], "review", {"reviewer": "R-1", "magnitude": 4.2}
        )
        return self.service.transition(
            self.admin, event["id"], "publish", {"communication_id": "C-1"}
        )

    def _draft(self, event_id, **overrides):
        data = {
            "event_id": event_id,
            "reason": "late station reports",
            "magnitude": 4.5,
            "reports": [{"station": "STA-3", "time_offset": 1, "distance_km": 0.8}],
        }
        data.update(overrides)
        return self.service.create(self.analyst, "revision", data)

    def test_revision_flow_replaces_published_content(self):
        event = self._published_event()
        draft = self._draft(event["id"])
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["data"]["event_id"], event["id"])

        submitted = self.service.transition(self.analyst, draft["id"], "submit", {})
        self.assertEqual(submitted["status"], "pending")
        self.assertEqual(submitted["data"]["submitted_by"], "analyst-1")

        applied = self.service.transition(self.reviewer, draft["id"], "approve", {})
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["data"]["reviewer"], "reviewer-1")

        updated = self.service.get(event["id"])
        self.assertEqual(updated["id"], event["id"])
        self.assertEqual(updated["status"], "revised")
        self.assertEqual(updated["version"], event["version"] + 1)
        self.assertEqual(len(updated["data"]["reports"]), 3)
        self.assertEqual(updated["data"]["magnitude"], 4.5)
        self.assertEqual(updated["data"]["last_revision_id"], draft["id"])

        history = self.service.history(event["id"])
        self.assertEqual(len(history["versions"]), 1)
        old = history["versions"][0]
        self.assertEqual(old["version"], event["version"])
        self.assertEqual(old["status"], "published")
        self.assertEqual(old["data"]["magnitude"], 4.2)
        self.assertEqual(old["revision_id"], draft["id"])

        apply_entries = [
            entry for entry in history["audit"] if entry["action"] == "apply_revision"
        ]
        self.assertEqual(len(apply_entries), 1)
        self.assertEqual(apply_entries[0]["actor_id"], "reviewer-1")
        self.assertEqual(apply_entries[0]["detail"]["analyst"], "analyst-1")
        self.assertEqual(apply_entries[0]["detail"]["reviewer"], "reviewer-1")

    def test_second_draft_blocked_with_current_draft_id(self):
        event = self._published_event()
        draft = self._draft(event["id"])
        with self.assertRaises(ConflictError) as ctx:
            self._draft(event["id"])
        self.assertIn(draft["id"], str(ctx.exception))

    def test_second_submit_blocked_while_pending(self):
        event = self._published_event()
        draft = self._draft(event["id"])
        self.service.transition(self.analyst, draft["id"], "submit", {})
        with self.assertRaises(ConflictError) as ctx:
            self._draft(event["id"])
        self.assertIn(draft["id"], str(ctx.exception))

    def test_new_draft_allowed_after_reject(self):
        event = self._published_event()
        draft = self._draft(event["id"])
        self.service.transition(self.analyst, draft["id"], "submit", {})
        rejected = self.service.transition(
            self.reviewer, draft["id"], "reject", {"reason": "bad amplitude"}
        )
        self.assertEqual(rejected["status"], "rejected")
        second = self._draft(event["id"])
        self.assertEqual(second["status"], "draft")
        unchanged = self.service.get(event["id"])
        self.assertEqual(unchanged["status"], "published")
        self.assertEqual(len(unchanged["data"]["reports"]), 2)

    def test_submit_requires_new_reports(self):
        event = self._published_event()
        draft = self._draft(event["id"], reports=[])
        with self.assertRaises(ValidationError):
            self.service.transition(self.analyst, draft["id"], "submit", {})

    def test_submit_requires_magnitude(self):
        event = self._published_event()
        draft = self._draft(event["id"], magnitude=None)
        with self.assertRaises(ValidationError):
            self.service.transition(self.analyst, draft["id"], "submit", {})

    def test_revision_requires_published_event(self):
        event = self.service.create(self.admin, "event", {
            "title": "Event-B",
            "origin_time": "2026-01-02T00:00:00Z",
            "location": "Region-B",
            "reports": [
                {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
            ],
        })
        with self.assertRaises(ValidationError):
            self._draft(event["id"])

    def test_analyst_cannot_approve(self):
        event = self._published_event()
        draft = self._draft(event["id"])
        self.service.transition(self.analyst, draft["id"], "submit", {})
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.analyst, draft["id"], "approve", {})

    def test_viewer_cannot_create_revision(self):
        event = self._published_event()
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("viewer-1", "viewer"),
                "revision",
                {"event_id": event["id"], "reports": [{"station": "S"}], "magnitude": 4.5},
            )


if __name__ == "__main__":
    unittest.main()
