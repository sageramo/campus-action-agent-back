"""Fake Dify HTTP -> normalization -> real Node draft adapter -> v2 calculator.

The fixtures are authored expected extractions, not live model predictions.
This checks the software handoff, not browser behavior or extraction accuracy.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'campus-action-agent-back'))
from campus_agent.dify_client import extract_notice
from campus_agent.snapshot import calculate_snapshot


class FakeWorkflow(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path == '/v1/workflows/run'
        assert request['inputs'] == self.server.case['inputs']
        self.server.call_count += 1
        body = json.dumps({'data': {'status': 'succeeded', 'outputs': {
            'extraction': json.dumps(self.server.case['expected'], ensure_ascii=False)
        }}}, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default=shutil.which('node'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not args.node:
        parser.error('Node is required; use --node to select it (no skipped success).')
    cases = json.loads((ROOT / 'dify/phase4-evaluation-cases.json').read_text())['cases']
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), FakeWorkflow)
    upstream.call_count = 0
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    records = []
    try:
        config = {'DIFY_API_BASE': f'http://127.0.0.1:{upstream.server_port}/v1',
                  'DIFY_API_KEY': 'app-synthetic-test-key-only', 'SIXU_MODEL_EXTRACTION_ENABLED': 'true',
                  'DIFY_TIMEOUT_SECONDS': '5'}
        with patch.dict(os.environ, config, clear=True):
            for case in cases:
                upstream.case = case
                inputs = case['inputs']
                result = extract_notice({'text': inputs['notice_text'], 'reference_date': inputs['reference_date'] or None, 'timezone': inputs['timezone']})
                records.append({'id': case['id'], 'text': inputs['notice_text'], 'result': result})
    finally:
        upstream.shutdown(); upstream.server_close(); thread.join(5)
    completed = subprocess.run([args.node, str(ROOT / 'campus-action-agent/scripts/model-pipeline-adapter.mjs')],
                               input=json.dumps(records), capture_output=True, text=True, timeout=20, check=True)
    snapshots = json.loads(completed.stdout)
    assert len(snapshots) == len(cases) == upstream.call_count
    for snapshot in snapshots:
        before = json.dumps(snapshot)
        result = calculate_snapshot(snapshot)
        assert json.dumps(snapshot) == before
        assert result['analyzed_task_ids'] == []
        assert 'tasks' not in result
        for task in snapshot['tasks']:
            assert task['status'] == 'draft' and task['confirmed_fields'] == []
            assert task['priority'] is None and task['must_do'] is None
        if snapshot['tasks']:
            assert result['status'] == 'information_needed'
    report = {'passed': True, 'synthetic_case_count': len(cases), 'fake_upstream_calls': upstream.call_count,
              'verification': 'fake_http_normalizer_node_drafts_v2_calculator', 'live_dify_verified': False,
              'live_model_verified': False, 'browser_verified': False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
