"""Collect explicitly supplied real notices for manual semantic review.

This separate opt-in path does not relabel user data as synthetic or weaken the
synthetic runner's guard. It neither opens registration links nor changes a user
snapshot. Full notices/results should be written outside a public repository.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from evaluate_notices import ROOT, local_base, request_json
from campus_agent.candidates import validate_extraction_input
from campus_agent.snapshot import calculate_snapshot


def load_notices(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('source_type') != 'user_provided' or data.get('purpose') != 'manual_notice_review':
        raise ValueError('Real notices must be explicitly labelled for manual review.')
    cases = data.get('cases')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 6:
        raise ValueError('Use 1..6 explicitly supplied notices.')
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', case['id']) or case['id'] in ids:
            raise ValueError('Each case requires a unique plain ID.')
        ids.add(case['id'])
        validate_extraction_input({'text': case.get('text'), 'reference_date': case.get('reference_date'), 'timezone': 'Asia/Shanghai'})
    return data


def check_drafts(records, node, reference_time):
    successful = [record for record in records if 'result' in record]
    inputs = [{'id': record['id'], 'text': record['notice_text'], 'result': record['result'],
               'source_kind': 'pasted_notice', 'reference_time': reference_time} for record in successful]
    run = subprocess.run([node, str(ROOT / 'campus-action-agent/scripts/evaluation-drafts.mjs')],
                         input=json.dumps(inputs), text=True, capture_output=True, check=True, timeout=30)
    drafts = json.loads(run.stdout)
    if [record['id'] for record in drafts] != [record['id'] for record in successful]:
        raise ValueError('Incomplete draft handoff.')
    for record, draft in zip(successful, drafts):
        passed = draft['accepted']
        if passed:
            snapshot = draft['snapshot']
            before = json.dumps(snapshot)
            result = calculate_snapshot(snapshot)
            passed = (json.dumps(snapshot) == before and result['analyzed_task_ids'] == [] and 'tasks' not in result
                      and all(source['kind'] == 'pasted_notice' and source['published_at'] is None for source in snapshot['sources'])
                      and all(task['status'] == 'draft' and not task['confirmed_fields']
                              and task['priority'] is None and task['must_do'] is None for task in snapshot['tasks']))
        record['draft_gate_passed'] = passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--send-to-configured-model', action='store_true', help='Explicitly opt in to sending these supplied notices.')
    parser.add_argument('--base', default='http://127.0.0.1:8765')
    parser.add_argument('--node', default=shutil.which('node'))
    parser.add_argument('--require-version', default='1.4')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Select a new output path; previous evidence will not be overwritten.')
    try:
        data = load_notices(args.inputs)
        base = local_base(args.base)
    except ValueError as error:
        parser.error(str(error))
    if not args.send_to_configured_model:
        print(json.dumps({'validated_notice_count': len(data['cases']), 'model_calls': 0, 'status': 'validated_only'}, ensure_ascii=False))
        return 0
    if not args.node:
        parser.error('Node is required for draft handoff validation.')
    health, error = request_json(base, '/api/health')
    if error or not isinstance(health, dict) or health.get('version') != args.require_version or health.get('model_extraction') != {'configured': True, 'enabled': True}:
        print('Configured backend version not ready; no model calls made.')
        return 2
    report = {'source_type': 'user_provided', 'synthetic': False, 'independent_test_set': False,
              'semantic_scoring': 'Manual decomposition and condition review; no automatic accuracy score',
              'started_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
              'inputs_sha256': hashlib.sha256(args.inputs.read_bytes()).hexdigest(), 'backend_version': health['version'],
              'selected_count': len(data['cases']), 'client_model_call_attempts': 0, 'automatic_client_retries': 0,
              'browser_state_accessed': False, 'registration_links_opened': False, 'status': 'running', 'records': []}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + '.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(args.output)

    save()
    previous = None
    for case in data['cases']:
        if previous is not None:
            time.sleep(max(0, 11 - (time.monotonic() - previous)))
        previous = time.monotonic()
        report['client_model_call_attempts'] += 1
        result, error = request_json(base, '/api/v2/extract', {'text': case['text'], 'reference_date': case.get('reference_date'), 'timezone': 'Asia/Shanghai'})
        record = {'id': case['id'], 'notice_text': case['text'], 'reference_date': case.get('reference_date'),
                  'elapsed_seconds': round(time.monotonic() - previous, 3), 'manual_review_status': 'pending'}
        if error:
            record['error'] = error
        else:
            record['result'] = result
        report['records'].append(record)
        save()
        print(f"{case['id']}: {error or 'response collected; semantic review pending'}", flush=True)
        if error:
            break
    check_drafts(report['records'], args.node, report['started_at'])
    report['status'] = 'collected_pending_manual_review' if len(report['records']) == len(data['cases']) and all(record.get('draft_gate_passed') for record in report['records']) else 'collection_failed_or_incomplete'
    report['summary'] = {'attempted': len(report['records']), 'successful_responses': sum('result' in record for record in report['records']),
                         'draft_gate_passed': sum(record.get('draft_gate_passed', False) for record in report['records']),
                         'not_run': len(data['cases']) - len(report['records'])}
    save(); print(json.dumps(report['summary'], ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'collected_pending_manual_review' else 1


if __name__ == '__main__':
    raise SystemExit(main())
