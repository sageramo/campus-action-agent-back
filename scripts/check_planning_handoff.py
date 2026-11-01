"""Exercise the actual app with a temporary API, without model or live-key access."""
import argparse
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from campus_agent.server import CampusAgentHandler


class QuietHandler(CampusAgentHandler):
    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch('campus_agent.server.extract_notice', side_effect=AssertionError('Model must never be used')) as model:
            completed = subprocess.run([args.node, str(ROOT.parent / 'campus-action-agent/scripts/check-planning-handoff.mjs'), f'http://127.0.0.1:{server.server_port}'], check=True, capture_output=True, text=True, timeout=30)
            model.assert_not_called()
        result = json.loads(completed.stdout)
        result.update(created_at=datetime.now(timezone.utc).isoformat(), model_calls=0, live_service_touched=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({key: result[key] for key in ('passed', 'backend_version', 'ui_version', 'model_calls')}, ensure_ascii=False))
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


if __name__ == '__main__':
    main()
