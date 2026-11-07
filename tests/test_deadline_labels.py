"""A time directly followed by 前/截止 is a deadline even when an event word is nearer."""
from unittest import TestCase

from campus_agent.extractor import parse_notice_text


def labels(text):
    result = parse_notice_text(text, source='user input', reference_date='2026-11-06').to_dict()
    return [(point['time_type'], point['value'], point['kind']) for point in result['time_points']]


class DeadlineLabelTests(TestCase):
    def test_registration_before_a_lecture_word_is_a_registration_deadline(self):
        text = '请于2026年11月7日15:00前完成讲座报名。讲座于2026年11月7日18:00-20:00举行。请于2026年11月7日23:59前提交课程报告。'
        self.assertEqual(labels(text), [('报名截止', '2026-11-07 15:00', 'point'),
                                         ('活动/比赛时间', '2026-11-07 18:00', 'interval'),
                                         ('材料提交', '2026-11-07 23:59', 'point')])

    def test_generic_before_without_action_word_is_a_deadline(self):
        self.assertEqual(labels('讲座资料请于2026年11月8日12:00之前处理完毕。')[0][0], '截止时间')

    def test_an_event_time_without_before_stays_an_event(self):
        self.assertEqual(labels('讲座于2026年11月7日18:00-20:00举行。')[0][0], '活动/比赛时间')
