"""Reproducible zero-model verification for the shared roadshow demos and v1.25 fixes.

Starts an ephemeral local HTTP backend (rules only, no external model calls), runs:
1. Shared Demo 1: Photography club notice -> 待书 -> +30m estimate -> 叠墨 -> keep course & recalc
   (plus alternate skip-course + rule leave note check, and colon-format notice variant).
2. Shared Demo 2A & 2B: Commitment capacity trial through the real frontend/miniprogram `agenda.js`
   against the live `/api/v3/calendar` endpoint (total shortage + contiguous shortage + splittable
   contrast + read-only snapshot check).
3. Legacy v2 snapshot compatibility (`/api/v2/calculate`, `/api/v2/plan`, and `migrateV2` -> v3).
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading

BACK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACK_ROOT.parent
sys.path.insert(0, str(BACK_ROOT))

from campus_agent import __version__  # noqa: E402
from campus_agent.server import CampusAgentHandler  # noqa: E402

PHOTO_NOTICE = (
    "【摄影社招新通知】\n"
    "2026年11月25日14:30-15:30举行摄影社招新说明会（学活204）。\n"
    "2026年11月25日18:00前提交社团报名表。"
)
PHOTO_NOTICE_COLON = (
    "【摄影社招新通知】\n"
    "招新说明会时间：2026年11月25日14:30-15:30，地点：学活204。\n"
    "说明会报名截止时间：2026年11月25日18:00。"
)
PHOTO_NOTICE_EMDASH = "11月25日（周三）14:30—15:30，摄影社招新说明会，地点：学活204。"
DEMO_WEEK = {"from": "2026-11-23", "to": "2026-11-29"}
REFERENCE_DATE = "2026-11-25"


class _QuietHandler(CampusAgentHandler):
    def log_message(self, *args: object) -> None:
        pass


def _request(port: int, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def _free_after_work(calendar_result: dict, day: str | None = None) -> int:
    from datetime import datetime

    free_mins = 0
    for item in calendar_result["layers"]["free"]:
        if day is None or item["start"].startswith(day):
            free_mins += int((datetime.fromisoformat(item["end"]) - datetime.fromisoformat(item["start"])).total_seconds() // 60)
    work_mins = 0
    for item in calendar_result["layers"]["work"]:
        if day is None or item["start"].startswith(day):
            work_mins += int((datetime.fromisoformat(item["end"]) - datetime.fromisoformat(item["start"])).total_seconds() // 60)
    return free_mins - work_mins


def run_demo1(port: int) -> dict:
    base = json.loads((BACK_ROOT / "examples/roadshow-photo-club.snapshot.json").read_text(encoding="utf-8"))
    status, extracted = _request(port, "POST", "/api/v3/extract", {
        "text": PHOTO_NOTICE, "reference_date": REFERENCE_DATE, "mode": "rules",
    })
    assert status == 200, extracted
    assert len(extracted["events"]) == 1 and len(extracted["tasks"]) == 1, extracted
    assert extracted["events"][0]["title"] == "摄影社招新说明会", extracted["events"][0]
    assert extracted["events"][0]["location"] == "学活204", extracted["events"][0]
    assert extracted["tasks"][0]["title"] == "提交社团报名表", extracted["tasks"][0]

    snap = deepcopy(base)
    snap["revision"] += 1
    snap["sources"].append(extracted["source"])
    snap["events"].extend(extracted["events"])
    snap["tasks"].extend(extracted["tasks"])

    status, cal_pending = _request(port, "POST", "/api/v3/calendar", {"snapshot": snap, "range": DEMO_WEEK})
    assert status == 200, cal_pending
    assert cal_pending["tasks"][0]["state"] == "estimate_undefined"
    assert cal_pending["undefined"] == [], cal_pending["undefined"]
    assert any(m["type"] == "deadline" and m["id"] == snap["tasks"][0]["id"] for m in cal_pending["markers"])
    assert len(cal_pending["conflicts"]) == 1 and cal_pending["conflicts"][0]["minutes"] == 30
    assert cal_pending["layers"]["work"] == []

    snap_est = deepcopy(snap)
    snap_est["revision"] += 1
    snap_est["tasks"][0]["estimate_minutes"] = 30
    status, cal_before = _request(port, "POST", "/api/v3/calendar", {"snapshot": snap_est, "range": DEMO_WEEK})
    assert status == 200, cal_before
    assert cal_before["validation"]["passed"] is True
    assert [(w["start"][11:16], w["end"][11:16]) for w in cal_before["layers"]["work"]] == [("15:30", "16:00")]
    assert _free_after_work(cal_before, "2026-11-25") == 120
    conflict = cal_before["conflicts"][0]
    keep_course = next(s for s in conflict["strategies"] if s["action"] == "cancel_event")
    skip_course = next(s for s in conflict["strategies"] if s["action"] == "skip_routine_date")
    assert skip_course["label"] == "这次不上「专业课」", skip_course
    assert keep_course["label"] == "不去「摄影社招新说明会」", keep_course

    snap_keep = deepcopy(snap_est)
    snap_keep["revision"] += 1
    snap_keep["events"][0]["status"] = "cancelled"
    status, cal_after = _request(port, "POST", "/api/v3/calendar", {"snapshot": snap_keep, "range": DEMO_WEEK})
    assert status == 200, cal_after
    assert cal_after["status"] == "clear" and cal_after["conflicts"] == []
    assert cal_after["validation"]["passed"] is True
    assert [(w["start"][11:16], w["end"][11:16]) for w in cal_after["layers"]["work"]] == [("15:00", "15:30")]
    assert _free_after_work(cal_after, "2026-11-25") == 150
    week_delta = _free_after_work(cal_after) - _free_after_work(cal_before)
    assert week_delta == 30, week_delta

    # Alternate path: skip course once + rule-based leave note
    snap_skip = deepcopy(snap_est)
    snap_skip["revision"] += 1
    snap_skip["routines"][0]["skip_dates"].append(skip_course["params"]["date"])
    status, cal_skip = _request(port, "POST", "/api/v3/calendar", {"snapshot": snap_skip, "range": DEMO_WEEK})
    assert status == 200 and cal_skip["conflicts"] == []
    status, advice = _request(port, "POST", "/api/v3/advise", {
        "mode": "rules",
        "type": conflict["type"],
        "advice": conflict["advice"],
        "items": [
            {"title": conflict["items"][0]["title"], "layer": conflict["items"][0]["layer"],
             "start": conflict["items"][0]["start"], "end": conflict["items"][0]["end"],
             "location": snap_est["events"][0]["location"]},
            {"title": conflict["items"][1]["title"], "layer": conflict["items"][1]["layer"],
             "start": conflict["items"][1]["start"], "end": conflict["items"][1]["end"],
             "location": snap_est["routines"][0]["location"]},
        ],
        "strategies": [{"action": skip_course["action"], "label": skip_course["label"],
                        "detail": skip_course["detail"], "recommended": True}],
    })
    assert status == 200 and advice["basis"] == "rules" and "【姓名】" in (advice["note"] or "")

    # Also verify colon-format and comma/em-dash photo-club variants
    status, colon_ext = _request(port, "POST", "/api/v3/extract", {
        "text": PHOTO_NOTICE_COLON, "reference_date": REFERENCE_DATE, "mode": "rules",
    })
    assert status == 200 and len(colon_ext["events"]) == 1 and len(colon_ext["tasks"]) == 1
    status, emdash_ext = _request(port, "POST", "/api/v3/extract", {
        "text": PHOTO_NOTICE_EMDASH, "reference_date": REFERENCE_DATE, "mode": "rules",
    })
    assert status == 200 and len(emdash_ext["events"]) == 1 and emdash_ext["tasks"] == [], emdash_ext
    assert emdash_ext["events"][0]["title"] == "摄影社招新说明会"
    assert emdash_ext["events"][0]["location"] == "学活204"

    return {
        "notice": PHOTO_NOTICE,
        "reference_date": REFERENCE_DATE,
        "range": DEMO_WEEK,
        "extracted": {
            "event": {"title": extracted["events"][0]["title"], "start_at": extracted["events"][0]["start_at"],
                      "end_at": extracted["events"][0]["end_at"], "location": extracted["events"][0]["location"]},
            "task": {"title": extracted["tasks"][0]["title"], "deadline_at": extracted["tasks"][0]["deadline_at"],
                     "estimate_minutes": extracted["tasks"][0]["estimate_minutes"]},
        },
        "step_pending": {
            "status": cal_pending["status"],
            "task_state": cal_pending["tasks"][0]["state"],
            "undefined_types": [u["type"] for u in cal_pending["undefined"]],
            "conflict_count": len(cal_pending["conflicts"]),
            "overlap_minutes": cal_pending["conflicts"][0]["minutes"],
        },
        "step_with_30m_estimate": {
            "status": cal_before["status"],
            "work_slot": f"{cal_before['layers']['work'][0]['start'][11:16]}-{cal_before['layers']['work'][0]['end'][11:16]}",
            "wed_free_after_work_minutes": _free_after_work(cal_before, "2026-11-25"),
            "week_free_after_work_minutes": _free_after_work(cal_before),
            "conflict_count": len(cal_before["conflicts"]),
            "strategies": [s["label"] for s in conflict["strategies"]],
            "validation_passed": cal_before["validation"]["passed"],
        },
        "step_keep_course_resolved": {
            "applied_strategy": keep_course["label"],
            "status": cal_after["status"],
            "work_slot": f"{cal_after['layers']['work'][0]['start'][11:16]}-{cal_after['layers']['work'][0]['end'][11:16]}",
            "wed_free_after_work_minutes": _free_after_work(cal_after, "2026-11-25"),
            "week_free_after_work_minutes": _free_after_work(cal_after),
            "week_free_delta_minutes": week_delta,
            "conflict_count": len(cal_after["conflicts"]),
            "validation_passed": cal_after["validation"]["passed"],
        },
        "alternate_skip_course": {
            "applied_strategy": skip_course["label"],
            "conflict_count": len(cal_skip["conflicts"]),
            "leave_note_preview": advice["note"],
        },
        "colon_variant_extract": {
            "event_title": colon_ext["events"][0]["title"],
            "task_title": colon_ext["tasks"][0]["title"],
        },
        "emdash_comma_variant_extract": {
            "event_title": emdash_ext["events"][0]["title"],
            "start_at": emdash_ext["events"][0]["start_at"],
            "end_at": emdash_ext["events"][0]["end_at"],
            "location": emdash_ext["events"][0]["location"],
            "task_count": len(emdash_ext["tasks"]),
        },
    }


def run_node_checks(port: int, node_bin: str) -> dict:
    script = f"""
import {{ readFileSync }} from 'node:fs';
import {{ agendaReply }} from '{(REPO_ROOT / "campus-action-agent/agenda.js").as_posix()}';
import {{ migrateV2 }} from '{(REPO_ROOT / "campus-action-agent/time-state.js").as_posix()}';
import {{ parseSnapshot }} from '{(REPO_ROOT / "campus-action-agent/task-state.js").as_posix()}';

const post = async (path, body) => {{
  const res = await fetch(`http://127.0.0.1:{port}${{path}}`, {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(body),
  }});
  const data = await res.json();
  if (!res.ok) throw new Error(`${{res.status}}: ${{JSON.stringify(data)}}`);
  return data;
}};

const now = Date.parse('2026-11-25T14:00:00+08:00');
const capA = JSON.parse(readFileSync('{(BACK_ROOT / "examples/roadshow-capacity-shortage.snapshot.json").as_posix()}', 'utf8'));
const beforeA = JSON.stringify(capA);
const qA = '今天再接1小时社团工作，留半小时休息，时间够吗？';
const rA = await agendaReply(qA, {{ snapshot: capA, now, calculate: body => post('/api/v3/calendar', body) }});

const capB = JSON.parse(readFileSync('{(BACK_ROOT / "examples/roadshow-capacity-fragmented.snapshot.json").as_posix()}', 'utf8'));
const beforeB = JSON.stringify(capB);
const qB1 = '今天还能接5小时的社团工作，留1小时休息，够吗？';
const rB1 = await agendaReply(qB1, {{ snapshot: capB, now, calculate: body => post('/api/v3/calendar', body) }});
const qB2 = '今天还能接5小时的社团工作，可以拆开做，留1小时休息吗？';
const rB2 = await agendaReply(qB2, {{ snapshot: capB, now, calculate: body => post('/api/v3/calendar', body) }});

const v2Path = '{(BACK_ROOT / "examples/nine-minutes.snapshot.json").as_posix()}';
const v2 = parseSnapshot(readFileSync(v2Path, 'utf8'));
const v2Calc = await post('/api/v2/calculate', v2);
const v2Plan = await post('/api/v2/plan', v2);
const v3Mig = migrateV2(v2);
const v3Cal = await post('/api/v3/calendar', {{ snapshot: v3Mig, range: {{ from: '2026-10-26', to: '2026-11-01' }} }});

console.log(JSON.stringify({{
  capacity_case_a: {{
    question: qA, reply: rA.text, snapshot_mutated: JSON.stringify(capA) !== beforeA, writes_next: rA.next !== undefined,
  }},
  capacity_case_b: {{
    unsplit_question: qB1, unsplit_reply: rB1.text,
    split_question: qB2, split_reply: rB2.text,
    snapshot_mutated: JSON.stringify(capB) !== beforeB,
    writes_next: rB1.next !== undefined || rB2.next !== undefined,
  }},
  legacy_v2_compat: {{
    fixture: 'examples/nine-minutes.snapshot.json',
    v2_calculate_status: v2Calc.status,
    v2_plan_status: v2Plan.status,
    v2_to_v3_status: v3Cal.status,
    v2_to_v3_tasks: v3Cal.tasks.map(t => t.state),
  }},
}}, null, 2));
"""
    proc = subprocess.run([node_bin, "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
    data = json.loads(proc.stdout)
    assert "总量还差 20 分钟" in data["capacity_case_a"]["reply"], data["capacity_case_a"]
    assert not data["capacity_case_a"]["snapshot_mutated"] and not data["capacity_case_a"]["writes_next"]
    assert "总量够，但没有连续 5 小时" in data["capacity_case_b"]["unsplit_reply"], data["capacity_case_b"]
    assert "5 小时排得下" in data["capacity_case_b"]["split_reply"], data["capacity_case_b"]
    assert not data["capacity_case_b"]["snapshot_mutated"] and not data["capacity_case_b"]["writes_next"]
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify shared roadshow demos without external model calls.")
    parser.add_argument("--node", default=shutil.which("node") or "node")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        status, health = _request(port, "GET", "/api/health")
        assert status == 200 and health["version"] == __version__
        report = {
            "ok": True,
            "backend_version": __version__,
            "model_calls": 0,
            "demo1_photo_club": run_demo1(port),
            **run_node_checks(port, args.node),
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
