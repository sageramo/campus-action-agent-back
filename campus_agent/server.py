from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .candidates import validate_extraction_input
from .conflict import detect_conflicts
from .data_loader import DATA_DIR, dataset_summary, load_conflict_cases, load_multitime_samples, parse_sample_documents
from .extractor import parse_notice_text
from .dify_client import ExtractionServiceError, extract_notice, extraction_service_status
from .extraction_access import EXTRACTION_BUDGET, PUBLIC_MODEL_GATE, forwarded_client_ip, local_extraction_allowed
from .snapshot import calculate_snapshot
from .advise import advise
from .agenda_chat import agenda_chat
from .end_estimate import estimate_end
from .layers import calendar_snapshot
from .layer_import import items_from_candidates, items_from_rules
from .personal import with_personal
from .timetable import recognize_timetable
from .deepseek_client import deepseek_status
from .planner import plan_snapshot
from .time_utils import DATE_ONLY


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "deploy"
MAX_BODY_BYTES = 1_048_576
MAX_SNAPSHOT_BODY_BYTES = 15_000_000


def process_post(path: str, payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求JSON必须是对象")
    timezone = payload.get("timezone", "Asia/Shanghai")
    if path == "/api/v2/calculate":
        return calculate_snapshot(payload)
    if path == "/api/v2/plan":
        return plan_snapshot(payload)
    if path == "/api/v2/extract":
        return extract_notice(payload)
    if path == "/api/v3/calendar":
        return calendar_snapshot(payload)
    if path == "/api/v3/timetable":
        return recognize_timetable(payload)
    if path == "/api/v3/estimate-end":
        return estimate_end(payload)
    if path == "/api/v3/chat":
        return agenda_chat(payload)
    if path == "/api/v3/advise":
        return advise(payload)
    if path == "/api/v3/extract":
        request = {key: value for key, value in payload.items() if key != "mode"}
        text, reference_date, _ = validate_extraction_input(request)
        mode = payload.get("mode", "rules")
        if mode == "rules":
            return with_personal(text, reference_date, lambda rest: items_from_rules(rest, reference_date))
        if mode == "model":
            return with_personal(text, reference_date,
                                 lambda rest: items_from_candidates(extract_notice({**request, "text": rest}), rest, reference_date))
        raise ValueError("mode 只能是 rules 或 model")
    if path == "/api/parse":
        return parse_notice_text(payload.get("text"), source="user input", reference_date=payload.get("reference_date"), timezone=timezone).to_dict()
    if path == "/api/conflicts":
        if "now" in payload and payload["now"] is None:
            raise ValueError("now不能显式传null；请提供参照时刻，或省略以使用服务器当前时间")
        if "availability" in payload and payload["availability"] is None:
            raise ValueError("availability不能显式传null；空数组表示没有空闲，省略表示未知")
        return detect_conflicts(payload.get("tasks", []), now=payload.get("now"), availability=payload.get("availability"), timezone=timezone)
    raise ValueError("unknown endpoint")


def write_cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Sixu-Access")
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
    server_version = "Sixu/1.17"

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
        if path in {"/", ""}:
            return json_response(
                self,
                {
                    "ok": True,
                    "name": "司序·知所先后 API",
                    "message": "Backend is running. /api/v2/extract is a separately configured local model service.",
                    "endpoints": ["/api/health", "/api/summary", "/api/multitime", "/api/conflict-cases", "/api/parse", "/api/conflicts", "/api/v2/calculate", "/api/v2/extract", "/api/v2/plan", "/api/v3/calendar", "/api/v3/extract", "/api/v3/timetable", "/api/v3/estimate-end", "/api/v3/advise", "/api/v3/chat"],
                },
            )
        if path == "/api/health":
            return json_response(self, {"ok": True, "name": "司序·知所先后 API", "version": "1.24", "capabilities": ["notice_candidates", "snapshot_v2", "model_notice_candidates_v1", "partial_fixed_start_v1", "bounded_planning_v1", "source_condition_guards_v1", "reminder_consolidation_v1", "date_reminder_folding_v1", "pending_event_supplement_v1", "attendance_requirement_folding_v1", "time_layers_v3", "timetable_v3", "timetable_image_v3", "appointments_v3", "conflict_strategies_v1", "end_estimate_v1", "public_model_access_v1", "strategy_advice_v1", "personal_reminders_v1", "agenda_chat_v1", "agenda_time_validation_v1"], "model_extraction": extraction_service_status(), "timetable_model": deepseek_status(), "model_access": PUBLIC_MODEL_GATE.status()})
        if path == "/api/summary":
            return json_response(self, dataset_summary(DATA_DIR))
        if path == "/api/multitime":
            return json_response(self, load_multitime_samples(DATA_DIR))
        if path == "/api/conflict-cases":
            return json_response(self, load_conflict_cases(DATA_DIR))
        if path == "/api/analyze-case":
            case_id = query.get("id", [""])[0]
            cases = load_conflict_cases(DATA_DIR).get("cases", [])
            case = next((item for item in cases if item.get("case_id") == case_id), None) if case_id else (cases[0] if cases else None)
            if not case:
                return json_response(self, {"error": "no case found"}, 404)
            reference = case.get("scenario_period", {}).get("start")
            assumptions = []
            if isinstance(reference, str) and DATE_ONLY.fullmatch(reference):
                reference += " 00:00"
                assumptions.append("历史演示案例只给起始日期，本接口显式以当日00:00重放；这不是用户当前时刻。")
            try:
                analysis = detect_conflicts(case.get("tasks", []), now=reference)
            except ValueError as exc:
                return json_response(self, {"error": str(exc)}, 400)
            analysis["warnings"].extend(assumptions)
            return json_response(self, {"case": case, "analysis": analysis})
        if path == "/api/parsed-samples":
            try:
                limit = int(query.get("limit", ["10"])[0])
                if not 0 <= limit <= 100:
                    raise ValueError
            except ValueError:
                return json_response(self, {"error": "limit must be an integer from 0 to 100"}, 400)
            return json_response(self, parse_sample_documents(DATA_DIR, limit=limit))
        return self.serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/parse", "/api/conflicts", "/api/v2/calculate", "/api/v2/extract", "/api/v2/plan", "/api/v3/calendar", "/api/v3/extract", "/api/v3/timetable", "/api/v3/estimate-end", "/api/v3/advise", "/api/v3/chat"}:
            return json_response(self, {"error": "unknown endpoint"}, 404)
        budget_acquired = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("Content-Length不得为负数")
            snapshot_request = parsed.path in {"/api/v2/calculate", "/api/v2/plan", "/api/v3/calendar"}
            # Timetable screenshots arrive as base64 and can exceed 1MiB.
            limit = MAX_SNAPSHOT_BODY_BYTES if snapshot_request or parsed.path == "/api/v3/timetable" else MAX_BODY_BYTES
            if length > limit:
                message = "快照超过15,000,000字节，请导出并拆分清单后重试" if snapshot_request else "请求体过大，请缩小图片或分批处理" if parsed.path == "/api/v3/timetable" else "请求体超过1MiB，请分批处理"
                return json_response(self, {"error": message}, 413)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            def reject_constant(value: str) -> None:
                raise ValueError(f"JSON不允许{value}")
            payload = json.loads(raw, parse_constant=reject_constant)
            model_request = parsed.path == "/api/v2/extract" or (parsed.path in {"/api/v3/extract", "/api/v3/timetable", "/api/v3/estimate-end", "/api/v3/advise", "/api/v3/chat"} and isinstance(payload, dict)
                                                                and payload.get("mode", "model" if parsed.path == "/api/v3/timetable" else "rules") in {"model", "image"})
            # The local workbench is trusted; anything else needs the server's access code and stays
            # within the daily caps (extraction_access.PublicModelGate).
            if model_request and not local_extraction_allowed(self.client_address[0], self.headers.get("Origin")):
                client = forwarded_client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"))
                refused = PUBLIC_MODEL_GATE.check(client, self.headers.get("Origin"), self.headers.get("X-Sixu-Access"))
                if refused:
                    message, status, code = refused
                    return json_response(self, {"error": message, "code": code}, status)
            if model_request:
                budget_error = EXTRACTION_BUDGET.acquire()
                if budget_error:
                    return json_response(self, {"error": budget_error, "code": "model_request_limit"}, 429)
                budget_acquired = True
            result = process_post(parsed.path, payload)
        except ExtractionServiceError as exc:
            return json_response(self, {"error": str(exc), "code": exc.code}, exc.status)
        except (ValueError, UnicodeError, RecursionError) as exc:
            return json_response(self, {"error": str(exc)}, 400)
        finally:
            if budget_acquired:
                EXTRACTION_BUDGET.release()
        return json_response(self, result)

    def serve_static(self, path: str) -> None:
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
