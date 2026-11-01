"""Reminder consolidation observed on the P07 thinking-off live response."""
from unittest import TestCase

from campus_agent.candidates import normalize_extraction


TIME_NOTE = '未提取到可核对的完整时间引文'
OVERLAP_NOTE = '出自同一句原文'


def candidate(title, action, notes=(), time_quote=None):
    return {'title': title, 'kind': 'flexible', 'action_quote': action, 'time_quote': time_quote,
            'location_quote': None, 'materials_quotes': [], 'uncertainties': list(notes)}


def extract(text, *candidates, reference_date=None):
    return normalize_extraction({'schema_version': 1, 'candidates': list(candidates), 'warnings': []}, text, reference_date)


class TimeReminderTests(TestCase):
    def test_model_note_about_missing_time_replaces_the_generic_reminder(self):
        for note in ['原文未给出还书的时间、地点和所需材料', '原文未明确填报截止时间', '原文没有说明领取日期']:
            with self.subTest(note=note):
                notes = extract('请办理还书。', candidate('还书', '请办理还书', [note]))['candidates'][0]['uncertainties']
                self.assertIn(note, notes)
                self.assertFalse(any(TIME_NOTE in item for item in notes))

    def test_generic_reminder_stays_without_a_model_time_note(self):
        for notes in [[], ['原文未明确该事项与后续各具体环节的先后关系'], ['无需在规定时间内到场']]:
            with self.subTest(notes=notes):
                result = extract('请办理还书。', candidate('还书', '请办理还书', notes))
                self.assertTrue(any(TIME_NOTE in item for item in result['candidates'][0]['uncertainties']))

    def test_time_field_stays_empty_when_the_reminder_is_consolidated(self):
        task = extract('请办理还书。', candidate('还书', '请办理还书', ['原文未给出截止时间']))['candidates'][0]
        self.assertEqual((task['deadline_at'], task['fixed_start_at'], task['fixed_end_at'], task['visit_windows']), (None, None, None, []))


class SameSentenceOverlapTests(TestCase):
    TEXT = ('请各位毕业生登录平台访问《离校手续》，根据表单进行事项办理，内容包括：还书、退宿、补缴费等。\n'
            '如需延期离校请提交申请。')

    def test_umbrella_and_enumerated_items_name_each_other(self):
        result = extract(self.TEXT,
                         candidate('按表单办理离校事项', '请各位毕业生登录平台访问《离校手续》，根据表单进行事项办理'),
                         candidate('还书', '内容包括：还书、退宿、补缴费等。'),
                         candidate('退宿', '内容包括：还书、退宿、补缴费等。'),
                         candidate('延期离校申请', '如需延期离校请提交申请。'))
        notes = [[item for item in task['uncertainties'] if OVERLAP_NOTE in item] for task in result['candidates']]
        self.assertEqual([len(group) for group in notes], [1, 1, 1, 0])
        self.assertIn('「还书」', notes[0][0])
        self.assertIn('「退宿」', notes[0][0])
        self.assertNotIn('「按表单办理离校事项」', notes[0][0])
        self.assertIn('「按表单办理离校事项」', notes[1][0])
        self.assertEqual(len(result['candidates']), 4)

    def test_candidates_from_separate_sentences_get_no_overlap_note(self):
        result = extract('请提交课程报告。\n请参加班会。',
                         candidate('提交报告', '请提交课程报告。'), candidate('参加班会', '请参加班会。'))
        self.assertFalse(any(OVERLAP_NOTE in note for task in result['candidates'] for note in task['uncertainties']))

    def test_overlap_note_lists_a_bounded_number_of_titles(self):
        items = [f'事项{index}' for index in range(10)]
        text = '内容包括：' + '、'.join(items) + '。'
        result = extract(text, *(candidate(item, item) for item in items))
        note = next(item for item in result['candidates'][0]['uncertainties'] if OVERLAP_NOTE in item)
        self.assertEqual(note.count('「'), 6)
        self.assertIn('等9项', note)


YEAR_ASSUMED = '原文未给年份，暂按 '
DATE_ONLY = '仅有日期、日期跨度'


class YearAndDateReminderTests(TestCase):
    def test_model_year_note_folds_into_the_assumed_year_note_when_a_time_is_used(self):
        text = '请于9月15日下午5点前提交学生证信息。'
        task = extract(text, candidate('提交信息', '请于9月15日下午5点前提交学生证信息', ['原文未写明年份，仅有“9月15日”，需核对具体年份。'],
                                       time_quote='9月15日下午5点前'), reference_date='2025-09-01')['candidates'][0]
        self.assertEqual(task['deadline_at'], '2025-09-15T17:00:00+08:00')
        self.assertEqual(sum('年份' in note for note in task['uncertainties']), 1)
        self.assertTrue(any(note.startswith(YEAR_ASSUMED) for note in task['uncertainties']))

    def test_assumed_year_note_is_dropped_when_no_time_is_used(self):
        text = '线上申请时间：5月29日—6月3日'
        task = extract(text, candidate('线上申请', text, ['原文未给出年份，仅有月日范围'], time_quote='5月29日—6月3日'),
                       reference_date='2025-05-01')['candidates'][0]
        self.assertEqual(task['deadline_at'], None)
        self.assertFalse(any(note.startswith(YEAR_ASSUMED) for note in task['uncertainties']))
        self.assertIn('原文未给出年份，仅有月日范围', task['uncertainties'])

    def test_date_only_span_keeps_one_date_only_reminder(self):
        text = '现场办理时间：6月4日—6月6日'
        notes = extract(text, candidate('现场办理', text, time_quote='6月4日—6月6日'), reference_date='2025-05-01')['candidates'][0]['uncertainties']
        self.assertEqual(sum('只有日期' in note or DATE_ONLY in note for note in notes), 1)
        self.assertTrue(any('时间原文已保留' in note for note in notes))

    def test_long_model_note_mentioning_year_with_other_facts_is_kept(self):
        note = '原文未写明年份，且未说明本科新生与研究生新生分别应使用哪一份填报表，需要分别核对适用表格与提交入口。'
        text = '请于9月15日下午5点前提交学生证信息。'
        task = extract(text, candidate('提交信息', '请于9月15日下午5点前提交学生证信息', [note], time_quote='9月15日下午5点前'),
                       reference_date='2025-09-01')['candidates'][0]
        self.assertIn(note, task['uncertainties'])


SUPPLEMENT_NOTE = '由程序依据原文补出'


class PendingTimeActivityTests(TestCase):
    TEXT = ('报名截止：3月26日（周三）下午3:00前\n'
            '讲座时间：待定（根据报名人数确定，预计本周四下午）\n'
            '请扫描下方二维码入群。')

    def test_omitted_tbd_lecture_becomes_an_untimed_fixed_candidate(self):
        result = extract(self.TEXT, candidate('报名', '报名截止：3月26日（周三）下午3:00前'),
                         candidate('入群', '请扫描下方二维码入群。'), reference_date='2025-03-24')
        self.assertEqual(len(result['candidates']), 3)
        task = result['candidates'][-1]
        self.assertEqual((task['title'], task['kind']), ('参加讲座（时间待定）', 'fixed'))
        self.assertEqual((task['deadline_at'], task['fixed_start_at'], task['fixed_end_at'], task['visit_windows']), (None, None, None, []))
        self.assertTrue(any(SUPPLEMENT_NOTE in note for note in task['uncertainties']))
        quote = next(item['quote'] for item in task['evidence'] if item['field'] == 'title')
        self.assertIn(quote, self.TEXT)
        self.assertIn('讲座时间：待定', quote)

    def test_no_supplement_when_a_candidate_already_quotes_the_tbd_sentence(self):
        result = extract(self.TEXT, candidate('参加讲座', '讲座时间：待定（根据报名人数确定，预计本周四下午）'))
        self.assertEqual(len(result['candidates']), 1)

    def test_tbd_pickup_or_non_activity_is_not_turned_into_an_event(self):
        for text in ['办理完成后的学生证领取时间另行通知。', '后期再组织补拍，时间、地点另行通知。', '截止时间待定。']:
            with self.subTest(text=text):
                self.assertEqual(extract(text, candidate('核对', text))['candidates'][0]['title'], '核对')
                self.assertEqual(len(extract(text, candidate('核对', text))['candidates']), 1)

    def test_supplement_also_applies_when_the_model_returns_nothing(self):
        result = extract('期末考试时间另行通知，请留意。')
        self.assertEqual([task['title'] for task in result['candidates']], ['参加考试（时间待定）'])

    def test_supplement_never_exceeds_the_candidate_limit(self):
        text = '\n'.join(f'事项{index}。' for index in range(40)) + '\n讲座时间：待定。'
        result = extract(text, *(candidate(f'事项{index}', f'事项{index}。') for index in range(40)))
        self.assertEqual(len(result['candidates']), 40)
        self.assertTrue(any('时间待定' in note for note in result['warnings']))


FOLD_NOTE = '到场要求原文'


def fixed(title, action, time_quote=None, location=None):
    item = candidate(title, action, time_quote=time_quote)
    item.update(kind='fixed', location_quote=location)
    return item


class AttendanceRequirementTests(TestCase):
    TEXT = ('讲座时间：2025年9月8日14:00\n线下：M234\n'
            '线下参加：开始须签到，结束须签退，以完整的考勤作为参加一次活动的记录。\n'
            '请同学们提前十分钟入场，不要迟到。\n本次讲座报名截止时间：2025年9月8日10:00')

    def main(self):
        return fixed('参加讲座', '讲座时间：2025年9月8日14:00', '2025年9月8日14:00', 'M234')

    def test_untimed_sign_in_and_entry_items_fold_into_the_single_timed_event(self):
        result = extract(self.TEXT, candidate('报名', '本次讲座报名截止时间：2025年9月8日10:00', time_quote='2025年9月8日10:00'),
                         self.main(), fixed('签到签退', '线下参加：开始须签到，结束须签退，以完整的考勤作为参加一次活动的记录。'),
                         fixed('提前十分钟入场', '请同学们提前十分钟入场，不要迟到。'), reference_date='2025-09-01')
        self.assertEqual([task['title'] for task in result['candidates']], ['报名', '参加讲座'])
        notes = [note for note in result['candidates'][1]['uncertainties'] if FOLD_NOTE in note]
        self.assertEqual(len(notes), 2)
        self.assertTrue(any('开始须签到' in note and '「签到签退」' in note for note in notes))
        self.assertTrue(any('提前十分钟入场' in note for note in notes))
        self.assertEqual(result['candidates'][1]['fixed_start_at'], '2025-09-08T14:00:00+08:00')

    def test_no_fold_without_exactly_one_timed_fixed_event(self):
        aux = fixed('签到', '线下参加：开始须签到，结束须签退，以完整的考勤作为参加一次活动的记录。')
        self.assertEqual(len(extract(self.TEXT, aux)['candidates']), 1)
        text = self.TEXT + '\n分会场时间：2025年9月9日14:00'
        second = fixed('参加分会场', '分会场时间：2025年9月9日14:00', '2025年9月9日14:00')
        result = extract(text, self.main(), second, aux, reference_date='2025-09-01')
        self.assertEqual(len(result['candidates']), 3)

    def test_items_with_their_own_time_place_or_real_action_are_kept(self):
        text = self.TEXT + '\n请于9月7日前完成签到报名。\n签到地点：图书馆一楼。'
        for aux in [fixed('签到报名', '请于9月7日前完成签到报名。'),
                    fixed('到图书馆签到', '签到地点：图书馆一楼。', location='图书馆一楼'),
                    candidate('签到', '线下参加：开始须签到，结束须签退，以完整的考勤作为参加一次活动的记录。')]:
            with self.subTest(title=aux['title']):
                result = extract(text, self.main(), aux, reference_date='2025-09-01')
                self.assertEqual(len(result['candidates']), 2)


class FrontendCaveatContractTests(TestCase):
    def test_frontend_filters_exactly_the_backend_review_warning(self):
        from pathlib import Path
        import re
        from campus_agent.candidates import REVIEW_WARNING
        script = (Path(__file__).resolve().parents[2] / 'campus-action-agent/model-candidates.js').read_text(encoding='utf-8')
        self.assertEqual(re.search(r"export const REVIEW_WARNING = '([^']+)'", script).group(1), REVIEW_WARNING)
