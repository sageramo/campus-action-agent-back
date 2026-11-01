"""Development evaluator safeguards and regressions from the first live batch."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campus_agent.candidates import normalize_extraction
from campus_agent.evaluation import load_suite, score_case

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / 'dify/evaluation/notice-challenges.v2.json'
CASES = {case['id']: case for case in load_suite(SUITE)['cases']}
spec = importlib.util.spec_from_file_location('sixu_evaluate_notices', ROOT / 'campus-action-agent-back/scripts/evaluate_notices.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def normalize(case, raw=None):
    inputs = case['inputs']
    return normalize_extraction(raw or case['gold_extraction'], inputs['notice_text'], inputs['reference_date'] or None)


class EvaluationTests(unittest.TestCase):
    def test_all_authored_answers_pass_declared_fact_checks_without_a_model(self):
        self.assertEqual(len(CASES), 24)
        self.assertEqual(sum(case['live_smoke'] for case in CASES.values()), 12)
        for case in CASES.values():
            with self.subTest(case=case['id']):
                self.assertTrue(score_case(case, normalize(case))['rule_passed'])

    def test_generated_title_paraphrase_is_not_an_exact_match_requirement(self):
        result = normalize(CASES['E10'])
        result['candidates'][0]['title'] = '礼堂学术讲座'
        self.assertTrue(score_case(CASES['E10'], result)['rule_passed'])

    def test_wrong_time_or_timezone_is_not_hidden_by_a_correct_title(self):
        for value in ['2026-11-03T14:00:00Z', None, 'not a date']:
            result = normalize(CASES['E10'])
            result['candidates'][0]['fixed_start_at'] = value
            self.assertFalse(score_case(CASES['E10'], result)['rule_passed'])

    def test_equivalent_explicit_offset_is_the_same_instant(self):
        result = normalize(CASES['E10'])
        result['candidates'][0]['fixed_start_at'] = '2026-11-03T06:00:00Z'
        self.assertTrue(score_case(CASES['E10'], result)['rule_passed'])

    def test_extra_or_missing_actions_fail_including_the_no_action_case(self):
        result = normalize(CASES['E10'])
        self.assertFalse(score_case(CASES['E02'], result)['rule_passed'])
        self.assertFalse(score_case(CASES['E10'], {'candidates': []})['rule_passed'])
        result['candidates'].append(deepcopy(result['candidates'][0]))
        self.assertFalse(score_case(CASES['E10'], result)['rule_passed'])

    def test_duplicate_evidence_cannot_satisfy_two_distinct_actions(self):
        result = normalize(CASES['E08'])
        result['candidates'][1] = deepcopy(result['candidates'][0])
        score = score_case(CASES['E08'], result)
        self.assertFalse(score['rule_passed'])
        self.assertEqual(score['matched_actions'], 1)

    def test_location_and_material_misbinding_are_scored_separately(self):
        result = normalize(CASES['E08'])
        result['candidates'][0]['location'] = '团委'
        result['candidates'][0]['materials'].append('社团报名表')
        rules = {issue['rule'] for issue in score_case(CASES['E08'], result)['issues']}
        self.assertIn('location', rules)
        self.assertIn('unexpected_material', rules)

    def test_missing_condition_is_not_satisfied_by_a_generic_review_warning(self):
        result = normalize(CASES['N14'])
        result['candidates'][0]['uncertainties'] = ['原文需要人工核对。']
        self.assertFalse(score_case(CASES['N14'], result)['rule_passed'])

    def test_malformed_response_or_evidence_does_not_get_a_silent_pass(self):
        for result in [None, {}, {'candidates': 'wrong'}, {'candidates': [{'evidence': None}]}]:
            with self.subTest(result=result):
                self.assertFalse(score_case(CASES['E10'], result)['rule_passed'])

    def test_live_target_rejects_remote_hosts_redirect_paths_and_credentials(self):
        for value in ['https://127.0.0.1:8765', 'http://example.com:8765', 'http://127.0.0.1:8765/path',
                      'http://user:secret@127.0.0.1:8765', 'http://127.0.0.1', 'http://127.0.0.1:8765/?key=x']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.local_base(value)
        self.assertEqual(runner.local_base('http://127.0.0.1:8765/'), 'http://127.0.0.1:8765')
        self.assertEqual(runner.local_base('http://localhost:8765'), 'http://localhost:8765')

    def test_existing_evidence_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'existing.json'
            output.write_text('keep this evidence')
            run = subprocess.run([sys.executable, str(ROOT / 'campus-action-agent-back/scripts/evaluate_notices.py'),
                                  '--output', str(output), '--node', 'unused-node'], capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(output.read_text(), 'keep this evidence')


class ObservedNormalizationRegressions(unittest.TestCase):
    def test_registration_qualification_does_not_erase_a_training_interval(self):
        case = CASES['N14']
        raw = deepcopy(case['gold_extraction'])
        raw['candidates'][0].update(action_quote=case['inputs']['notice_text'], uncertainties=[])
        result = normalize(case, raw)
        self.assertTrue(score_case(case, result)['rule_passed'])
        self.assertTrue(any('已报名且审核通过' in note for note in result['candidates'][0]['uncertainties']))

    def test_real_registration_or_submission_action_stays_unresolved_if_mislabelled_fixed(self):
        for text in ['仅已报名的同学，2026年11月5日14:00—16:00提交材料后参加培训。',
                     '2026年11月5日14:00—16:00报名培训。',
                     '2026年11月5日14:00—16:00已经报名。']:
            raw = {'schema_version': 1, 'candidates': [{'title': '核对事项', 'kind': 'fixed', 'action_quote': text,
                   'time_quote': '2026年11月5日14:00—16:00', 'location_quote': None,
                   'materials_quotes': [], 'uncertainties': []}], 'warnings': []}
            candidate = normalize_extraction(raw, text)['candidates'][0]
            self.assertIsNone(candidate['fixed_start_at'])
            self.assertIsNone(candidate['fixed_end_at'])

    def test_received_object_is_not_a_preparation_material_and_quote_is_preserved(self):
        case = CASES['N19']
        raw = deepcopy(case['gold_extraction'])
        raw['candidates'][0]['materials_quotes'] = ['资料']
        candidate = normalize(case, raw)['candidates'][0]
        self.assertEqual(candidate['materials'], [])
        self.assertTrue(any(item['quote'] == '资料' for item in candidate['evidence']))
        self.assertTrue(any('领取或发放对象' in note for note in candidate['uncertainties']))
        self.assertEqual(candidate['visit_windows'], [])

    def test_a_genuine_bring_requirement_is_not_deleted_by_another_receipt_occurrence(self):
        text = '2026年11月5日14:00—16:00领取资料，领取时请携带资料。'
        raw = {'schema_version': 1, 'candidates': [{'title': '领取资料', 'kind': 'visit_window', 'action_quote': text,
               'time_quote': '2026年11月5日14:00—16:00', 'location_quote': None,
               'materials_quotes': ['资料'], 'uncertainties': []}], 'warnings': []}
        self.assertEqual(normalize_extraction(raw, text)['candidates'][0]['materials'], ['资料'])

    def test_shared_year_interval_is_parsed_but_not_unrelated_or_reverse_dates(self):
        self.assertEqual(normalize(CASES['E04'])['candidates'][0]['visit_windows'],
                         [{'start': '2026-11-02T09:00:00+08:00', 'end': '2026-11-04T17:00:00+08:00'}])
        for quote in ['2026年11月2日09:00和11月4日17:00', '11月2日09:00—2026年11月4日17:00',
                      '2026年12月31日23:00至1月1日01:00', '2026年11月2日09:00至明天17:00']:
            raw = {'schema_version': 1, 'candidates': [{'title': '参加讲座', 'kind': 'fixed', 'action_quote': quote,
                   'time_quote': quote, 'location_quote': None, 'materials_quotes': [], 'uncertainties': []}], 'warnings': []}
            candidate = normalize_extraction(raw, quote)['candidates'][0]
            self.assertIsNone(candidate['fixed_start_at'], quote)
            self.assertIsNone(candidate['fixed_end_at'], quote)


if __name__ == '__main__':
    unittest.main()
