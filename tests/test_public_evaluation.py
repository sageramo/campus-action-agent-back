"""Protect the new public review evidence and catch unsafe scoring shortcuts."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

from campus_agent.candidates import normalize_extraction

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, 'path', [str(ROOT / 'campus-action-agent-back/scripts'), *sys.path]):
    spec = importlib.util.spec_from_file_location('sixu_public_evaluation', ROOT / 'campus-action-agent-back/scripts/evaluate_public_notices.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
SUITE = runner.load_public_suite(runner.SUITE)


def response(case):
    # Authored scorer input, not a model response or an extraction accuracy claim.
    return {'candidates': [dict(kind=fact['kinds'][0], **deepcopy(fact['fields']),
                               evidence=[{'field': 'title', 'origin': 'source_text', 'quote': fact['action_any'][0]}],
                               uncertainties=[group[0] for group in fact.get('notes_any_groups', [])],
                               materials=fact.get('materials_required', [])) for fact in case['checks']['required_actions']]}


class PublicEvaluationTests(TestCase):
    def load_changed(self, mutate):
        data = deepcopy(SUITE)
        mutate(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'suite.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            return runner.load_public_suite(path)

    def test_inputs_are_public_and_not_authored_model_predictions(self):
        self.assertEqual(len(SUITE['cases']), 8)
        self.assertTrue(all('gold_extraction' not in case for case in SUITE['cases']))
        self.assertTrue(all(case['source']['excerpt_scope'] for case in SUITE['cases']))

    def test_private_relabelled_or_unfrozen_data_is_rejected(self):
        for mutate in [lambda d: d.update(source_type='user_provided'),
                       lambda d: d.update(independent_accuracy_benchmark=True),
                       lambda d: d['cases'][0]['inputs'].update(text='Changed text'),
                       lambda d: d['cases'][0]['source'].update(url='https://127.0.0.1/private'),
                       lambda d: d['cases'][0]['source'].update(url='https://www.szu.edu.cn.evil.test/'),
                       lambda d: d['cases'][0]['source'].update(url='https://key@www.szu.edu.cn/')]:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.load_changed(mutate)

    def test_unknown_fields_and_non_source_action_anchors_are_rejected(self):
        for mutate in [lambda d: d['cases'][0]['checks']['required_actions'][0]['fields'].update(priority=50),
                       lambda d: d['cases'][0]['checks']['required_actions'][0].update(action_any=['Not in source'])]:
            with self.assertRaises(ValueError):
                self.load_changed(mutate)

    def test_matching_fields_pass_but_wrong_or_fabricated_times_fail(self):
        case = SUITE['cases'][1]
        self.assertTrue(runner.score_public_case(case, response(case))['passed'])
        for value in [None, '2025-09-08T10:00:00', '2025-09-08T10:00:00Z', 'invalid']:
            result = response(case)
            result['candidates'][0]['deadline_at'] = value
            self.assertFalse(runner.score_public_case(case, result)['passed'])
        result = response(case)
        result['candidates'][1]['fixed_end_at'] = '2025-09-08T16:00:00+08:00'
        self.assertFalse(runner.score_public_case(case, result)['passed'])

    def test_same_instant_offset_is_accepted(self):
        case = SUITE['cases'][1]
        result = response(case)
        result['candidates'][0]['deadline_at'] = '2025-09-08T02:00:00Z'
        self.assertTrue(runner.score_public_case(case, result)['passed'])

    def test_duplicate_source_action_cannot_cover_distinct_obligations(self):
        case = SUITE['cases'][0]
        result = response(case)
        result['candidates'][1] = deepcopy(result['candidates'][0])
        scored = runner.score_public_case(case, result)
        self.assertFalse(scored['passed'])
        self.assertEqual(scored['matched_required_actions'], 1)

    def test_past_review_with_dates_cannot_be_accepted_as_an_action(self):
        case = SUITE['cases'][5]
        self.assertTrue(runner.score_public_case(case, {'candidates': []})['passed'])
        self.assertFalse(runner.score_public_case(case, {'candidates': [{}]})['passed'])

    def test_conditions_and_materials_are_not_satisfied_by_generic_warning(self):
        for index in [3, 6, 7]:
            case = SUITE['cases'][index]
            result = response(case)
            self.assertTrue(runner.score_public_case(case, result)['passed'])
            for candidate in result['candidates']:
                candidate.update(uncertainties=['需要核对'], materials=[])
            self.assertFalse(runner.score_public_case(case, result)['passed'])

    def test_malformed_response_is_not_success(self):
        for result in [None, {}, {'candidates': 'not an array'}, {'candidates': [None]}]:
            self.assertFalse(runner.score_public_case(SUITE['cases'][0], result)['passed'])

    def test_without_send_flag_cli_validates_only_and_does_not_create_results(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'live.json'
            run = subprocess.run([sys.executable, str(Path(runner.__file__)), '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['model_calls'], 0)
            self.assertFalse(output.exists())

    def test_existing_evidence_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'live.json'
            output.write_text('original record')
            run = subprocess.run([sys.executable, str(Path(runner.__file__)), '--output', str(output)], capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(output.read_text(), 'original record')

    def test_real_draft_adapter_blocks_fixed_work_and_unsupported_windows(self):
        records = []
        for case in json.loads((ROOT / 'dify/evaluation/notice-challenges.v2.json').read_text())['cases'][:4]:
            inputs = case['inputs']
            records.append({'id': case['id'], 'notice_text': inputs['notice_text'],
                            'result': normalize_extraction(case['gold_extraction'], inputs['notice_text'], inputs['reference_date'] or None)})
        runner.check_planning_gate(records, '/Users/Amo/Developer/nodejs/current/bin/node', '2026-10-29T10:00:00+08:00')
        self.assertTrue(all(record['planning_draft_gate_passed'] for record in records))
