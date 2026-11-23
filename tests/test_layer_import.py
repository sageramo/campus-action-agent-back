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

    def test_campus_events_without_juxing_become_events_and_strip_trailing_colons(self):
        text = '【摄影社招新通知】\n招新说明会时间：2026年11月25日14:30-15:30，地点：学活204。\n说明会报名截止时间：2026年11月25日18:00。'
        imported = items_from_rules(text, '2026-11-23')
        assemble(imported)
        self.assertEqual([(item['title'], item['start_at'], item['end_at'], item['location']) for item in imported['events']],
                         [('招新说明会时间', '2026-11-25T14:30:00+08:00', '2026-11-25T15:30:00+08:00', '学活204')])
        self.assertEqual([(item['title'], item['deadline_at']) for item in imported['tasks']],
                         [('说明会报名截止时间', '2026-11-25T18:00:00+08:00')])
        start_only = items_from_rules('2026年11月25日14:30 摄影社招新说明会，地点为学活204。', '2026-11-23')
        assemble(start_only)
        self.assertEqual([(item['title'], item['start_at'], item['end_at']) for item in start_only['events']],
                         [('摄影社招新说明会', '2026-11-25T14:30:00+08:00', None)])
        gala = items_from_rules('2026年11月28日19:00-21:30 迎新晚会，地点：中传礼堂。', '2026-11-23')
        self.assertEqual(len(gala['events']), 1)
        self.assertEqual(gala['tasks'], [])

    def test_leading_juxing_and_parenthesized_venue_are_split_cleanly(self):
        notice = (
            '【摄影社招新通知】\n'
            '2026年11月25日14:30-15:30举行摄影社招新说明会（学活204）。\n'
            '2026年11月25日18:00前提交社团报名表。'
        )
        imported = items_from_rules(notice, '2026-11-25')
        assemble(imported)
        self.assertEqual(
            [(item['title'], item['start_at'], item['end_at'], item['location']) for item in imported['events']],
            [('摄影社招新说明会', '2026-11-25T14:30:00+08:00', '2026-11-25T15:30:00+08:00', '学活204')],
        )
        self.assertTrue(any(ev['field'] == 'location' and ev['quote'] == '学活204' for ev in imported['events'][0]['evidence']))
        self.assertEqual(
            [(item['title'], item['deadline_at']) for item in imported['tasks']],
            [('提交社团报名表', '2026-11-25T18:00:00+08:00')],
        )
        non_venue = items_from_rules('2026年11月25日14:30-15:30举行摄影社招新说明会（全体新生参加）。', '2026-11-25')
        assemble(non_venue)
        self.assertEqual(
            [(item['title'], item['location']) for item in non_venue['events']],
            [('摄影社招新说明会（全体新生参加）', None)],
        )

    def test_comma_separated_em_dash_interval_becomes_event_with_clean_title(self):
        text = '11月25日（周三）14:30—15:30，摄影社招新说明会，地点：学活204。'
        imported = items_from_rules(text, '2026-11-25')
        assemble(imported)
        self.assertEqual(
            [(item['title'], item['start_at'], item['end_at'], item['location']) for item in imported['events']],
            [('摄影社招新说明会', '2026-11-25T14:30:00+08:00', '2026-11-25T15:30:00+08:00', '学活204')],
        )
        self.assertEqual(imported['tasks'], [])
        # Two timed clauses separated by a comma must not leak the second clause's hint into the first.
        dual = items_from_rules('2026年11月25日14:30—15:30，2026年11月25日18:00前提交报名表。', '2026-11-25')
        assemble(dual)
        self.assertEqual(dual['events'], [])
        self.assertEqual([item['deadline_at'] for item in dual['tasks']],
                         ['2026-11-25T15:30:00+08:00', '2026-11-25T18:00:00+08:00'])

    def test_change_notice_location_drops_the_verb(self):
        text = '摄影社招新说明会改到2026年11月26日19:00-20:00，地点改为学活305。'
        imported = items_from_rules(text, '2026-11-25')
        assemble(imported)
        self.assertEqual([item['location'] for item in imported['events']], ['学活305'])
        quotes = [entry['quote'] for entry in imported['events'][0]['evidence'] if entry['field'] == 'location']
        self.assertEqual(quotes, ['学活305'])

    def test_niyu_juban_inline_venue_becomes_event_not_deadline(self):
        notice = (
            '@所有人 为纪念中国工农红军长征胜利 90 周年，引导广大学生从伟大长征精神中汲取奋进力量，'
            '坚定理想信念、厚植家国情怀，学校特邀江西于都“长征源”宣讲团，拟于 11 月 24 日（周二）15:00在中传礼堂举办“永远的长征”主题大课。\n'
            '我院共分配30个名额，活动需提前15分钟到场签到，参加本次活动加校级德育20分，有意向同学请填写以下链接https://www.wjx.top/vm/eygYdZK.aspx'
        )
        imported = items_from_rules(notice, '2026-11-23')
        assemble(imported)
        self.assertEqual(imported['tasks'], [])
        self.assertEqual(
            [(item['title'], item['start_at'], item['end_at'], item['location']) for item in imported['events']],
            [('“永远的长征”主题大课', '2026-11-24T15:00:00+08:00', None, '中传礼堂')],
        )
        self.assertTrue(any(ev['field'] == 'location' and ev['quote'] == '中传礼堂' for ev in imported['events'][0]['evidence']))

        mixed = '请于2026年11月24日15:00前提交活动报名表，学术分享定于2026年11月25日19:00在48教A201召开。'
        imported_mixed = items_from_rules(mixed, '2026-11-23')
        assemble(imported_mixed)
        self.assertEqual([(item['title'], item['deadline_at']) for item in imported_mixed['tasks']],
                         [('提交活动报名表', '2026-11-24T15:00:00+08:00')])
        self.assertEqual([(item['title'], item['start_at'], item['location']) for item in imported_mixed['events']],
                         [('参加学术分享', '2026-11-25T19:00:00+08:00', '48教A201')])

    def test_parenthesized_date_after_relative_weekday_and_topic_line(self):
        notice = (
            '【家长心理讲座通知】各位老师：学校心理中心定于本周日晚举办线上心理讲座，具体安排如下：\n'
            '⌚讲座时间：本周日（11月29日）19:00-20:30\n'
            '📌讲座主题：远山有信，温言相伴——大学生家长的亲子沟通艺术\n'
            '💻参与方式：#腾讯会议：784-711-232；密码：1129'
        )
        imported = items_from_rules(notice, None)
        assemble(imported)
        self.assertEqual(imported['tasks'], [])
        self.assertEqual(
            [(ev['title'], ev['start_at'], ev['end_at']) for ev in imported['events']],
            [('远山有信，温言相伴——大学生家长的亲子沟通艺术', '2026-11-29T19:00:00+08:00', '2026-11-29T20:30:00+08:00')],
        )
        # Duplicate identical relative-date warnings are collapsed to one.
        self.assertEqual(len(imported['warnings']), len(set(imported['warnings'])))
        # A bare correction sentence without an existing target must not create a bogus "应该是（周六）" task.
        with self.assertRaises(ValueError):
            items_from_rules('消息错了，应该是11月28日（周六）', None)


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
