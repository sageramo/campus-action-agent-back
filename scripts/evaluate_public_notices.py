"""One-shot, source-labelled public excerpt checks, never user browser data.

The suite contains no authored model answers. Freeze its bytes before the first
live call. A convenience sample and an agent-written rubric are not an unbiased
accuracy benchmark; manually review alternative action decompositions afterwards.
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
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from evaluate_notices import ROOT, local_base, request_json
from review_user_notices import check_drafts
from campus_agent.candidates import validate_extraction_input
from campus_agent.planner import plan_snapshot


SUITE = ROOT / 'dify/evaluation/public-notices.20261029.json'
ALLOWED_FIELDS = {'deadline_at', 'fixed_start_at', 'fixed_end_at', 'visit_windows', 'location'}


def load_public_suite(path):
    suite = json.loads(path.read_text(encoding='utf-8'))
    if (suite.get('schema_version') != 1 or suite.get('source_type') != 'public_university_excerpt'
            or suite.get('purpose') != 'frozen_first_run_review' or suite.get('independent_accuracy_benchmark') is not False):
        raise ValueError('Use only the explicitly labelled public excerpt suite.')
    cases = suite.get('cases')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise ValueError('Use 1..12 public cases.')
    ids = set()
    for case in cases:
        case_id = case.get('id')
        if not isinstance(case_id, str) or not re.fullmatch(r'P[0-9]{2}', case_id) or case_id in ids:
            raise ValueError('Each public case needs a unique P00-style ID.')
        ids.add(case_id)
        source = case['source']
        parsed = urlsplit(source['url'])
        if (parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith(('.edu.cn', '.ac.cn'))
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError('A public university/institute HTTPS source URL is required.')
        if not source.get('title') or not source.get('excerpt_scope') or not source.get('verified_on'):
            raise ValueError('Source attribution and excerpt limits are required.')
        payload = case['inputs']
        text, _, _ = validate_extraction_input(payload)
        if hashlib.sha256(text.encode()).hexdigest() != case['input_sha256']:
            raise ValueError('Public excerpt bytes changed; freeze a new suite explicitly.')
        low, high = case['checks']['candidate_count_range']
        if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 40:
            raise ValueError('Invalid candidate count bounds.')
        required = case['checks']['required_actions']
        if len(required) > high or not case.get('manual_review'):
            raise ValueError('Manual semantics review is required even when field checks pass.')
        for fact in required:
            if not fact['action_any'] or any(anchor not in text for anchor in fact['action_any']):
                raise ValueError('Expected action anchors must occur in the source excerpt.')
            if set(fact['fields']) - ALLOWED_FIELDS or not set(fact['kinds']) <= {'fixed', 'flexible', 'visit_window', 'milestone'}:
                raise ValueError('Unknown field or kind in the frozen checks.')
    return suite


def same_value(field, actual, expected):
    if field.endswith('_at'):
        def instant(value):
            if value is None:
                return None
            parsed = datetime.fromisoformat(value)
            if parsed.utcoffset() is None:
                raise ValueError('Explicit timezone required.')
            return parsed
        return instant(actual) == instant(expected)
    if field == 'visit_windows':
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(same_value('start_at', a['start'], b['start']) and same_value('end_at', a['end'], b['end'])
                        for a, b in zip(actual, expected)))
    return actual == expected


def score_public_case(case, result):
    candidates = result.get('candidates') if isinstance(result, dict) else None
    if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
        return {'passed': False, 'issues': [{'rule': 'response_shape'}], 'matched_required_actions': 0}
    checks = case['checks']
    low, high = checks['candidate_count_range']
    issues = [] if low <= len(candidates) <= high else [{'rule': 'candidate_count_range', 'actual': len(candidates)}]
    facts = checks['required_actions']
    edges = []
    for fact in facts:
        edges.append([index for index, candidate in enumerate(candidates)
                      if any(anchor in evidence.get('quote', '')
                             for evidence in candidate.get('evidence', []) if isinstance(evidence, dict)
                             and evidence.get('field') == 'title' and evidence.get('origin') == 'source_text'
                             and isinstance(evidence.get('quote'), str)
                             for anchor in fact['action_any'])])
    owner = {}

    def assign(fact_index, visited):
        for index in edges[fact_index]:
            if index not in visited:
                visited.add(index)
                if index not in owner or assign(owner[index], visited):
                    owner[index] = fact_index
                    return True
        return False

    for index in range(len(facts)):
        if not assign(index, set()):
            issues.append({'rule': 'missing_required_action', 'action': facts[index]['id']})
    for index, fact_index in sorted(owner.items()):
        candidate, fact = candidates[index], facts[fact_index]
        if candidate.get('kind') not in fact['kinds']:
            issues.append({'rule': 'kind', 'action': fact['id'], 'candidate_index': index})
        for field, expected in fact['fields'].items():
            try:
                correct = field in candidate and same_value(field, candidate[field], expected)
            except (ValueError, TypeError, KeyError):
                correct = False
            if not correct:
                issues.append({'rule': field, 'action': fact['id'], 'candidate_index': index})
        notes = '\n'.join(note for note in candidate.get('uncertainties', []) if isinstance(note, str))
        for alternatives in fact.get('notes_any_groups', []):
            if not any(anchor in notes for anchor in alternatives):
                issues.append({'rule': 'condition_note', 'action': fact['id'], 'candidate_index': index})
        materials = candidate.get('materials', [])
        for anchor in fact.get('materials_required', []):
            if not any(isinstance(item, str) and anchor in item for item in materials):
                issues.append({'rule': 'required_material', 'action': fact['id'], 'candidate_index': index})
    return {'passed': not issues, 'candidate_count': len(candidates), 'matched_required_actions': len(owner),
            'issues': issues, 'scope': 'Frozen targeted fields only; semantic review still required'}


def check_planning_gate(records, node, reference_time):
    successful = [record for record in records if 'result' in record]
    payload = [{'id': item['id'], 'text': item['notice_text'], 'result': item['result'],
                'source_kind': 'pasted_notice', 'reference_time': reference_time} for item in successful]
    run = subprocess.run([node, str(ROOT / 'campus-action-agent/scripts/evaluation-drafts.mjs')],
                         input=json.dumps(payload), text=True, capture_output=True, check=True, timeout=30)
    drafts = json.loads(run.stdout)
    if [item['id'] for item in drafts] != [item['id'] for item in successful]:
        raise ValueError('Incomplete planning gate handoff.')
    for record, draft in zip(successful, drafts):
        passed = draft['accepted']
        if passed:
            snapshot = draft['snapshot']
            before = json.dumps(snapshot)
            result = plan_snapshot(snapshot)
            passed = (json.dumps(snapshot) == before and result['schedule'] == []
                      and (result['status'] in {'information_needed', 'unsupported'}
                           and any(issue['code'] == 'draft' for issue in result['issues'])
                           if snapshot['tasks'] else result['status'] == 'nothing_to_schedule'))
        record['planning_draft_gate_passed'] = passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--send-public-excerpts', action='store_true')
    parser.add_argument('--base', default='http://127.0.0.1:8765')
    parser.add_argument('--require-version', default='1.5')
    parser.add_argument('--node', default=shutil.which('node'))
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Keep previous evidence: output already exists.')
    suite = load_public_suite(args.suite)
    digest = hashlib.sha256(args.suite.read_bytes()).hexdigest()
    if not args.send_public_excerpts:
        print(json.dumps({'status': 'validated_only', 'case_count': len(suite['cases']), 'suite_sha256': digest, 'model_calls': 0}))
        return 0
    if not args.node:
        parser.error('Node is required; no skipped-success handoff.')
    base = local_base(args.base)
    health, error = request_json(base, '/api/health')
    if (error or not isinstance(health, dict) or health.get('version') != args.require_version
            or health.get('model_extraction') != {'configured': True, 'enabled': True}):
        print('Required configured model service is not ready; attempted 0 model calls.')
        return 2
    report = {'source_type': suite['source_type'], 'independent_accuracy_benchmark': False,
              'started_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
              'suite_sha256': digest, 'backend_version': health['version'],
              'deployed_prompt_independently_verified': False, 'browser_verified': False,
              'selected_count': len(suite['cases']), 'client_model_call_attempts': 0,
              'automatic_client_retries': 0, 'status': 'running', 'records': []}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temp = args.output.with_suffix(args.output.suffix + '.tmp')
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temp.replace(args.output)

    save()
    previous = None
    for case in suite['cases']:
        # Frozen bytes must still match before each outbound request.
        if hashlib.sha256(args.suite.read_bytes()).hexdigest() != digest:
            report['stop_reason'] = 'suite_changed_during_run'
            break
        if previous is not None:
            time.sleep(max(0, 11 - (time.monotonic() - previous)))
        previous = time.monotonic()
        report['client_model_call_attempts'] += 1
        result, error = request_json(base, '/api/v2/extract', case['inputs'])
        record = {'id': case['id'], 'source_url': case['source']['url'], **{'notice_text': case['inputs']['text']},
                  'reference_date': case['inputs'].get('reference_date'), 'input_sha256': case['input_sha256'],
                  'elapsed_seconds': round(time.monotonic() - previous, 3), 'manual_review_status': 'pending'}
        if error:
            record['error'] = error
        else:
            record['result'] = result
            record['targeted_checks'] = score_public_case(case, result)
        report['records'].append(record)
        save()
        print(f"{case['id']}: {error or ('targeted checks passed' if record['targeted_checks']['passed'] else 'targeted checks failed')}", flush=True)
        if error:
            report['stop_reason'] = error
            break
    check_drafts(report['records'], args.node, report['started_at'])
    check_planning_gate(report['records'], args.node, report['started_at'])
    report['status'] = 'collected_pending_manual_review' if len(report['records']) == len(suite['cases']) and not report.get('stop_reason') else 'stopped_or_incomplete'
    report['summary'] = {'successful_responses': sum('result' in item for item in report['records']),
                         'targeted_checks_passed': sum(item.get('targeted_checks', {}).get('passed', False) for item in report['records']),
                         'draft_gate_passed': sum(item.get('draft_gate_passed', False) for item in report['records']),
                         'planning_draft_gate_passed': sum(item.get('planning_draft_gate_passed', False) for item in report['records']),
                         'not_run': len(suite['cases']) - len(report['records'])}
    save()
    print(json.dumps(report['summary'], ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'collected_pending_manual_review' else 1


if __name__ == '__main__':
    raise SystemExit(main())
