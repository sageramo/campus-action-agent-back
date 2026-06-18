from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .conflict import detect_conflicts
from .data_loader import DATA_DIR, dataset_summary, load_conflict_cases, load_multitime_samples, parse_sample_documents
from .extractor import parse_notice_text


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web" / "deploy"


def write_cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Max-Age", "86400")


def json_response(handler: BaseHTTPRequestHandler, data: object, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    write_cors(handler)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class CampusAgentHandler(BaseHTTPRequestHandler):
    server_version = "CampusAgent/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print("[server]", self.address_string(), "-", fmt % args)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        write_cors(self)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/health":
            return json_response(self, {"ok": True, "name": "口述行动链 API", "version": "1.0"})
        if path == "/api/summary":
            return json_response(self, dataset_summary(DATA_DIR))
        if path == "/api/multitime":
            return json_response(self, load_multitime_samples(DATA_DIR))
        if path == "/api/conflict-cases":
            return json_response(self, load_conflict_cases(DATA_DIR))
        if path == "/api/analyze-case":
            case_id = query.get("id", [""])[0]
            cases = load_conflict_cases(DATA_DIR).get("cases", [])
            case = next((item for item in cases if item.get("case_id") == case_id), cases[0] if cases else None)
            if not case:
                return json_response(self, {"error": "no case found"}, 404)
            return json_response(self, {"case": case, "analysis": detect_conflicts(case.get("tasks", []), now=case.get("scenario_period", {}).get("start"))})
        if path == "/api/parsed-samples":
            limit = int(query.get("limit", ["10"])[0])
            return json_response(self, parse_sample_documents(DATA_DIR, limit=limit))
        return self.serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="ignore") if length else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return json_response(self, {"error": "invalid json body"}, 400)
        if parsed.path == "/api/parse":
            text = str(payload.get("text", ""))
            if not text.strip():
                return json_response(self, {"error": "text is required"}, 400)
            task = parse_notice_text(text, source="用户输入")
            return json_response(self, task.to_dict())
        if parsed.path == "/api/conflicts":
            tasks = payload.get("tasks", [])
            if not isinstance(tasks, list):
                return json_response(self, {"error": "tasks must be a list"}, 400)
            now = payload.get("now")
            return json_response(self, detect_conflicts(tasks, now=now))
        return json_response(self, {"error": "unknown endpoint"}, 404)

    def serve_static(self, path: str) -> None:
        if path in {"/", ""}:
            file_path = WEB_DIR / "index.html"
        else:
            file_path = (WEB_DIR / path.lstrip("/")).resolve()
            if WEB_DIR.resolve() not in file_path.parents and file_path != WEB_DIR.resolve():
                return json_response(self, {"error": "forbidden"}, 403)
        if not file_path.exists() or not file_path.is_file():
            return json_response(self, {"error": "not found"}, 404)
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        write_cors(self)
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        else:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host: str | None = None, port: int | None = None) -> None:
    host = host or os.environ.get("HOST", "127.0.0.1")
    port = port or int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer((host, port), CampusAgentHandler)
    print(f"Campus Agent API running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
