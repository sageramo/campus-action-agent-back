"""Real HTTP checks on an ephemeral local test server; no browser/model calls."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest

from campus_agent.conflict import detect_conflicts
from campus_agent.extractor import parse_notice_file
from campus_agent.server import CampusAgentHandler, MAX_BODY_BYTES

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(CampusAgentHandler):
    def log_message(self, *args):
        pass


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, payload=None, raw=None, headers=None):
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                result = json.loads(result)
            return response.status, result
        finally:
            connection.close()

    def test_health_exposes_new_version(self):
        status, result = self.request("GET", "/api/health")
        self.assertEqual((status, result["version"]), (200, "1.24"))
        self.assertIn('model_notice_candidates_v1', result['capabilities'])
        self.assertEqual(set(result['model_extraction']), {'configured', 'enabled'})

    def test_parse_forwards_reference_date_and_interval_metadata(self):
        status, result = self.request("POST", "/api/parse", {"text": "明天下午3点-5点开会", "reference_date": "2026-10-24", "timezone": "Asia/Shanghai"})
        self.assertEqual(status, 200)
        self.assertEqual(result["time_points"][0]["end_value"], "2026-10-25 17:00")
        self.assertEqual(result["parsing_status"], "needs_confirmation")

    def test_invalid_notice_payloads_return_400(self):
        for payload in [[], None, {"text": None}, {"text": 12}, {"text": ""}, {"text": "明天", "reference_date": "2026-02-30"}, {"text": "x", "timezone": "UTC"}]:
            with self.subTest(payload=payload):
                raw = b"null" if payload is None else None
                status, result = self.request("POST", "/api/parse", payload, raw=raw)
                self.assertEqual(status, 400)
                self.assertIn("error", result)

    def test_invalid_task_semantics_return_400(self):
        for payload in [
            {"tasks": "bad"}, {"tasks": [None]}, {"tasks": [{"name": "x", "must_do": "false"}]},
            {"tasks": [{"name": "x", "status": []}]}, {"tasks": [{"name": "x", "deadline": "2026-10-25"}]},
            {"tasks": [], "now": "bad"}, {"tasks": [], "now": None}, {"tasks": [], "availability": None},
            {"tasks": [], "availability": [{"start": "2026-10-26 12:00", "end": "2026-10-26 09:00"}]},
        ]:
            with self.subTest(payload=payload):
                status, result = self.request("POST", "/api/conflicts", payload)
                self.assertEqual(status, 400)
                self.assertIn("error", result)

    def test_invalid_json_and_encoding_do_not_crash_handler(self):
        for raw in [b"{", b"\xff", b'{"tasks":[{"name":"x","estimated_hours":NaN}]}', b'{"x":Infinity}', b"[" * 1200 + b"0" + b"]" * 1200]:
            with self.subTest(raw=raw[:40]):
                status, result = self.request("POST", "/api/conflicts", raw=raw)
                self.assertEqual(status, 400)
                self.assertIn("error", result)

    def test_oversized_and_bad_length_requests_are_rejected(self):
        status, _ = self.request("POST", "/api/parse", raw=b"", headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)
        status, _ = self.request("POST", "/api/parse", raw=b"", headers={"Content-Length": "bad"})
        self.assertEqual(status, 400)

    def test_conflict_endpoint_uses_supplied_availability(self):
        payload = {"now": "2026-10-26 09:00", "tasks": [{"name": "报告", "remaining_minutes": 180, "deadline": "2026-10-26 12:00"}, {"name": "课程", "fixed_time": {"start": "2026-10-26 10:00", "end": "2026-10-26 11:00"}}], "availability": [{"start": "2026-10-26 09:00", "end": "2026-10-26 12:00"}]}
        status, result = self.request("POST", "/api/conflicts", payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 120)
        self.assertEqual(result["capacity_checks"][0]["result"], "shortage")

    def test_bad_query_and_unknown_case_have_explicit_errors(self):
        self.assertEqual(self.request("GET", "/api/parsed-samples?limit=bad")[0], 400)
        self.assertEqual(self.request("GET", "/api/analyze-case?id=does-not-exist")[0], 404)
        self.assertEqual(self.request("POST", "/does-not-exist", {})[0], 404)

    def test_historical_demo_reference_has_explicit_assumption(self):
        status, result = self.request("GET", "/api/analyze-case?id=CF-003")
        self.assertEqual(status, 200)
        self.assertTrue(any("历史演示" in warning for warning in result["analysis"]["warnings"]))
        self.assertEqual(sum(item["type"] == "hard_time_overlap" for item in result["analysis"]["conflicts"]), 2)

    def test_static_legacy_mapping_points_to_existing_deploy_folder(self):
        status, content = self.request("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertIn("司序".encode(), content)

    def test_original_constructed_cases_can_be_replayed_without_crashing(self):
        data = json.loads((ROOT / "data/冲突案例/冲突案例_演示数据.json").read_text())
        for case in data["cases"]:
            with self.subTest(case=case["case_id"]):
                status, result = self.request("GET", "/api/analyze-case?id=" + case["case_id"])
                self.assertEqual(status, 200)
                self.assertIn(result["analysis"]["status"], {"conflicts_detected", "information_needed", "no_conflict_detected"})

    def test_original_document_parsing_does_not_raise(self):
        docs = [path for path in (ROOT / "data").rglob("*") if path.suffix.lower() in {".txt", ".pdf", ".docx"}]
        self.assertGreater(len(docs), 0)
        for path in docs:
            with self.subTest(path=path.name):
                result = parse_notice_file(path).to_dict()
                self.assertEqual(result["parsing_status"], "needs_confirmation")


if __name__ == "__main__":
    unittest.main()
