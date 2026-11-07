"""End-time estimates for 点墨 that only have a start time."""
from unittest import TestCase
from unittest.mock import patch

from campus_agent.end_estimate import estimate_end

START = '2026-11-11T19:00:00+08:00'


def one(title, **extra):
    return {'id': 'e1', 'title': title, 'start_at': START, **extra}


class RuleTests(TestCase):
    def test_kinds_of_events_get_their_usual_length(self):
        cases = {'学术讲座': 90, '英语四级考试': 120, '学生会面试': 30, '迎新晚会': 150, '班会': 60, '校园歌手决赛': 180, '某某活动': 60}
        for title, minutes in cases.items():
            result = estimate_end({'items': [one(title)]})['estimates'][0]
            self.assertEqual((title, result['minutes'], result['basis']), (title, minutes, 'rules'))
        self.assertEqual(estimate_end({'items': [one('学术讲座')]})['estimates'][0]['end_at'], '2026-11-11T20:30:00+08:00')
        self.assertIn('讲座', estimate_end({'items': [one('学术讲座')]})['estimates'][0]['reason'])

    def test_bad_requests_are_rejected(self):
        for body in [{}, {'items': []}, {'items': [one('讲座', extra=1)]}, {'items': [one('讲座')], 'mode': 'guess'},
                     {'items': [{'id': 'e1', 'title': '讲座', 'start_at': 'tomorrow'}]}]:
            with self.assertRaises(ValueError):
                estimate_end(body)


class ModelTests(TestCase):
    def test_the_model_reads_the_notice_and_its_minutes_are_checked(self):
        with patch('campus_agent.end_estimate.chat_json', return_value={'minutes': 120, 'reason': '原文写“约2小时”'}) as call:
            result = estimate_end({'mode': 'model', 'items': [one('讲座', context='19:00开始，约2小时')]})['estimates'][0]
        self.assertEqual((result['minutes'], result['end_at'], result['basis']), (120, '2026-11-11T21:00:00+08:00', 'model'))
        self.assertIn('约2小时', call.call_args.args[1])
        with patch('campus_agent.end_estimate.chat_json', return_value={'minutes': 5000}):
            with self.assertRaises(ValueError):
                estimate_end({'mode': 'model', 'items': [one('讲座')]})
