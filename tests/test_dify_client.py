"""Real stdlib HTTP transport against a fake loopback upstream, never Dify.

Timeout branches are injected, not exercised with expensive/sleeping requests.
Sentinel values deliberately occur in upstream errors to detect disclosure.
"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import patch
import urllib.error

from campus_agent.dify_client import MAX_RESPONSE_BYTES, ExtractionServiceError, extract_notice, extraction_service_status


KEY = "app-DO-NOT-DISCLOSE-SECRET-SENTINEL"
TEXT = "2026年10月30日17:00前提交报告。"
PAYLOAD = {"text": TEXT, "reference_date": "2026-10-25", "timezone": "Asia/Shanghai"}
RAW_EXTRACTION = {"schema_version": 1, "candidates": [{"title": "提交报告", "kind": "flexible",
                  "action_quote": "提交报告", "time_quote": "2026年10月30日17:00", "location_quote": None,
                  "materials_quotes": [], "uncertainties": []}], "warnings": []}


def wire_response(extraction=RAW_EXTRACTION, status="succeeded"):
    return json.dumps({"data": {"status": status, "outputs": {"extraction": json.dumps(extraction, ensure_ascii=False)}}}, ensure_ascii=False).encode("utf-8")


class FakeDifyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request_body = self.rfile.read(length)
        self.server.requests.append({"path": self.path, "headers": dict(self.headers), "body": request_body})
        status, headers, response_body = self.server.response
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(response_body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class DifyClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeDifyHandler)
        cls.upstream.daemon_threads = True
        cls.thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.upstream.requests = []
        self.upstream.response = (200, {"Content-Type": "application/json"}, wire_response())
        self.base = f"http://127.0.0.1:{self.upstream.server_port}/v1"
        self.env = patch.dict(os.environ, {"DIFY_API_BASE": self.base, "DIFY_API_KEY": KEY,
                              "DIFY_TIMEOUT_SECONDS": "60", "SIXU_MODEL_EXTRACTION_ENABLED": "true"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def assert_safe_failure(self, code, status=502, payload=PAYLOAD):
        with self.assertRaises(ExtractionServiceError) as caught:
            extract_notice(payload)
        error = caught.exception
        self.assertEqual((error.code, error.status), (code, status))
        self.assertEqual(error.message, str(error))
        public = json.dumps({"error": str(error), "code": error.code}, ensure_ascii=False)
        self.assertNotIn(KEY, public)
        self.assertNotIn(TEXT, public)
        self.assertNotIn(self.base, public)
        self.assertNotIn("TRACE-SENTINEL", public)
        return error

    def test_default_disabled_does_not_contact_upstream(self):
        del os.environ["SIXU_MODEL_EXTRACTION_ENABLED"]
        self.assertEqual(extraction_service_status(), {"configured": False, "enabled": False})
        self.assert_safe_failure("model_disabled", 503)
        self.assertEqual(self.upstream.requests, [])

    def test_enable_switch_requires_explicit_true(self):
        for enabled in ("1", "yes", "false", "", "tru"):
            with self.subTest(enabled=enabled):
                os.environ["SIXU_MODEL_EXTRACTION_ENABLED"] = enabled
                self.assertEqual(extraction_service_status(), {"configured": False, "enabled": False})
        os.environ["SIXU_MODEL_EXTRACTION_ENABLED"] = "TRUE"
        self.assertEqual(extraction_service_status(), {"configured": True, "enabled": True})

    def test_enabled_without_key_reports_unconfigured_not_secret(self):
        del os.environ["DIFY_API_KEY"]
        self.assertEqual(extraction_service_status(), {"configured": False, "enabled": True})
        self.assert_safe_failure("model_not_configured", 503)

    def test_health_configuration_is_fixed_booleans_not_a_connectivity_claim(self):
        self.assertEqual(extraction_service_status(), {"configured": True, "enabled": True})
        self.assertEqual(self.upstream.requests, [])

    def test_workflow_request_contains_notice_context_only_and_fixed_anonymous_user(self):
        result = extract_notice(PAYLOAD)
        self.assertEqual((result["provider"], result["parsing_status"]), ("dify", "needs_confirmation"))
        self.assertEqual(result["candidates"][0]["deadline_at"], "2026-10-30T17:00:00+08:00")
        self.assertNotIn("tasks", result)
        request = self.upstream.requests[0]
        self.assertEqual(request["path"], "/v1/workflows/run")
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(json.loads(request["body"]), {"inputs": {"notice_text": TEXT, "reference_date": "2026-10-25", "timezone": "Asia/Shanghai"},
                         "response_mode": "blocking", "user": "sixu-local-workbench"})

    def test_missing_reference_is_sent_as_empty_string_not_wall_clock_date(self):
        extract_notice({"text": TEXT})
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["inputs"]["reference_date"], "")

    def test_reasoning_tag_prefix_is_rejected_without_salvaging_json(self):
        # Even valid JSON after a reasoning prefix is not a valid extraction.
        extraction = '<think>sample reasoning</think>' + json.dumps(RAW_EXTRACTION)
        self.upstream.response = (200, {}, json.dumps({'data': {'status': 'succeeded', 'outputs': {'extraction': extraction}}}).encode())
        self.assert_safe_failure('model_invalid_extraction')

    def test_user_first_trial_answer_passes_once_separated_from_reasoning(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/phase4-first-model-answer.json').read_text())
        self.upstream.response = (200, {}, wire_response(fixture['answer']))
        result = extract_notice(fixture['payload'])
        report, lecture = result['candidates']
        self.assertEqual(report['deadline_at'], '2026-10-28T15:00:00+08:00')
        self.assertEqual(report['materials'], ['课程报告'])
        self.assertIsNone(report['location'])
        self.assertEqual(lecture['fixed_start_at'], '2026-10-28T18:00:00+08:00')
        self.assertEqual(lecture['fixed_end_at'], '2026-10-28T20:00:00+08:00')
        self.assertEqual(lecture['location'], '主楼405')
        self.assertEqual(lecture['materials'], [])
        self.assertIn('原文仅说明讲座举行，未明确要求参加。', lecture['uncertainties'])
        self.assertEqual(result['parsing_status'], 'needs_confirmation')

    def test_success_envelope_cannot_echo_the_configured_key(self):
        raw = deepcopy(RAW_EXTRACTION)
        raw['warnings'] = [KEY]
        self.upstream.response = (200, {}, wire_response(raw))
        self.assert_safe_failure('model_protected_content')

    def test_unicode_escaped_key_in_extraction_is_also_rejected(self):
        extraction = json.dumps({**RAW_EXTRACTION, 'warnings': [KEY]})
        extraction = extraction.replace(KEY, ''.join('\\u%04x' % ord(char) for char in KEY))
        body = json.dumps({'data': {'status': 'succeeded', 'outputs': {'extraction': extraction}}}).encode()
        self.upstream.response = (200, {}, body)
        self.assert_safe_failure('model_protected_content')

    def test_config_url_cannot_contain_credentials_query_fragment_controls_or_bad_ports(self):
        for base in ("ftp://api.example/v1", "https://user:" + KEY + "@api.example/v1", "https://api.example/v1?key=" + KEY,
                     "https://api.example/v1#" + KEY, "https://api.example/v1?", "https://api.example/v1#", "https://api.example:99999/v1",
                     "https://api.example:0/v1", "https://api.example:not-a-port/v1", "https://api.example/\x7f", "https://api.example/\nfoo", "https://api.example\\@other/v1",
                     "https:///v1", "https://中文.example/v1"):
            with self.subTest(base_index=len(base)):
                os.environ["DIFY_API_BASE"] = base
                self.assert_safe_failure("model_not_configured", 503)
        self.assertEqual(self.upstream.requests, [])

    def test_invalid_key_cannot_inject_http_headers(self):
        for key in ("", "app key", "app-key\r\nX-Evil: 1", "中文", "x" * 1025):
            with self.subTest(length=len(key)):
                os.environ["DIFY_API_KEY"] = key
                self.assert_safe_failure("model_not_configured", 503)
        self.assertEqual(self.upstream.requests, [])

    def test_timeout_configuration_is_bounded_finite(self):
        for timeout in ("4.99", "90.1", "nan", "inf", "oops", "0", "-1"):
            with self.subTest(timeout=timeout):
                os.environ["DIFY_TIMEOUT_SECONDS"] = timeout
                self.assert_safe_failure("model_not_configured", 503)
        self.assertEqual(self.upstream.requests, [])

    def test_default_timeout_is_60_and_never_retries(self):
        del os.environ["DIFY_TIMEOUT_SECONDS"]
        with patch("campus_agent.dify_client.urllib.request.OpenerDirector.open", side_effect=socket.timeout(KEY)) as opened:
            self.assert_safe_failure("model_timeout", 504)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.kwargs["timeout"], 60)

    def test_timeout_exception_paths_have_safe_messages(self):
        for error in (TimeoutError(KEY), urllib.error.URLError(socket.timeout(KEY))):
            with self.subTest(error_type=type(error).__name__):
                with patch("campus_agent.dify_client.urllib.request.OpenerDirector.open", side_effect=error):
                    self.assert_safe_failure("model_timeout", 504)

    def test_network_error_does_not_echo_url_key_or_trace(self):
        with patch("campus_agent.dify_client.urllib.request.OpenerDirector.open", side_effect=urllib.error.URLError(KEY + TEXT + self.base + "TRACE-SENTINEL")):
            self.assert_safe_failure("model_connection_error")

    def test_upstream_503_does_not_echo_error_and_is_not_retried(self):
        self.upstream.response = (503, {"Content-Type": "application/json"}, (KEY + TEXT + "TRACE-SENTINEL").encode("utf-8"))
        self.assert_safe_failure("model_upstream_error")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_auth_and_rate_limit_failures_are_explicit_and_safe(self):
        for status, code, public_status in ((401, "model_auth_rejected", 502), (403, "model_auth_rejected", 502), (429, "model_upstream_rate_limited", 503)):
            with self.subTest(status=status):
                self.upstream.response = (status, {}, (KEY + TEXT + "TRACE-SENTINEL").encode("utf-8"))
                self.assert_safe_failure(code, public_status)

    def test_redirect_never_forwards_authorization_or_follows_location(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.upstream.requests.clear()
                self.upstream.response = (status, {"Location": self.base + "/should-not-be-called?" + KEY}, KEY.encode("utf-8"))
                self.assert_safe_failure("model_redirect_blocked")
                self.assertEqual(len(self.upstream.requests), 1)
                self.assertEqual(self.upstream.requests[0]["path"], "/v1/workflows/run")

    def test_oversized_declared_response_is_rejected_without_adoption(self):
        self.upstream.response = (200, {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b"")
        self.assert_safe_failure("model_response_too_large")

    def test_oversized_response_without_content_length_is_bounded(self):
        self.upstream.response = (200, {}, b"x" * (MAX_RESPONSE_BYTES + 1))
        self.assert_safe_failure("model_response_too_large")

    def test_malformed_outer_json_duplicate_keys_nonfinite_and_invalid_utf8_are_rejected(self):
        for body in (b"not JSON " + KEY.encode(), b"[]", b'{"data":{},"data":{}}', b'{"data":{"x":NaN}}',
                     b'{"data":{"x":Infinity}}', b'{"data":{"x":1e999}}', b'\xff', b"[" * 1200 + b"0" + b"]" * 1200):
            with self.subTest(size=len(body)):
                self.upstream.response = (200, {}, body)
                self.assert_safe_failure("model_invalid_response")

    def test_data_status_must_be_succeeded_even_if_output_seems_valid(self):
        for status in ("failed", "running", "stopped", "paused", None):
            with self.subTest(status=status):
                self.upstream.response = (200, {}, wire_response(status=status))
                self.assert_safe_failure("model_workflow_failed")

    def test_only_extraction_string_is_accepted_and_no_fence_or_prose_unwrapping(self):
        for extraction in (RAW_EXTRACTION, None, "```json\n" + json.dumps(RAW_EXTRACTION) + "\n```", "Here you go: " + json.dumps(RAW_EXTRACTION),
                           "[]", '{"schema_version":1,"schema_version":1}', '{"warnings":[NaN]}'):
            with self.subTest(output_type=type(extraction).__name__):
                body = json.dumps({"data": {"status": "succeeded", "outputs": {"extraction": extraction}}}).encode()
                self.upstream.response = (200, {}, body)
                self.assert_safe_failure("model_invalid_extraction")

    def test_missing_workflow_fields_do_not_raise_unhandled_errors(self):
        for response in ({}, {"data": []}, {"data": {"status": "succeeded"}}, {"data": {"status": "succeeded", "outputs": []}}):
            with self.subTest(keys=list(response)):
                self.upstream.response = (200, {}, json.dumps(response).encode())
                self.assert_safe_failure("model_invalid_extraction")

    def test_invented_quotation_and_forbidden_task_control_fields_reject_whole_output(self):
        for mode in ("quote", "status", "priority", "must_do", "remaining_minutes"):
            with self.subTest(mode=mode):
                raw = deepcopy(RAW_EXTRACTION)
                if mode == "quote":
                    raw["candidates"][0]["action_quote"] = "原文没有的TRACE-SENTINEL引文"
                else:
                    raw["candidates"][0][mode] = True
                self.upstream.response = (200, {}, wire_response(raw))
                self.assert_safe_failure("model_invalid_extraction")

    def test_invalid_public_input_never_contacts_upstream(self):
        for payload in ({"text": ""}, {"text": TEXT, "tasks": []}, {"text": TEXT, "api_key": KEY}, {"text": TEXT, "timezone": "UTC"},
                        {"text": TEXT, "reference_date": "2026-02-30"}, {"text": "x" * 20001}):
            with self.subTest(keys=list(payload)):
                with self.assertRaises(ValueError):
                    extract_notice(payload)
        self.assertEqual(self.upstream.requests, [])

    def test_configuration_and_extraction_do_not_mutate_input_or_expose_key(self):
        original = deepcopy(PAYLOAD)
        result = extract_notice(PAYLOAD)
        self.assertEqual(PAYLOAD, original)
        self.assertNotIn(KEY, json.dumps(result, ensure_ascii=False))
        self.assertEqual(len(self.upstream.requests), 1)

    def test_ambient_proxy_is_not_used_to_send_credentials(self):
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        # Clear the usual loopback bypass, so this only passes with ProxyHandler({}).
        os.environ["no_proxy"] = ""
        os.environ["NO_PROXY"] = ""
        self.assertEqual(extract_notice(PAYLOAD)["provider"], "dify")


if __name__ == "__main__":
    unittest.main()
