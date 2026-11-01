import http.client
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from campus_agent.extraction_access import ExtractionBudget, local_extraction_allowed
from campus_agent.server import CampusAgentHandler


class BudgetTests(unittest.TestCase):
    def test_real_loopback_only_and_explicit_origin(self):
        with patch.dict(os.environ, {}, clear=True):
            for ip in ['127.0.0.1', '::1']:
                self.assertTrue(local_extraction_allowed(ip, None))
                self.assertTrue(local_extraction_allowed(ip, 'http://127.0.0.1:8873'))
                self.assertFalse(local_extraction_allowed(ip, 'https://attacker.invalid'))
                self.assertFalse(local_extraction_allowed(ip, 'null'))
            for ip in ['192.168.1.3', '8.8.8.8', 'malformed']:
                self.assertFalse(local_extraction_allowed(ip, 'http://127.0.0.1:8873'))

    def test_origin_override_is_exact_and_does_not_enable_nonlocal_clients(self):
        with patch.dict(os.environ, {'SIXU_MODEL_ALLOWED_ORIGINS': 'https://my-preview.invalid'}):
            self.assertTrue(local_extraction_allowed('127.0.0.1', 'https://my-preview.invalid'))
            self.assertFalse(local_extraction_allowed('127.0.0.1', 'https://my-preview.invalid.evil'))
            self.assertFalse(local_extraction_allowed('10.0.0.2', 'https://my-preview.invalid'))

    def test_concurrency_and_minute_limit_include_failed_calls(self):
        clock = [0.0]; budget = ExtractionBudget(lambda: clock[0])
        self.assertIsNone(budget.acquire())
        self.assertIn('正在调用', budget.acquire()); budget.release()
        for _ in range(5):
            self.assertIsNone(budget.acquire()); budget.release()
        self.assertIn('上限', budget.acquire())
        clock[0] = 61
        self.assertIsNone(budget.acquire()); budget.release()

    def test_hour_limit_and_recovery(self):
        clock = [0.0]; budget = ExtractionBudget(lambda: clock[0])
        for minute in range(10):
            clock[0] = minute * 61
            for _ in range(6):
                self.assertIsNone(budget.acquire()); budget.release()
        clock[0] = 1000
        self.assertIn('上限', budget.acquire())
        clock[0] = 3601
        self.assertIsNone(budget.acquire()); budget.release()


class QuietHandler(CampusAgentHandler):
    def log_message(self, *args):
        pass


class ExtractHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(5)

    def request(self, origin='http://127.0.0.1:8873'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('POST', '/api/v2/extract', json.dumps({'text': '测试通知'}), {'Content-Type': 'application/json', 'Origin': origin})
            result = connection.getresponse()
            return result.status, json.loads(result.read())
        finally:
            connection.close()

    def test_untrusted_web_origin_is_rejected_before_model_call(self):
        with patch('campus_agent.server.extract_notice') as model:
            self.assertEqual(self.request(origin='https://attacker.invalid')[0], 403)
            model.assert_not_called()

    def test_disabled_service_has_safe_error_and_no_rule_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch('campus_agent.server.EXTRACTION_BUDGET', ExtractionBudget()), patch('campus_agent.server.parse_notice_text') as rules:
            status, body = self.request()
            self.assertEqual(status, 503)
            self.assertIn('code', body)
            rules.assert_not_called()

    def test_busy_budget_returns_429_without_model_call(self):
        budget = ExtractionBudget(); budget.acquire()
        with patch('campus_agent.server.EXTRACTION_BUDGET', budget), patch('campus_agent.server.extract_notice') as model:
            self.assertEqual(self.request()[0], 429)
            model.assert_not_called()

    def test_success_releases_slot_and_never_returns_task_truth(self):
        budget = ExtractionBudget()
        output = {'schema_version': 1, 'provider': 'dify', 'parsing_status': 'needs_confirmation', 'candidates': [], 'warnings': []}
        with patch('campus_agent.server.EXTRACTION_BUDGET', budget), patch('campus_agent.server.extract_notice', return_value=output):
            status, body = self.request()
            self.assertEqual((status, body), (200, output))
            self.assertIsNone(budget.acquire())
            self.assertNotIn('tasks', body)
