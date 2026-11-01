"""Run local v1 regressions; baseline mode records failures without calling them fixes."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from campus_agent.conflict import detect_conflicts, parse_dt
from campus_agent.extractor import parse_notice_text
from campus_agent.snapshot import calculate_snapshot


def run_adapter(case: dict, node: str | None = None) -> dict:
    payload = case["input"]
    if case["adapter"] == "snapshot":
        result = calculate_snapshot(payload)
        return {
            "available_minutes": [item["available_minutes"] for item in result["capacity_checks"]],
            "known_required_minutes": [item["known_required_minutes"] for item in result["capacity_checks"]],
            "inactive_count": len(result["inactive_task_ids"]),
        }
    if case["adapter"] == "node_revision":
        if not node:
            raise RuntimeError("Node.js is required for browser state regression; use --node to select it.")
        completed = subprocess.run([node, str(ROOT.parent / "campus-action-agent/scripts/regression-adapter.mjs")], input=json.dumps(payload), text=True, capture_output=True, timeout=15, check=True)
        return json.loads(completed.stdout)
    if case["adapter"] == "datetime":
        value = parse_dt(payload["value"])
        return {"normalized": value.strftime("%Y-%m-%d %H:%M") if value else None}
    if case["adapter"] == "conflicts":
        result = detect_conflicts(payload["tasks"], now=payload["now"])
        return {
            "conflict_types": [item["type"] for item in result["conflicts"]],
            "drop_or_delegate": result["recommendations"]["drop_or_delegate"],
        }
    result = parse_notice_text(payload["text"], source="synthetic-regression", reference_date=payload.get("reference_date"), timezone=payload.get("timezone", "Asia/Shanghai")).to_dict()
    # Inspect both structured endpoints; the start remains v1's value field.
    # Expectations in the frozen fixture are unchanged.
    values = [value for item in result["time_points"] for value in [item["value"], item.get("end_value")] if value is not None]
    invalid = []
    for value in values:
        if re.match(r"^\d{4}-\d{2}-\d{2}", value):
            try:
                datetime.fromisoformat(value)
            except ValueError:
                invalid.append(value)
    return {
        "time_values": values,
        "time_types": [{"value": item["value"], "type": item["time_type"]} for item in result["time_points"]],
        "materials": result["materials"],
        "locations": result.get("locations", [result["location"]] if result["location"] else []),
        "invalid_dates": invalid,
    }


def matches(actual: dict, check: dict) -> bool:
    got = actual[check["field"]]
    expected = check["value"]
    operation = check["op"]
    if operation == "equals":
        return got == expected
    if operation == "contains":
        return expected in got
    if operation == "not_contains":
        return expected not in got
    if operation == "contains_fragment":
        return any(expected in item for item in got)
    raise ValueError(f"Unknown check operation: {operation}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="Record known failures/pending items; any execution error still fails.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", type=Path, default=ROOT / "tests/fixtures/phase3-cases.json", help="Current acceptance fixture by default; phase1-cases.json replays the original baseline.")
    parser.add_argument("--node", default=shutil.which("node"), help="Node.js executable for the real browser state module regression.")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    results = []
    for case in cases:
        item = {"id": case["id"], "title": case["title"]}
        if case["adapter"] == "v2_pending":
            item.update(status="not_implemented", requirement=case["requirement"])
        else:
            try:
                actual = run_adapter(case, args.node)
                failed_checks = [check for check in case["checks"] if not matches(actual, check)]
                item.update(status="failed" if failed_checks else "passed", actual=actual, failed_checks=failed_checks)
            except Exception as exc:
                item.update(status="execution_error", error=f"{type(exc).__name__}: {exc}")
        results.append(item)
    summary = {status: sum(item["status"] == status for item in results) for status in ["passed", "failed", "not_implemented", "execution_error"]}
    modules = ["time_parser.py", "time_utils.py", "extractor.py", "conflict.py", "models.py", "server.py", "snapshot.py"]
    report = {
        "label": "开发回归记录；不是独立测试准确率，也不证明完整链路已上线",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "mode": "baseline" if args.baseline else "strict",
        "fixture": str(args.cases.relative_to(ROOT)) if args.cases.is_relative_to(ROOT) else str(args.cases),
        "fixture_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "algorithm_sha256": {name: hashlib.sha256((ROOT / "campus_agent" / name).read_bytes()).hexdigest() for name in modules},
        "browser_state_sha256": {name: hashlib.sha256((ROOT.parent / "campus-action-agent" / name).read_bytes()).hexdigest() for name in ["task-state.js", "snapshot-store.js"]},
        "summary": summary,
        "cases": results,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    for item in results:
        print(f'{item["id"]} {item["status"]}: {item["title"]}')
    if summary["execution_error"]:
        return 2
    if not args.baseline and (summary["failed"] or summary["not_implemented"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
