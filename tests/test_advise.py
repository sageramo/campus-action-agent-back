"""Weighing 叠墨 strategies: rules take the recommendation, the model may only pick an offered option."""
from unittest import TestCase
from unittest.mock import patch

from campus_agent.advise import advise

ITEMS = [
    {'title': '学术讲座', 'layer': 'temporary', 'start': '2026-11-11T09:00:00+08:00', 'end': '2026-11-11T10:30:00+08:00', 'location': '主楼报告厅'},
    {'title': '高等数学', 'layer': 'routine', 'start': '2026-11-11T08:00:00+08:00', 'end': '2026-11-11T09:50:00+08:00'},
]
OPTIONS = [
    {'action': 'skip_routine_date', 'label': '这次不上「高等数学」', 'detail': '底色里记下这一次不占用。', 'recommended': True},
    {'action': 'cancel_event', 'label': '不去「学术讲座」', 'detail': '照常上课。', 'recommended': False},
]


def body(**extra):
    return {'type': 'temporary_routine_overlap', 'items': ITEMS, 'strategies': OPTIONS, 'advice': '点墨错过就没有了。', **extra}


class RuleAdviceTests(TestCase):
    def test_rules_take_the_recommended_option_and_draft_a_leave_note(self):
        result = advise(body())
        self.assertEqual((result['pick'], result['basis']), (0, 'rules'))
        self.assertEqual(result['reason'], '点墨错过就没有了。')
        note = result['note']
        self.assertIn('11月11日（周三）08:00的「高等数学」', note)
        self.assertIn('学术讲座', note)
        self.assertIn('主楼报告厅', note)
        self.assertIn('【姓名】', note)

    def test_no_note_unless_a_class_is_skipped(self):
        options = [dict(OPTIONS[1], recommended=True), OPTIONS[0]]
        self.assertIsNone(advise(body(strategies=options))['note'])

    def test_bad_requests_are_rejected(self):
        bad = [{}, body(mode='guess'), body(extra=1), body(items=[]), body(strategies=[]),
               body(strategies=[{'action': 'delete_everything', 'label': 'x', 'detail': 'y'}]),
               body(items=[dict(ITEMS[0], layer='sky')]), body(context='字' * 5000)]
        for payload in bad:
            with self.assertRaises(ValueError):
                advise(payload)


class ModelAdviceTests(TestCase):
    def test_the_model_picks_an_offered_option_and_explains(self):
        answer = {'pick': 1, 'reason': '高数这周讲新章，讲座有回放。', 'note': '不应出现'}
        with patch('campus_agent.advise.chat_json', return_value=answer) as call:
            result = advise(body(mode='model', context='讲座提供回放。'))
        self.assertEqual((result['pick'], result['basis'], result['note']), (1, 'model', None))
        self.assertEqual(result['reason'], '高数这周讲新章，讲座有回放。')
        prompt = call.call_args.args[1]
        self.assertIn('0. 这次不上「高等数学」', prompt)
        self.assertIn('讲座提供回放。', prompt)

    def test_a_model_note_is_kept_and_a_missing_one_falls_back_to_the_template(self):
        with patch('campus_agent.advise.chat_json', return_value={'pick': 0, 'reason': '讲座只此一次。', 'note': '老师您好，【姓名】请假。'}):
            self.assertEqual(advise(body(mode='model'))['note'], '老师您好，【姓名】请假。')
        with patch('campus_agent.advise.chat_json', return_value={'pick': 0, 'reason': '讲座只此一次。'}):
            self.assertIn('【班级】', advise(body(mode='model'))['note'])

    def test_answers_outside_the_options_are_refused(self):
        for answer in [{'pick': 2, 'reason': 'x'}, {'pick': -1, 'reason': 'x'}, {'pick': True, 'reason': 'x'}, {'pick': '0', 'reason': 'x'}, {'pick': 0, 'reason': ''}]:
            with patch('campus_agent.advise.chat_json', return_value=answer):
                with self.assertRaises(ValueError):
                    advise(body(mode='model'))
