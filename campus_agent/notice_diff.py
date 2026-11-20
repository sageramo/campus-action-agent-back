"""Campus notice change tracking & incremental re-plan (Notice Diff, v3).

Detects whether a newly pasted notice updates or cancels existing active events/tasks
in the user's v3 snapshot, computes field-level diffs, and runs a read-only preview
recalculation via `calendar_snapshot` so the user sees exact conflict/free-time impacts
before confirming the patch.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import re
from typing import Any

from .candidates import validate_extraction_input
from .dify_client import extract_notice
from .layer_import import items_from_candidates, items_from_rules
from .layers import calendar_snapshot, validate_snapshot_v3
from .personal import with_personal
from .snapshot import _instant
from .time_utils import get_zone

FILLER_RE = re.compile(
    r"(?:参加|举行|举办|召开|开展|提交|上传|完成|关于|原定(?:于)?|调整后(?:的)?|变更后(?:的)?|新的|最新|本次|此次|"
    r"改为|改到|调整至|延期至|延后至|顺延至|变更为|通知|提醒|紧急|重要|时间|截止|地点)"
)
PUNCT_RE = re.compile(r"[\s，,。；;：:！？!?（）()【】\[\]「」『』“”\"'—–\-~～·、]+")
CANCEL_CUE_RE = re.compile(r"(?:取消|停办|作废|不再举行|不再举办|暂停举行|免交|无需提交|不用交)")
UPDATE_CUE_RE = re.compile(r"(?:改为|改到|改期|调整(?:至|到|为)|延期(?:至|到)?|延后(?:至|到)?|顺延(?:至|到)?|变更(?:为)?|推迟(?:至|到)?|提前(?:至|到)?|原定)")
MATCH_THRESHOLD = 0.45


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _default_range(snapshot: dict) -> dict[str, str]:
    ref = _instant(snapshot["reference_time"], "reference_time").astimezone(get_zone()).date()
    monday = ref - timedelta(days=ref.weekday())
    return {"from": monday.isoformat(), "to": (monday + timedelta(days=6)).isoformat()}


def _clean_title(title: str) -> str:
    cleaned = FILLER_RE.sub("", str(title or ""))
    return PUNCT_RE.sub("", cleaned).lower()


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _title_similarity(a: str, b: str) -> float:
    ca, cb = _clean_title(a), _clean_title(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    if ca in cb or cb in ca:
        shorter, longer = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
        return max(0.7, len(shorter) / len(longer))
    ba, bb = _bigrams(ca), _bigrams(cb)
    inter = len(ba & bb)
    return (2.0 * inter) / (len(ba) + len(bb)) if (ba or bb) else 0.0


def _best_match(incoming_title: str, existing_items: list[dict], used_ids: set[str]) -> tuple[dict | None, float]:
    best_item = None
    best_score = 0.0
    for item in existing_items:
        if item["id"] in used_ids or item.get("status") != "active":
            continue
        score = _title_similarity(incoming_title, item["title"])
        if score > best_score:
            best_score = score
            best_item = item
    if best_item is not None and best_score >= MATCH_THRESHOLD:
        return best_item, round(best_score, 3)
    return None, 0.0


def _field_changes(old: dict, new: dict, fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for field in fields:
        new_val = new.get(field)
        old_val = old.get(field)
        if new_val is not None and new_val != old_val:
            changes[field] = {"from": old_val, "to": new_val}
    return changes


def _net_free_minutes(cal: dict) -> int:
    free = sum(_minutes(_instant(f["start"], "s"), _instant(f["end"], "e")) for f in cal["layers"]["free"])
    work = sum(
        _minutes(_instant(w["start"], "s"), _instant(w["end"], "e"))
        for w in cal["layers"]["work"]
        if w.get("kind") != "appointment"
    )
    return free - work


def _detect_cancelled_existing(text: str, existing_events: list[dict], existing_tasks: list[dict], matched_ids: set[str]) -> list[dict]:
    """Detect explicit cancellation sentences referencing an existing active event or task."""
    if not CANCEL_CUE_RE.search(text):
        return []
    cancels: list[dict] = []
    sentences = [s.strip() for s in re.split(r"[。；;！？!?\n]+", text) if s.strip()]
    for sent in sentences:
        if not CANCEL_CUE_RE.search(sent):
            continue
        clean_sent = _clean_title(sent)
        for kind, pool in (("event", existing_events), ("task", existing_tasks)):
            for item in pool:
                if item["id"] in matched_ids or item.get("status") != "active":
                    continue
                core = _clean_title(item["title"])
                if len(core) >= 3 and (core in clean_sent or _title_similarity(sent, item["title"]) >= MATCH_THRESHOLD):
                    matched_ids.add(item["id"])
                    cancels.append({
                        "kind": kind,
                        "action": "cancel",
                        "target_id": item["id"],
                        "target_title": item["title"],
                        "confidence": 0.9,
                        "quote": sent,
                        "changes": {"status": {"from": "active", "to": "cancelled"}},
                    })
    return cancels


def diff_notice(payload: object) -> dict[str, Any]:
    """POST /api/v3/notice-diff: match revised/cancelled notice against v3 snapshot and preview recalc."""
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise ValueError("请求必须包含 v3 snapshot 对象")
    snapshot = payload["snapshot"]
    validate_snapshot_v3(snapshot)
    extract_req = {k: v for k, v in payload.items() if k in {"text", "reference_date", "timezone"}}
    text, reference_date, _ = validate_extraction_input(extract_req)
    mode = payload.get("mode", "rules")
    if mode not in {"rules", "model"}:
        raise ValueError("mode 只能是 rules 或 model")
    date_range = payload.get("range") or _default_range(snapshot)

    if mode == "rules":
        extracted = with_personal(text, reference_date, lambda rest: items_from_rules(rest, reference_date))
    else:
        extracted = with_personal(
            text,
            reference_date,
            lambda rest: items_from_candidates(extract_notice({**extract_req, "text": rest}), rest, reference_date),
        )

    is_revision_notice = bool(UPDATE_CUE_RE.search(text) or CANCEL_CUE_RE.search(text))
    matched_ids: set[str] = set()
    operations: list[dict[str, Any]] = []

    # 1. First catch explicit cancellation sentences that might have no timestamp (so extractor gave a dateless task).
    explicit_cancels = _detect_cancelled_existing(text, snapshot["events"], snapshot["tasks"], matched_ids)
    operations.extend(explicit_cancels)

    # 2. Match extracted events
    for ev in extracted["events"]:
        target, score = _best_match(ev["title"], snapshot["events"], matched_ids)
        if target is not None:
            matched_ids.add(target["id"])
            changes = _field_changes(target, ev, ("start_at", "end_at", "location"))
            operations.append({
                "kind": "event",
                "action": "update" if changes else "unchanged",
                "target_id": target["id"],
                "target_title": target["title"],
                "incoming": ev,
                "confidence": score,
                "changes": changes,
            })
        else:
            operations.append({
                "kind": "event",
                "action": "add",
                "target_id": None,
                "target_title": None,
                "incoming": ev,
                "confidence": 0.0,
                "changes": {},
            })

    # 3. Match extracted tasks (skip bogus dateless placeholder task when text was purely an explicit cancellation)
    for task in extracted["tasks"]:
        if explicit_cancels and task["deadline_at"] is None and CANCEL_CUE_RE.search(task["title"]):
            continue
        target, score = _best_match(task["title"], snapshot["tasks"], matched_ids)
        if target is not None:
            matched_ids.add(target["id"])
            changes = _field_changes(target, task, ("deadline_at", "earliest_start_at", "location"))
            operations.append({
                "kind": "task",
                "action": "update" if changes else "unchanged",
                "target_id": target["id"],
                "target_title": target["title"],
                "incoming": task,
                "confidence": score,
                "changes": changes,
            })
        else:
            operations.append({
                "kind": "task",
                "action": "add",
                "target_id": None,
                "target_title": None,
                "incoming": task,
                "confidence": 0.0,
                "changes": {},
            })

    # 4. Build detached preview snapshot & compute before/after calendar impact
    preview = deepcopy(snapshot)
    preview["revision"] = int(preview.get("revision", 0)) + 1
    if not any(s["id"] == extracted["source"]["id"] for s in preview["sources"]):
        preview["sources"].append(extracted["source"])

    ev_by_id = {ev["id"]: ev for ev in preview["events"]}
    tk_by_id = {tk["id"]: tk for tk in preview["tasks"]}
    for op in operations:
        if op["action"] == "cancel":
            pool = ev_by_id if op["kind"] == "event" else tk_by_id
            if op["target_id"] in pool:
                pool[op["target_id"]]["status"] = "cancelled"
        elif op["action"] == "update":
            pool = ev_by_id if op["kind"] == "event" else tk_by_id
            target = pool.get(op["target_id"])
            if target:
                for field, diff in op["changes"].items():
                    target[field] = diff["to"]
                # Append source evidence trace from the revision notice
                for ev_item in op["incoming"].get("evidence", []):
                    if ev_item["field"] in op["changes"]:
                        target["evidence"].append(ev_item)
        elif op["action"] == "add":
            if op["kind"] == "event":
                preview["events"].append(op["incoming"])
            else:
                preview["tasks"].append(op["incoming"])

    cal_before = calendar_snapshot({"snapshot": snapshot, "range": date_range})
    cal_after = calendar_snapshot({"snapshot": preview, "range": date_range})

    before_work = {(w.get("task_id") or w.get("id")): (w["start"], w["end"]) for w in cal_before["layers"]["work"]}
    after_work = {(w.get("task_id") or w.get("id")): (w["start"], w["end"]) for w in cal_after["layers"]["work"]}
    work_shifts = [
        {"id": tid, "from": before_work.get(tid), "to": after_work.get(tid)}
        for tid in sorted(set(before_work) | set(after_work))
        if before_work.get(tid) != after_work.get(tid)
    ]

    counts = {
        "updated": sum(1 for op in operations if op["action"] == "update"),
        "cancelled": sum(1 for op in operations if op["action"] == "cancel"),
        "added": sum(1 for op in operations if op["action"] == "add"),
        "unchanged": sum(1 for op in operations if op["action"] == "unchanged"),
    }

    return {
        "schema_version": 1,
        "mode": extracted["mode"],
        "is_revision_notice": is_revision_notice,
        "range": date_range,
        "source": extracted["source"],
        "summary": counts,
        "operations": operations,
        "impact": {
            "conflicts_before": len(cal_before["conflicts"]),
            "conflicts_after": len(cal_after["conflicts"]),
            "conflict_delta": len(cal_after["conflicts"]) - len(cal_before["conflicts"]),
            "free_delta_minutes": _net_free_minutes(cal_after) - _net_free_minutes(cal_before),
            "work_shifts": work_shifts,
            "validation_passed": cal_after["validation"]["passed"],
        },
        "preview_snapshot": preview,
    }
