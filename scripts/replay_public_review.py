"""Offline regression reconstructed from normalized public API responses.

The upstream model's raw JSON is unavailable here. Reconstruct only its preserved
quotes/fields, label that reconstruction, and never describe it as a second live
model run. Prior outputs and the frozen rubric remain unchanged.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

from evaluate_public_notices import SUITE, load_public_suite, score_public_case, check_planning_gate
from review_user_notices import check_drafts
from campus_agent.candidates import normalize_extraction


def reconstruct(result):
    output = {'schema_version': 1, 'candidates': [], 'warnings': []}
    for candidate in result['candidates']:
        evidence = candidate['evidence']
        def quote(field):
            return next((item['quote'] for item in evidence if item['field'] == field), None)
        time_fields = {'fixed': ['fixed_start_at', 'fixed_end_at'], 'visit_window': ['visit_windows']}.get(candidate['kind'], ['deadline_at'])
        output['candidates'].append({'title': candidate['title'], 'kind': candidate['kind'],
                                     'action_quote': quote('title'), 'time_quote': next((quote(field) for field in time_fields if quote(field)), None),
                                     'location_quote': candidate['location'],
                                     'materials_quotes': [item['quote'] for item in evidence if item['field'] == 'materials'],
                                     'uncertainties': candidate['uncertainties']})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--node', default=shutil.which('node'))
    args = parser.parse_args()
    if args.output.exists() or not args.node:
        parser.error('Use a new output path and a working Node runtime.')
    suite = load_public_suite(SUITE)
    original = json.loads(args.input.read_text())
    if original['suite_sha256'] != hashlib.sha256(SUITE.read_bytes()).hexdigest():
        parser.error('The original frozen suite bytes do not match.')
    cases = {case['id']: case for case in suite['cases']}
    records = []
    for record in original['records']:
        if 'result' not in record:
            records.append({'id': record['id'], 'error': 'no_original_response'})
            continue
        case = cases[record['id']]
        result = normalize_extraction(reconstruct(record['result']), **{'text': case['inputs']['text'],
                                      'reference_date': case['inputs']['reference_date'], 'timezone': case['inputs']['timezone']})
        records.append({'id': record['id'], 'notice_text': case['inputs']['text'], 'result': result,
                        'targeted_checks': score_public_case(case, result)})
    reference = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    check_drafts(records, args.node, reference)
    check_planning_gate(records, args.node, reference)
    report = {'mode': 'offline_reconstructed_regression', 'model_calls': 0,
              'reconstructed_input_is_raw_model_output': False, 'first_run_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest(),
              'suite_sha256': original['suite_sha256'], 'created_at': reference, 'records': records,
              'summary': {'case_count': len(records), 'targeted_checks_passed': sum(item.get('targeted_checks', {}).get('passed', False) for item in records),
                          'draft_gate_passed': sum(item.get('draft_gate_passed', False) for item in records),
                          'planning_draft_gate_passed': sum(item.get('planning_draft_gate_passed', False) for item in records)}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report['summary']))
    return 0 if all(item.get('draft_gate_passed') and item.get('planning_draft_gate_passed') for item in records) else 1


if __name__ == '__main__':
    raise SystemExit(main())
