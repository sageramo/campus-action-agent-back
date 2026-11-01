"""Source-bound safeguards for role and conditional-action omissions."""
from unittest import TestCase

from campus_agent.candidates import normalize_extraction


def extract(text, action=None):
    return normalize_extraction({'schema_version': 1, 'candidates': [{'title': '核对事项', 'kind': 'flexible',
                                'action_quote': action or text, 'time_quote': None, 'location_quote': None,
                                'materials_quotes': [], 'uncertainties': []}], 'warnings': []}, text)


class NoticeConditionTests(TestCase):
    def test_counsellor_and_class_teacher_duties_require_role_review(self):
        for text in ['请毕业班辅导员于周三审核信息。', '请各年级辅导员老师核实信息。', '请班主任汇总回执。']:
            with self.subTest(text=text):
                candidate = extract(text)['candidates'][0]
                self.assertTrue(any('执行对象原文' in note and ('辅导员' in note or '班主任' in note) for note in candidate['uncertainties']))
                self.assertNotIn('must_do', candidate)

    def test_recipient_is_not_mislabelled_as_the_task_actor(self):
        for text in ['请向辅导员提交申请。', '整理后交给班主任。', '联系负责人核实。']:
            self.assertFalse(any('执行对象原文' in note for note in extract(text)['candidates'][0]['uncertainties']))

    def test_role_stays_visible_globally_when_action_quote_omits_subject(self):
        result = extract('请新生辅导员核实后提交名单。', '提交名单')
        self.assertTrue(any('新生辅导员' in note for note in result['warnings']))

    def test_per_person_pending_status_is_bound_to_named_tasks(self):
        text = '还书、退宿已标记为“无需办理”或“待办理”，仅办理“待办理”事项。请填写就业信息。'
        result = extract(text, '还书')
        self.assertTrue(any('个人办理状态原文' in note and '待办理' in note for note in result['candidates'][0]['uncertainties']))
        other = extract(text, '请填写就业信息')
        self.assertFalse(any('个人办理状态原文' in note for note in other['candidates'][0]['uncertainties']))
        self.assertTrue(any('个人办理状态' in note for note in other['warnings']))

    def test_optional_request_and_precondition_survive_empty_model_notes(self):
        for text, anchor in [('如需延期请提交申请。', '条件行动原文'),
                             ('请完成所有离校环节后领取证书。', '前置条件原文'),
                             ('申请通过后到窗口领取材料。', '前置条件原文')]:
            with self.subTest(text=text):
                candidate = extract(text)['candidates'][0]
                self.assertTrue(any(anchor in note for note in candidate['uncertainties']))
                self.assertNotIn('depends_on', candidate)

    def test_no_implicit_condition_is_added_to_an_ordinary_submission(self):
        notes = extract('请提交课程报告。')['candidates'][0]['uncertainties']
        self.assertFalse(any('条件行动原文' in note or '前置条件原文' in note or '个人办理状态原文' in note for note in notes))

    def test_global_guard_overflow_is_explicit_and_frontend_notes_remain_bounded(self):
        text = '\n'.join(f'第{index}项仅办理“待办理”事项。' for index in range(30))
        result = extract(text, '第0项')
        self.assertLessEqual(len(result['warnings']), 40)
        self.assertTrue(any('未全部展开' in note for note in result['warnings']))
