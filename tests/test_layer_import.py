"""Notice extraction turned into v3 events and tasks, plus the v3 HTTP routes."""
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
from unittest import TestCase
from unittest.mock import patch

from campus_agent.layer_import import items_from_candidates, items_from_rules
from campus_agent.layers import validate_snapshot_v3
from campus_agent.server import CampusAgentHandler

NOTICE = ('【虚构示例】讲座与课程报告安排\n请于2026年11月7日15:00前完成讲座报名。讲座于2026年11月7日18:00-20:00举行，'
          '地点为主楼405。请于2026年11月7日23:59前提交课程报告。')


def assemble(imported):
    data = {'schema_version': 3, 'revision': 0, 'timezone': 'Asia/Shanghai', 'reference_time': '2026-11-06T09:00:00+08:00',
            'sources': [imported['source']], 'daily_window': None, 'routines_confirmed': False, 'routines': [],
            'free_adjustments': [], 'events': imported['events'], 'tasks': imported['tasks']}
    validate_snapshot_v3(data)
    return data


class RulesImportTests(TestCase):
    def test_example_notice_becomes_one_event_and_two_deadlines_without_asking(self):
        imported = items_from_rules(NOTICE, '2026-11-06')
        assemble(imported)
        self.assertEqual([(item['start_at'], item['end_at']) for item in imported['events']],
                         [('2026-11-07T18:00:00+08:00', '2026-11-07T20:00:00+08:00')])
        self.assertEqual(sorted(item['deadline_at'] for item in imported['tasks']),
                         ['2026-11-07T15:00:00+08:00', '2026-11-07T23:59:00+08:00'])
        self.assertTrue(all(item['estimate_minutes'] is None for item in imported['tasks']))
        self.assertEqual([(item['title'], item['location']) for item in imported['events']], [('参加讲座', '主楼405')])
        self.assertEqual(sorted(item['title'] for item in imported['tasks']), ['完成讲座报名', '提交课程报告'])
        for item in [*imported['events'], *imported['tasks']]:
            self.assertTrue(any(evidence['field'] == 'title' and evidence['quote'] in NOTICE for evidence in item['evidence']))
        self.assertEqual(imported['mode'], 'rules')

    def test_a_notice_without_times_gives_one_undefined_task(self):
        imported = items_from_rules('请尽快完成学生信息核对。', None)
        assemble(imported)
        self.assertEqual(imported['events'], [])
        self.assertEqual([item['deadline_at'] for item in imported['tasks']], [None])


def candidate(kind, **fields):
    item = {'title': '事项', 'kind': kind, 'deadline_at': None, 'fixed_start_at': None, 'fixed_end_at': None, 'visit_windows': [],
            'location': None, 'materials': [], 'evidence': [{'field': 'title', 'origin': 'source_text', 'quote': '原文'}],
            'uncertainties': ['提醒']}
    item.update(fields)
    return item


class ModelImportTests(TestCase):
    def test_each_candidate_kind_maps_to_the_right_layer(self):
        result = {'schema_version': 1, 'provider': 'dify', 'parsing_status': 'needs_confirmation', 'warnings': ['全局提醒'], 'candidates': [
            candidate('fixed', title='讲座', fixed_start_at='2026-11-07T18:00:00+08:00', fixed_end_at='2026-11-07T20:00:00+08:00', location='主楼405'),
            candidate('fixed', title='比赛', fixed_start_at='2026-11-08T09:00:00+08:00'),
            candidate('fixed', title='参加讲座（时间待定）'),
            candidate('flexible', title='提交报告', deadline_at='2026-11-07T23:59:00+08:00', materials=['报告']),
            candidate('milestone', title='公示'),
            candidate('visit_window', title='领取学生证', visit_windows=[{'start': '2026-11-10T09:00:00+08:00', 'end': '2026-11-12T17:00:00+08:00'}]),
        ]}
        imported = items_from_candidates(result, '原文', '2026-11-06')
        assemble(imported)
        events = {item['title']: item for item in imported['events']}
        tasks = {item['title']: item for item in imported['tasks']}
        self.assertEqual((events['讲座']['end_at'], events['讲座']['location']), ('2026-11-07T20:00:00+08:00', '主楼405'))
        self.assertEqual(events['比赛']['end_at'], None)
        self.assertEqual(events['参加讲座（时间待定）']['start_at'], None)
        self.assertEqual((tasks['提交报告']['deadline_at'], tasks['提交报告']['materials']), ('2026-11-07T23:59:00+08:00', ['报告']))
        self.assertEqual(tasks['公示']['deadline_at'], None)
        self.assertEqual((tasks['领取学生证']['earliest_start_at'], tasks['领取学生证']['deadline_at']),
                         ('2026-11-10T09:00:00+08:00', '2026-11-12T17:00:00+08:00'))
        self.assertTrue(any('办理窗口' in note for note in tasks['领取学生证']['notes']))
        self.assertEqual(imported['warnings'], ['全局提醒'])
        self.assertTrue(all('全局提醒' not in item['notes'] for item in [*imported['events'], *imported['tasks']]))
        self.assertEqual(imported['mode'], 'model')


class Quiet(CampusAgentHandler):
    def log_message(self, *args):
        pass


class V3RouteTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post(self, path, payload, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('POST', path, body=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                               headers={'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_rules_extract_then_calendar_round_trip(self):
        status, imported = self.post('/api/v3/extract', {'text': NOTICE, 'reference_date': '2026-11-06', 'mode': 'rules'})
        self.assertEqual(status, 200)
        data = assemble(imported)
        data['daily_window'] = [{'start': '08:00', 'end': '23:00'}] * 7
        status, result = self.post('/api/v3/calendar', {'snapshot': data})
        self.assertEqual(status, 200)
        self.assertEqual(len(result['layers']['temporary']), 1)
        self.assertEqual({item['state'] for item in result['tasks']}, {'estimate_undefined'})

    def test_bad_requests_are_400_and_unknown_mode_is_rejected(self):
        self.assertEqual(self.post('/api/v3/calendar', {'snapshot': {}})[0], 400)
        self.assertEqual(self.post('/api/v3/extract', {'text': NOTICE, 'mode': 'guess'})[0], 400)

    def test_model_mode_keeps_the_local_only_guard(self):
        status, result = self.post('/api/v3/extract', {'text': NOTICE, 'mode': 'model'}, {'Origin': 'https://attacker.invalid'})
        self.assertEqual((status, result['code']), (403, 'local_access_required'))

    def test_model_mode_maps_the_normalized_candidates(self):
        fake = {'schema_version': 1, 'provider': 'dify', 'parsing_status': 'needs_confirmation', 'warnings': [],
                'candidates': [candidate('flexible', title='提交课程报告', deadline_at='2026-11-07T23:59:00+08:00',
                                         evidence=[{'field': 'title', 'origin': 'source_text', 'quote': '提交课程报告'}])]}
        with patch('campus_agent.server.extract_notice', return_value=fake):
            status, result = self.post('/api/v3/extract', {'text': NOTICE, 'reference_date': '2026-11-06', 'mode': 'model'},
                                       {'Origin': 'http://127.0.0.1:8873'})
        self.assertEqual(status, 200)
        self.assertEqual(result['tasks'][0]['deadline_at'], '2026-11-07T23:59:00+08:00')
