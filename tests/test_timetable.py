"""Timetable text -> weekly routines: the model only structures, the program computes dates and times."""
import json
import os
from unittest import TestCase
from unittest.mock import patch

from campus_agent.dify_client import ExtractionServiceError
from campus_agent.layers import calendar_snapshot
from campus_agent.timetable import recognize_timetable, routines_from_courses

TEXT = ('高等数学 周一 第1-2节 1-16周 主楼301 张老师\n'
        '大学英语 周三 第3-4节 1-16周(单) 外语楼204\n'
        '体育 周五 14:00-15:40 2-16周 东操场')
PERIODS = {'1': {'start': '08:00', 'end': '08:45'}, '2': {'start': '08:55', 'end': '09:40'},
           '3': {'start': '10:00', 'end': '10:45'}, '4': {'start': '10:55', 'end': '11:40'}}


def course(**changes):
    item = {'title': '高等数学', 'quote': '高等数学 周一 第1-2节 1-16周 主楼301', 'weekdays': [1], 'start_period': 1, 'end_period': 2,
            'start_time': None, 'end_time': None, 'weeks': [[1, 16]], 'parity': 'all', 'location': '主楼301'}
    item.update(changes)
    return item


def convert(courses, term_start='2026-09-07', periods=PERIODS):
    return routines_from_courses(courses, term_start, periods, 's-timetable')


class ConversionTests(TestCase):
    def test_periods_and_weeks_become_a_dated_weekly_routine(self):
        routines, drafts = convert([course()])
        self.assertEqual(drafts, [])
        routine = routines[0]
        self.assertEqual((routine['start'], routine['end'], routine['weekdays']), ('08:00', '09:40', [1]))
        self.assertEqual((routine['from'], routine['until'], routine['week_interval']), ('2026-09-07', '2026-12-27', 1))
        self.assertEqual((routine['location'], routine['source_id']), ('主楼301', 's-timetable'))

    def test_odd_and_even_weeks_alternate_from_the_right_week(self):
        odd = convert([course(parity='odd')])[0][0]
        even = convert([course(parity='even')])[0][0]
        self.assertEqual((odd['from'], odd['week_interval']), ('2026-09-07', 2))
        self.assertEqual((even['from'], even['week_interval']), ('2026-09-14', 2))

    def test_split_week_ranges_give_one_routine_each_and_explicit_times_win(self):
        routines, _ = convert([course(weeks=[[1, 4], [6, 16]], start_time='14:00', end_time='15:40')])
        self.assertEqual([(item['from'], item['until']) for item in routines], [('2026-09-07', '2026-10-04'), ('2026-10-12', '2026-12-27')])
        self.assertTrue(all((item['start'], item['end']) == ('14:00', '15:40') for item in routines))

    def test_placeholder_locations_are_not_kept_as_places(self):
        for place in ['未安排地点', '待定', '无', '地点待定']:
            with self.subTest(place=place):
                text = f'体育 周二 第5-6节 @{place}'
                result = recognize_timetable({'mode': 'convert', 'text': text, 'term_start': '2026-09-07', 'periods': PERIODS,
                                              'courses': [course(title='体育', quote=text, weekdays=[2], location=place)]})
                self.assertIsNone(result['routines'][0]['location'])

    def test_missing_period_times_or_term_start_stay_as_drafts(self):
        routines, drafts = convert([course()], periods={})
        self.assertEqual(routines, [])
        self.assertIn('节次', drafts[0]['problem'])
        routines, drafts = convert([course()], term_start=None)
        self.assertEqual(routines, [])
        self.assertIn('第1周', drafts[0]['problem'])
        self.assertEqual(drafts[0]['course']['title'], '高等数学')

    def test_routines_are_valid_on_the_calendar(self):
        routines, _ = convert([course(), course(title='大学英语', weekdays=[3], start_period=3, end_period=4, parity='odd')])
        snapshot = {'schema_version': 3, 'revision': 0, 'timezone': 'Asia/Shanghai', 'reference_time': '2026-09-07T07:00:00+08:00',
                    'sources': [{'id': 's-timetable', 'kind': 'timetable', 'text': TEXT, 'published_at': None}],
                    'daily_window': [{'start': '07:00', 'end': '23:00'}] * 7, 'routines_confirmed': True, 'routines': routines,
                    'free_adjustments': [], 'events': [], 'tasks': []}
        result = calendar_snapshot({'snapshot': snapshot})
        self.assertEqual([(item['title'], item['start'][:16]) for item in result['layers']['routine']],
                         [('高等数学', '2026-09-07T08:00'), ('大学英语', '2026-09-09T10:00')])


def model_reply(payload):
    return {'choices': [{'message': {'content': json.dumps(payload, ensure_ascii=False)}, 'finish_reason': 'stop'}]}


class ModelTests(TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'sk-test-secret'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def recognize(self, reply, **payload):
        calls = []

        def fake(config, body):
            calls.append((config, json.loads(body)))
            return reply

        with patch('campus_agent.deepseek_client._fetch', side_effect=fake):
            result = recognize_timetable({'text': TEXT, 'mode': 'model', 'term_start': '2026-09-07', 'periods': PERIODS, **payload})
        return result, calls

    def test_request_disables_thinking_asks_for_json_and_never_sends_the_key_in_the_body(self):
        reply = model_reply({'courses': [course(quote='高等数学 周一 第1-2节 1-16周 主楼301')], 'warnings': []})
        result, calls = self.recognize(reply)
        config, body = calls[0]
        self.assertEqual(body['model'], 'deepseek-flash')
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertIn('json', json.dumps(body['messages'], ensure_ascii=False).lower())
        self.assertNotIn('sk-test-secret', json.dumps(body))
        self.assertEqual(config.key, 'sk-test-secret')
        self.assertEqual(config.endpoint, 'https://api.deepseek.com/chat/completions')
        self.assertEqual(len(result['routines']), 1)
        self.assertEqual(result['source']['kind'], 'timetable')
        self.assertEqual(result['courses'][0]['quote'], '高等数学 周一 第1-2节 1-16周 主楼301')

    def test_courses_without_a_real_quote_or_with_bad_fields_are_dropped_with_a_warning(self):
        reply = model_reply({'courses': [course(quote='原文里没有这句'), course(title='英语', quote='大学英语 周三', weekdays=[8]),
                                         course(title='体育', quote='体育 周五 14:00-15:40', weekdays=[5], start_period=None, end_period=None,
                                                start_time='14:00', end_time='15:40', weeks=[[2, 16]])], 'warnings': []})
        result, _ = self.recognize(reply)
        self.assertEqual([item['title'] for item in result['routines']], ['体育'])
        self.assertEqual(sum('未采用' in note for note in result['warnings']), 2)

    def test_blank_or_non_json_content_is_an_error_not_an_empty_timetable(self):
        for content in ['   ', '不是JSON']:
            with self.subTest(content=content):
                with self.assertRaises(ExtractionServiceError):
                    self.recognize({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})

    def test_missing_key_is_reported_as_not_configured(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            with self.assertRaises(ExtractionServiceError) as caught:
                recognize_timetable({'text': TEXT, 'mode': 'model'})
        self.assertEqual(caught.exception.code, 'model_not_configured')


class ConvertOnlyTests(TestCase):
    def test_courses_can_be_reconverted_without_a_model_call(self):
        with patch('campus_agent.deepseek_client._fetch', side_effect=AssertionError('no model call')):
            result = recognize_timetable({'mode': 'convert', 'courses': [course()], 'term_start': '2026-09-07', 'periods': PERIODS,
                                          'text': TEXT})
        self.assertEqual(len(result['routines']), 1)

    def test_bad_payloads_are_rejected(self):
        for payload in [{'mode': 'convert', 'courses': 'x', 'text': TEXT}, {'mode': 'guess', 'text': TEXT},
                        {'mode': 'convert', 'courses': [], 'text': TEXT, 'term_start': '2026-13-01'},
                        {'mode': 'convert', 'courses': [], 'text': TEXT, 'periods': {'1': {'start': '9:00'}}}]:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    recognize_timetable(payload)


class Quiet(__import__('campus_agent.server', fromlist=['CampusAgentHandler']).CampusAgentHandler):
    def log_message(self, *args):
        pass


class TimetableRouteTests(TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def call(self, method, path, payload=None, headers=None):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
            connection.request(method, path, body=body, headers={'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_convert_mode_needs_no_key_or_local_origin(self):
        status, result = self.call('POST', '/api/v3/timetable', {'mode': 'convert', 'text': TEXT, 'courses': [course()],
                                                                 'term_start': '2026-09-07', 'periods': PERIODS},
                                   {'Origin': 'https://elsewhere.invalid'})
        self.assertEqual((status, len(result['routines'])), (200, 1))

    def test_model_mode_is_local_only_and_reports_missing_key(self):
        status, result = self.call('POST', '/api/v3/timetable', {'mode': 'model', 'text': TEXT}, {'Origin': 'https://attacker.invalid'})
        self.assertEqual((status, result['code']), (403, 'local_access_required'))
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            status, result = self.call('POST', '/api/v3/timetable', {'mode': 'model', 'text': TEXT}, {'Origin': 'http://127.0.0.1:8873'})
        self.assertEqual((status, result['code']), (503, 'model_not_configured'))

    def test_health_reports_only_whether_deepseek_is_configured(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'sk-test-secret'}):
            status, result = self.call('GET', '/api/health')
        self.assertEqual(result['timetable_model'], {'configured': True})
        self.assertIn('timetable_v3', result['capabilities'])
        self.assertNotIn('sk-test-secret', json.dumps(result))


PNG_1PX = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


def grid_cell(**changes):
    item = {'title': '复变函数-05', 'quote': '复变函数-05 @四十八教306', 'weekdays': [1], 'start_period': 3, 'end_period': 4,
            'start_time': None, 'end_time': None, 'weeks': [], 'parity': 'all', 'location': '四十八教306'}
    item.update(changes)
    return item


class ImageTests(TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'sk-test-secret'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def recognize(self, reply, **payload):
        calls = []

        def fake(config, body):
            calls.append(json.loads(body))
            return model_reply(reply)

        with patch('campus_agent.deepseek_client._fetch', side_effect=fake):
            result = recognize_timetable({'mode': 'image', 'image': PNG_1PX, 'term_start': '2026-09-07', **payload})
        return result, calls

    def test_the_image_goes_in_the_user_message_and_the_grid_becomes_a_transcript(self):
        seen = {'1': {'start': '08:00', 'end': '08:50'}, '2': {'start': '09:00', 'end': '09:50'},
                '3': {'start': '10:10', 'end': '11:00'}, '4': {'start': '11:10', 'end': '12:00'}}
        result, calls = self.recognize({'courses': [grid_cell(), grid_cell(title='传播科学导论-01', quote='传播科学导论-01 @四十八教510', start_period=9, end_period=11, location='四十八教510')],
                                        'periods_seen': seen, 'warnings': []}, periods=PERIODS)
        body = calls[0]
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertIsInstance(body['messages'][0]['content'], str, 'no image in the system message')
        user = body['messages'][1]['content']
        self.assertEqual([block['type'] for block in user], ['text', 'image_url'])
        self.assertTrue(user[1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertNotIn('sk-test-secret', json.dumps(body))
        self.assertEqual(result['source']['text'].splitlines()[0], '周一 第3-4节 复变函数-05 @四十八教306')
        self.assertEqual(result['periods_seen'], seen)
        self.assertEqual(result['mode'], 'image')
        self.assertTrue(any('对照截图' in note for note in result['warnings']))
        self.assertEqual([(item['start'], item['end']) for item in result['routines']][0], ('10:00', '11:40'),
                         'the user period table wins over the one seen in the image')
        self.assertEqual(result['drafts'][0]['course']['title'], '传播科学导论-01', 'period 9 is not in the user table')

    def test_without_a_user_period_table_the_seen_one_is_used_and_said_so(self):
        seen = {'3': {'start': '10:10', 'end': '11:00'}, '4': {'start': '11:10', 'end': '12:00'}}
        result, _ = self.recognize({'courses': [grid_cell()], 'periods_seen': seen, 'warnings': []})
        self.assertEqual((result['routines'][0]['start'], result['routines'][0]['end']), ('10:10', '12:00'))
        self.assertTrue(any('截图上读到的节次时间' in note for note in result['warnings']))

    def test_a_bad_seen_period_table_is_ignored_not_fatal(self):
        result, _ = self.recognize({'courses': [grid_cell()], 'periods_seen': {'3': {'start': '10点'}}, 'warnings': []}, periods=PERIODS)
        self.assertEqual(result['periods_seen'], None)
        self.assertEqual(len(result['routines']), 1)

    def test_only_real_images_within_the_size_limit_are_sent(self):
        for image in ['https://example.invalid/a.png', 'data:image/png;base64,bm90IGFuIGltYWdl', 'data:text/plain;base64,aGk=', 'data:image/png;base64,***']:
            with self.subTest(image=image[:30]):
                with patch('campus_agent.deepseek_client._fetch', side_effect=AssertionError('no call')):
                    with self.assertRaises(ValueError):
                        recognize_timetable({'mode': 'image', 'image': image})
        big = 'data:image/png;base64,' + 'A' * (11 * 1024 * 1024)
        with self.assertRaises(ValueError):
            recognize_timetable({'mode': 'image', 'image': big})


class ImageRouteTests(TimetableRouteTests):
    def test_image_mode_is_local_only_and_accepts_bodies_over_one_megabyte(self):
        status, result = self.call('POST', '/api/v3/timetable', {'mode': 'image', 'image': PNG_1PX}, {'Origin': 'https://attacker.invalid'})
        self.assertEqual((status, result['code']), (403, 'local_access_required'))
        padded = {'mode': 'image', 'image': PNG_1PX, 'text': 'x' * 1_200_000}
        status, result = self.call('POST', '/api/v3/timetable', padded, {'Origin': 'http://127.0.0.1:8873'})
        self.assertNotEqual(status, 413)
