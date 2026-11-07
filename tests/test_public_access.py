"""Public access to model features: access code, page origin, lockout and daily caps."""
import os
import unittest
from unittest.mock import patch

from campus_agent.extraction_access import PublicModelGate, forwarded_client_ip
from tests.test_api import APITests

PAGE = 'https://sageramo.github.io'
ENV = {'SIXU_MODEL_ACCESS_CODE': 'ink-and-paper', 'SIXU_MODEL_DAILY_LIMIT': '3', 'SIXU_MODEL_IP_DAILY_LIMIT': '2'}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_800_000_000.0]
        self.gate = PublicModelGate(lambda: self.now[0])

    def code(self, result):
        return result and result[2]

    def test_without_a_server_code_only_the_local_workbench_may_use_models(self):
        with patch.dict(os.environ, {'SIXU_MODEL_ACCESS_CODE': ''}):
            self.assertEqual(self.code(self.gate.check('1.2.3.4', PAGE, 'anything')), 'local_access_required')
            self.assertEqual(self.gate.status(), {'code_required': False})

    def test_the_code_and_the_page_origin_are_both_required(self):
        with patch.dict(os.environ, ENV):
            self.assertEqual(self.code(self.gate.check('1.2.3.4', 'https://evil.invalid', 'ink-and-paper')), 'origin_not_allowed')
            self.assertEqual(self.code(self.gate.check('1.2.3.4', PAGE, None)), 'access_code_required')
            self.assertEqual(self.code(self.gate.check('1.2.3.4', PAGE, 'wrong')), 'access_code_invalid')
            self.assertIsNone(self.gate.check('1.2.3.4', PAGE, ' ink-and-paper '))

    def test_ten_wrong_codes_lock_that_address_for_an_hour(self):
        with patch.dict(os.environ, ENV):
            for _ in range(10):
                self.gate.check('5.5.5.5', PAGE, 'guess')
            self.assertEqual(self.code(self.gate.check('5.5.5.5', PAGE, 'ink-and-paper')), 'access_code_locked')
            self.assertIsNone(self.gate.check('6.6.6.6', PAGE, 'ink-and-paper'), 'other addresses are not affected')
            self.now[0] += 3601
            self.assertIsNone(self.gate.check('5.5.5.5', PAGE, 'ink-and-paper'))

    def test_calls_are_capped_per_address_and_for_the_whole_site_each_day(self):
        with patch.dict(os.environ, ENV):
            self.assertIsNone(self.gate.check('1.1.1.1', PAGE, 'ink-and-paper'))
            self.assertIsNone(self.gate.check('1.1.1.1', PAGE, 'ink-and-paper'))
            self.assertEqual(self.code(self.gate.check('1.1.1.1', PAGE, 'ink-and-paper')), 'model_ip_daily_limit')
            self.assertIsNone(self.gate.check('2.2.2.2', PAGE, 'ink-and-paper'))
            self.assertEqual(self.code(self.gate.check('3.3.3.3', PAGE, 'ink-and-paper')), 'model_daily_limit')
            self.now[0] += 86400
            self.assertIsNone(self.gate.check('3.3.3.3', PAGE, 'ink-and-paper'), 'a new Beijing day resets the counts')

    def test_the_client_address_is_the_one_the_proxy_appended(self):
        self.assertEqual(forwarded_client_ip('10.0.0.1', '9.9.9.9, 203.0.113.7'), '203.0.113.7')
        self.assertEqual(forwarded_client_ip('10.0.0.1', None), '10.0.0.1')


class PublicAccessAPITests(APITests):
    """Over HTTP the test client is loopback, so a foreign Origin stands for a public page."""
    body = {'mode': 'model', 'items': [{'id': 'e1', 'title': '讲座', 'start_at': '2026-11-11T19:00:00+08:00'}]}

    def test_cors_allows_the_access_header(self):
        status, _ = self.request('OPTIONS', '/api/v3/estimate-end')
        self.assertEqual(status, 204)

    def test_public_model_calls_need_the_code_and_then_reach_the_model(self):
        with patch.dict(os.environ, {**ENV, 'SIXU_MODEL_IP_DAILY_LIMIT': '50', 'SIXU_MODEL_DAILY_LIMIT': '50', 'DEEPSEEK_API_KEY': ''}):
            status, result = self.request('POST', '/api/v3/estimate-end', self.body, headers={'Origin': PAGE})
            self.assertEqual((status, result['code']), (401, 'access_code_required'))
            status, result = self.request('POST', '/api/v3/estimate-end', self.body, headers={'Origin': PAGE, 'X-Sixu-Access': 'ink-and-paper'})
            self.assertEqual((status, result['code']), (503, 'model_not_configured'), 'past the gate, the missing key is the next stop')
            status, result = self.request('POST', '/api/v3/estimate-end', {**self.body, 'mode': 'rules'}, headers={'Origin': PAGE})
            self.assertEqual(status, 200, 'rule features never need the code')
            status, result = self.request('GET', '/api/health')
            self.assertEqual(result['model_access'], {'code_required': True})


del APITests
