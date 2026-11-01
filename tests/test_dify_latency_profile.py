"""A comparison profile must not change the extraction task or validation."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('sixu_latency_profile', ROOT / 'dify/build_phase10_workflow.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
spec = importlib.util.spec_from_file_location('sixu_profile_contract_check', ROOT / 'campus-action-agent-back/scripts/check_dify_workflow.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class LatencyProfileTests(TestCase):
    def test_only_thinking_flag_changes_and_original_export_is_not_mutated(self):
        baseline = json.loads(builder.BASELINE.read_text())
        before = deepcopy(baseline)
        profile = builder.build_profile(baseline)
        llm = next(node['data'] for node in profile['workflow']['graph']['nodes'] if node['data']['type'] == 'llm')
        self.assertIs(llm['model']['completion_params'].pop('thinking'), False)
        self.assertEqual(profile, before)
        self.assertEqual(baseline, before)

    def test_output_keeps_strict_contract_and_reasoning_output_separation(self):
        profile = builder.build_profile(json.loads(builder.BASELINE.read_text()))
        schema = json.loads(checker.SCHEMA_PATH.read_text())
        self.assertEqual(checker.check_workflow(profile, schema), [])
        llm = next(node['data'] for node in profile['workflow']['graph']['nodes'] if node['data']['type'] == 'llm')
        self.assertEqual(llm['reasoning_format'], 'separated')
        self.assertFalse(llm['retry_config']['retry_enabled'])
        self.assertNotIn('max_tokens', llm['model']['completion_params'])

    def test_wrong_provider_or_additional_llm_is_not_silently_configured(self):
        baseline = json.loads(builder.BASELINE.read_text())
        llm = next(node for node in baseline['workflow']['graph']['nodes'] if node['data']['type'] == 'llm')
        llm['data']['model']['provider'] = 'another-provider'
        with self.assertRaises(ValueError):
            builder.build_profile(baseline)
        baseline = json.loads(builder.BASELINE.read_text())
        baseline['workflow']['graph']['nodes'].append(deepcopy(llm))
        with self.assertRaises(ValueError):
            builder.build_profile(baseline)

    def test_committed_comparison_profile_is_generated_from_immutable_baseline(self):
        expected = json.dumps(builder.build_profile(json.loads(builder.BASELINE.read_text())), ensure_ascii=False, indent=2) + '\n'
        self.assertEqual(builder.OUTPUT.read_text(), expected)
