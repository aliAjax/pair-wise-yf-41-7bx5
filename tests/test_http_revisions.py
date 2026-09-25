import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HttpRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "http.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), "/tmp")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, payload=None, user="analyst-1", role="analyst"):
        data = None
        headers = {"X-User-Id": user, "X-Role": role}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def publish_event(self):
        status, event = self.request(
            "POST",
            "/api/events",
            {
                "title": "HTTP Event",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "Region",
                "reports": [
                    {"station": "STA-1", "time_offset": 1, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": -1, "distance_km": 1.2},
                ],
            },
        )
        self.assertEqual(status, 201)
        event_id = event["id"]
        self.request("POST", "/api/entities/%s/actions" % event_id, {"action": "associate", "data": {}})
        self.request(
            "POST",
            "/api/entities/%s/actions" % event_id,
            {"action": "review", "data": {"reviewer": "reviewer-1", "magnitude": 4.1}},
            user="reviewer-1",
            role="reviewer",
        )
        self.request(
            "POST",
            "/api/entities/%s/actions" % event_id,
            {"action": "publish", "data": {"communication_id": "C-1"}},
            user="reviewer-1",
            role="reviewer",
        )
        return event_id

    def test_revision_http_workflow(self):
        event_id = self.publish_event()
        query = "?" + urlencode({"status": "published"})
        status, payload = self.request("GET", "/api/events" + query)
        self.assertEqual(status, 200)
        self.assertIn(event_id, [item["id"] for item in payload["items"]])

        draft_body = {
            "reason": "late report",
            "added_reports": [{"station": "STA-3", "time_offset": 0, "distance_km": 1.1}],
            "magnitude": 4.4,
            "expected_version": 4,
        }
        status, draft = self.request(
            "POST", "/api/events/%s/revisions" % event_id, draft_body
        )
        self.assertEqual(status, 201)
        self.assertEqual(draft["status"], "pending_review")
        self.assertTrue(draft["id"].startswith(event_id + "-R"))

        status, second = self.request(
            "POST", "/api/events/%s/revisions" % event_id, draft_body
        )
        self.assertEqual(status, 409)
        self.assertEqual(second["details"]["revision_id"], draft["id"])

        status, revisions = self.request(
            "GET", "/api/events/%s/revisions?status=pending_review" % event_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(revisions["items"][0]["id"], draft["id"])

        status, approved = self.request(
            "POST",
            "/api/entities/%s/actions" % draft["id"],
            {"action": "approve", "data": {"magnitude": 4.5, "review_result": "updated"}},
            user="reviewer-1",
            role="reviewer",
        )
        self.assertEqual(status, 200)
        self.assertEqual(approved["status"], "approved")

        status, event = self.request("GET", "/api/entities/%s" % event_id)
        self.assertEqual(event["status"], "revised")
        self.assertEqual(event["version"], 5)
        self.assertEqual(len(event["data"]["reports"]), 3)

        status, history = self.request("GET", "/api/events/%s/history" % event_id)
        self.assertEqual(status, 200)
        self.assertEqual(history["versions"][3]["data"]["magnitude"], 4.1)
        self.assertEqual(history["versions"][4]["data"]["magnitude"], 4.5)
        self.assertEqual(history["versions"][4]["updated_by"], "reviewer-1")

    def test_revision_without_added_report_is_rejected(self):
        event_id = self.publish_event()
        status, payload = self.request(
            "POST",
            "/api/events/%s/revisions" % event_id,
            {"added_reports": [], "magnitude": 4.4},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["type"], "ValidationError")


if __name__ == "__main__":
    unittest.main()
