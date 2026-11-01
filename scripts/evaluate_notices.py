"""Run frozen development checks with authored or actual local model outputs.

Fixture mode does not call a model. Live mode sends only labelled synthetic
notices through the already-configured loopback service, one attempt per case.
It never reads credentials or browser state and never retries a failed call.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'campus-action-agent-back'))
from campus_agent.candidates import normalize_extraction
from campus_agent.evaluation import load_suite, score_case
from campus_agent.snapshot import calculate_snapshot

MAX_BODY = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def local_base(value):
    parsed = urlsplit(value)
    try:
        valid_host = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname).is_loopback
        valid_port = parsed.port is not None
    except (ValueError, TypeError):
        valid_host = valid_port = False
    if parsed.scheme != 'http' or not valid_host or not valid_port or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Live evaluation requires a plain loopback HTTP service URL with a port.')
    return value.rstrip('/')


def request_json(base, path, body=None):
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    request = Request(base + path, data=raw, headers={'Content-Type': 'application/json'})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=110) as response:
            data = response.read(MAX_BODY + 1)
        if len(data) > MAX_BODY:
            return None, 'response_too_large'
        return json.loads(data), None
    except HTTPError as error:
        # Only fixed public error codes from our service, never upstream bodies.
        status = error.code
        try:
            payload = json.loads(error.read(16384))
            code = payload.get('code')
            safe_codes = {'model_disabled', 'model_not_configured', 'model_auth_rejected', 'model_invalid_extraction', 'model_timeout', 'model_upstream_rate_limited'}
            code = code if code in safe_codes else f'http_{status}'
        except (ValueError, TypeError, AttributeError):
            code = f'http_{status}'
        finally:
            error.close()
        return None, code
    except (URLError, TimeoutError, OSError, ValueError):
        return None, 'connection_or_json_error'


def attach_draft_checks(records, node):
    accepted = [item for item in records if 'result' in item]
    payload = [{'id': item['id'], 'text': item['notice_text'], 'result': item['result']} for item in accepted]
    run = subprocess.run([node, str(ROOT / 'campus-action-agent/scripts/evaluation-drafts.mjs')],
                         input=json.dumps(payload), text=True, capture_output=True, check=True, timeout=30)
    outputs = json.loads(run.stdout)
    if [item['id'] for item in outputs] != [item['id'] for item in accepted]:
        raise ValueError('Draft adapter returned incomplete or reordered results.')
    for record, output in zip(accepted, outputs):
        passed = output['accepted']
        if passed:
            snapshot = output['snapshot']
            before = json.dumps(snapshot)
            result = calculate_snapshot(snapshot)
            passed = (json.dumps(snapshot) == before and result['revision'] == snapshot['revision']
                      and result['analyzed_task_ids'] == [] and 'tasks' not in result
                      and all(task['status'] == 'draft' and task['confirmed_fields'] == []
                              and task['priority'] is None and task['must_do'] is None
                              and (task['kind'] != 'flexible' or task['remaining_minutes'] is None)
                              for task in snapshot['tasks']))
        record['draft_gate_passed'] = passed
        record['case_passed'] = record['rule_check']['rule_passed'] and passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('fixture', 'live'), default='fixture')
    parser.add_argument('--suite', type=Path, default=ROOT / 'dify/evaluation/notice-challenges.v2.json')
    parser.add_argument('--base', default='http://127.0.0.1:8765')
    parser.add_argument('--node', default=shutil.which('node'))
    parser.add_argument('--max-calls', type=int, default=12)
    parser.add_argument('--min-interval', type=float, default=11)
    parser.add_argument('--case', action='append', default=[], help='Restrict to these frozen IDs; repeat for multiple cases.')
    parser.add_argument('--require-version', help='In live mode, do not call the model if the backend version differs.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.node:
        parser.error('Node is required; no skipped-success results.')
    if args.output.exists():
        parser.error('Output already exists; select a new path to preserve previous evidence.')
    if not 1 <= args.max_calls <= 12 or not 11 <= args.min_interval <= 60:
        parser.error('Use 1..12 maximum calls and an interval of 11..60 seconds.')
    suite = load_suite(args.suite)
    selected = suite['cases'] if args.mode == 'fixture' else [case for case in suite['cases'] if case['live_smoke']][:args.max_calls]
    if args.case:
        requested = set(args.case)
        if requested - {case['id'] for case in suite['cases']}:
            parser.error('Unknown case ID; no calls were made.')
        selected = [case for case in suite['cases'] if case['id'] in requested]
        if args.mode == 'live' and len(selected) > args.max_calls:
            parser.error('Selected cases exceed --max-calls; no calls were made.')
    if args.mode == 'live':
        try:
            base = local_base(args.base)
        except ValueError as error:
            parser.error(str(error))
    report = {'label': 'Synthetic development checks, not independent accuracy or browser verification',
              'started_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
              'mode': args.mode, 'suite_sha256': hashlib.sha256(args.suite.read_bytes()).hexdigest(),
              'prompt_file_sha256': hashlib.sha256((ROOT / 'dify/phase4-extraction-system.txt').read_bytes()).hexdigest(),
              'prompt_hash_scope': 'Local source only; deployed prompt contents are not independently verified',
              'normalizer_file_sha256': hashlib.sha256((ROOT / 'campus-action-agent-back/campus_agent/candidates.py').read_bytes()).hexdigest(),
              'live_code_scope': 'Running local service may require restart after code edits; this run does not claim hot reload',
              'synthetic': True, 'independent_test_set': False, 'browser_verified': False,
              'selected_count': len(selected), 'model_call_attempts': 0, 'automatic_retries': 0,
              'status': 'running', 'records': [], 'manual_review_required': suite['manual_review_required']}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + '.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(args.output)

    save()
    if args.mode == 'live':
        health, error = request_json(base, '/api/health')
        if error or not isinstance(health, dict) or health.get('model_extraction') != {'configured': True, 'enabled': True}:
            report.update(status='not_run', stop_reason='model_service_not_ready')
            save(); print('Model service not ready; attempted 0 model calls.', flush=True); return 2
        report['backend_version'] = health.get('version')
        if args.require_version and health.get('version') != args.require_version:
            report.update(status='not_run', stop_reason='backend_version_mismatch')
            save(); print('Backend version mismatch; attempted 0 model calls.', flush=True); return 2
    last_start = None
    for case in selected:
        inputs = case['inputs']
        record = {'id': case['id'], 'purpose': case['purpose'], 'notice_text': inputs['notice_text'],
                  'reference_date': inputs['reference_date'], 'case_passed': False}
        start = time.monotonic()
        if args.mode == 'fixture':
            try:
                result = normalize_extraction(case['gold_extraction'], inputs['notice_text'], inputs['reference_date'] or None, inputs['timezone'])
                error = None
            except ValueError:
                result, error = None, 'gold_normalization_rejected'
        else:
            if last_start is not None:
                time.sleep(max(0, args.min_interval - (time.monotonic() - last_start)))
            last_start = start = time.monotonic()
            report['model_call_attempts'] += 1
            result, error = request_json(base, '/api/v2/extract', {'text': inputs['notice_text'], 'reference_date': inputs['reference_date'] or None, 'timezone': inputs['timezone']})
        record['elapsed_seconds'] = round(time.monotonic() - start, 3)
        if error:
            record['error'] = error
        else:
            record['result'] = result
            record['rule_check'] = score_case(case, result)
        report['records'].append(record)
        save()
        print(f"{case['id']}: {error or ('field rules passed' if record['rule_check']['rule_passed'] else 'field rules failed')}", flush=True)
        if args.mode == 'live' and error in {'model_disabled', 'model_not_configured', 'model_auth_rejected', 'model_timeout', 'model_upstream_rate_limited', 'http_429', 'connection_or_json_error'}:
            report['stop_reason'] = error
            break
    attach_draft_checks(report['records'], args.node)
    report['status'] = 'completed' if len(report['records']) == len(selected) else 'stopped'
    report['summary'] = {'selected': len(selected), 'attempted_cases': len(report['records']),
                         'successful_responses': sum('result' in item for item in report['records']),
                         'returned_candidate_count': sum(len(item['result'].get('candidates', [])) for item in report['records'] if isinstance(item.get('result'), dict) and isinstance(item['result'].get('candidates'), list)),
                         'case_passed': sum(item['case_passed'] for item in report['records']),
                         'case_failed': sum(not item['case_passed'] for item in report['records']),
                         'not_run': len(selected) - len(report['records']),
                         'draft_gate_passed': sum(item.get('draft_gate_passed', False) for item in report['records'])}
    save(); print(json.dumps(report['summary'], ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'completed' and report['summary']['case_failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
