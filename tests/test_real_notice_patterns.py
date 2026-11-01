"""Synthetic regressions for structures found in user notices, no personal text."""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from campus_agent.candidates import normalize_extraction
from campus_agent.time_parser import extract_time_candidates

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, 'path', [str(ROOT / 'campus-action-agent-back/scripts'), *sys.path]):
    spec = importlib.util.spec_from_file_location('sixu_user_notice_review', ROOT / 'campus-action-agent-back/scripts/review_user_notices.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)


def raw(action, quote=None, kind='flexible'):
    return {'schema_version': 1, 'candidates': [{'title': '核对事项', 'kind': kind, 'action_quote': action,
            'time_quote': quote, 'location_quote': None, 'materials_quotes': [], 'uncertainties': []}], 'warnings': []}


class NoticePatternTests(unittest.TestCase):
    def test_day_only_uses_explicit_publication_month_and_never_the_machine_day(self):
        quote = '15号晚22：00'
        candidate = normalize_extraction(raw(quote, quote), quote, '2031-03-14')['candidates'][0]
        self.assertEqual(candidate['deadline_at'], '2031-03-15T22:00:00+08:00')
        self.assertTrue(any('2031-03-14' in note and '2031-03-15' in note for note in candidate['uncertainties']))

    def test_short_evening_and_morning_periods_keep_their_meaning(self):
        for quote, expected in [('15号晚8点', '20:00'), ('15日早9点', '09:00')]:
            with self.subTest(quote=quote):
                points = extract_time_candidates(quote, reference_date='2031-03-14')
                self.assertEqual(points[0].value, f'2031-03-15 {expected}')

    def test_earliest_and_latest_are_not_mistaken_for_morning_or_evening(self):
        for quote, expected in [('15号最晚9点', '09:00'), ('15号最早13:00', '13:00')]:
            self.assertEqual(extract_time_candidates(quote, reference_date='2031-03-14')[0].value, f'2031-03-15 {expected}')

    def test_day_only_without_reference_is_not_a_full_date(self):
        notes = []
        self.assertEqual(extract_time_candidates('15号晚8点', warnings=notes), [])
        self.assertTrue(any('reference_date' in note for note in notes))

    def test_bare_day_does_not_guess_a_month_rollover_or_repair_an_invalid_date(self):
        for quote, reference in [('1号晚8点', '2031-03-31'), ('31号晚8点', '2031-11-14'), ('29号晚8点', '2031-02-14')]:
            with self.subTest(quote=quote, reference=reference):
                self.assertEqual(extract_time_candidates(quote, reference_date=reference), [])

    def test_building_number_is_not_a_bare_day(self):
        points = extract_time_candidates('15号楼102室，3月16日13:30开会', reference_date='2031-03-14')
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].value, '2031-03-16 13:30')

    def test_ambiguous_two_month_context_does_not_assign_a_reference_month_silently(self):
        notes = []
        points = extract_time_candidates('4月16日13:30及15号22:00', reference_date='2031-03-14', warnings=notes)
        self.assertTrue(any('归属尚不支持' in note for note in notes))
        self.assertFalse(any(point.value == '2031-03-15 22:00' for point in points))

    def test_fixed_start_is_retained_without_a_fabricated_end(self):
        quote = '3月16日(周日)下午1:30'
        text = quote + '，请报名同学准时出席。'
        candidate = normalize_extraction(raw('请报名同学准时出席', quote, 'fixed'), text, '2031-03-14')['candidates'][0]
        self.assertEqual(candidate['fixed_start_at'], '2031-03-16T13:30:00+08:00')
        self.assertIsNone(candidate['fixed_end_at'])
        self.assertTrue(any('结束时间仍未知' in note for note in candidate['uncertainties']))
        self.assertEqual({item['field'] for item in candidate['evidence'] if item['field'].startswith('fixed_')}, {'fixed_start_at'})

    def test_weekday_annotation_is_checked_instead_of_becoming_an_unsupported_relative_day(self):
        notes = []
        points = extract_time_candidates('2031年3月16日(周日)下午1:30', warnings=notes)
        self.assertEqual(points[0].value, '2031-03-16 13:30')
        self.assertFalse(any('尚不支持' in note for note in notes))
        notes = []
        self.assertEqual(extract_time_candidates('2031年3月16日(周一)下午1:30', warnings=notes), [])
        self.assertTrue(any('星期不一致' in note for note in notes))

    def test_registration_action_is_not_exempted_as_a_registered_attendee(self):
        quote = '2031年3月16日13:30至15:00'
        text = quote + '报名后参加交流会。'
        candidate = normalize_extraction(raw(text, quote, 'fixed'), text)['candidates'][0]
        self.assertIsNone(candidate['fixed_start_at'])
        self.assertIsNone(candidate['fixed_end_at'])

    def test_a_deadline_mislabelled_fixed_does_not_become_a_known_start(self):
        for quote in ['2031年3月16日13:30前', '截止2031年3月16日13:30']:
            text = quote + '完成准备。'
            candidate = normalize_extraction(raw(text, quote, 'fixed'), text)['candidates'][0]
            self.assertIsNone(candidate['fixed_start_at'])
            self.assertIsNone(candidate['fixed_end_at'])

    def test_explicit_role_gets_a_reminder_without_becoming_a_user_decision(self):
        text = '各班学习委员收集回执，整理后提交给负责人。'
        result = normalize_extraction(raw(text), text)
        candidate = result['candidates'][0]
        self.assertTrue(any('各班学习委员' in note and '职责' in note for note in candidate['uncertainties']))
        self.assertNotIn('must_do', candidate)
        self.assertNotIn('priority', candidate)
        other = normalize_extraction(raw('整理后提交给负责人'), '整理后提交给负责人')['candidates'][0]
        self.assertFalse(any('执行对象' in note for note in other['uncertainties']))

    def test_global_participation_restriction_is_preserved_as_source_not_assumed_identity(self):
        text = '本次活动仅限23、24级研究生参加。请阅读安排。'
        result = normalize_extraction(raw('请阅读安排'), text)
        self.assertTrue(any('仅限23、24级研究生参加' in note for note in result['warnings']))
        self.assertEqual(result['parsing_status'], 'needs_confirmation')

    def test_role_scope_is_still_visible_when_a_short_action_quote_omits_the_subject(self):
        text = '各班学习委员收集回执，整理后提交给负责人。'
        result = normalize_extraction(raw('整理后提交给负责人'), text)
        self.assertTrue(any('各班学习委员' in note and '指定执行对象' in note for note in result['warnings']))
        self.assertFalse(any('执行对象原文' in note for note in result['candidates'][0]['uncertainties']))

    def test_many_scope_lines_are_explicitly_bounded_for_the_existing_frontend(self):
        text = '\n'.join(f'本次活动仅限第{index}组学生参加。' for index in range(30))
        result = normalize_extraction({'schema_version': 1, 'candidates': [], 'warnings': ['原始提醒' + str(index) for index in range(20)]}, text)
        self.assertLessEqual(len(result['warnings']), 40)
        self.assertTrue(any('未全部展开' in note for note in result['warnings']))


class RealNoticeCollectorTests(unittest.TestCase):
    def manifest(self):
        return {'source_type': 'user_provided', 'purpose': 'manual_notice_review',
                'cases': [{'id': 'U01', 'text': '测试通知原文。', 'reference_date': None}]}

    def test_validation_only_never_sends_the_notice_or_creates_a_result_file(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / 'inputs.json', Path(folder) / 'results.json'
            source.write_text(json.dumps(self.manifest()))
            args = ['review_user_notices.py', '--inputs', str(source), '--output', str(output)]
            with patch.object(sys, 'argv', args), patch.object(runner, 'request_json') as request, redirect_stdout(StringIO()):
                self.assertEqual(runner.main(), 0)
                request.assert_not_called()
            self.assertFalse(output.exists())

    def test_malformed_or_mislabelled_manifests_are_rejected(self):
        bad = [[], {'source_type': 'synthetic'}, self.manifest(), self.manifest()]
        bad[2]['cases'][0]['id'] = None
        bad[3]['cases'].append(deepcopy(bad[3]['cases'][0]))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'
            for data in bad:
                path.write_text(json.dumps(data))
                with self.subTest(data=data), self.assertRaises(ValueError):
                    runner.load_notices(path)


if __name__ == '__main__':
    unittest.main()
