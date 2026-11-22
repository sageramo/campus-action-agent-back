"""Closed-loop replanning engine (v3): state change / disruption -> replan preview -> revision-guarded confirmation.

Deterministic core (zero external dependencies, 100% offline):
- Reuses `/api/v3/solve` (`exact_solver.py`), `/api/v3/calendar` (`layers.py`), and `calibration.py`.
- Supports optional focus windows (soft ranking), task cognitive load, and user-locked temporary rest blocks (hard constraints).
- Materializes the chosen plan into the preview snapshot (`earliest_start_at`) and verifies block-for-block equivalence
  with `/api/v3/calendar` both at preview time and at confirm time.
- Compares three deterministic objectives (focus_matched / buffer_first / minimal_change) with moved_minutes metrics,
  explicit omitted_plans explanations, and backend-owned needs_decision / decision_reason flags.
- Enforces optimistic revision, fingerprint, and calendar-replay checks on confirmation (HTTP 409 on stale or mismatched preview).
- DeepSeek is strictly optional (`mode="model"`): used only to draft pending state changes from natural language or
  to generate a tailored 5-minute starter action.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from typing import Any

from .calibration import compute_calibration_profile, kind_of, _round_to_5
from .deepseek_client import chat_json
from .dify_client import ExtractionServiceError
from .exact_solver import (
    LEVEL_CN,
    VALID_LEVELS,
    _default_range,
    _iso,
    _minutes,
    parse_focus_windows,
    parse_rest_blocks,
    parse_task_profiles,
    score_task_focus,
    solve_snapshot,
)
from .layers import calendar_snapshot, validate_snapshot_v3
from .personal import DURATION, _minutes as parse_duration_minutes
from .snapshot import _instant
from .time_parser import CLOCK_RE, RANGE_RE, clock_value
from .time_utils import get_zone


class ReplanConflictError(Exception):
    """Raised when confirming a preview against a stale snapshot or when calendar replay diverges."""

    def __init__(
        self,
        message: str,
        *,
        expected_revision: int,
        current_revision: int,
        code: str = "stale_preview",
        status: int = 409,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details: dict[str, Any] = {
            "expected_revision": expected_revision,
            "current_revision": current_revision,
            "requires_repreview": True,
        }
        if extra:
            self.details.update(extra)


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    """Canonical SHA-256 fingerprint over the schedule-defining state of a v3 snapshot."""
    canonical = {
        "schema_version": snapshot.get("schema_version"),
        "revision": snapshot.get("revision"),
        "reference_time": snapshot.get("reference_time"),
        "daily_window": snapshot.get("daily_window"),
        "routines_confirmed": snapshot.get("routines_confirmed"),
        "routines": snapshot.get("routines"),
        "free_adjustments": snapshot.get("free_adjustments"),
        "events": snapshot.get("events"),
        "tasks": snapshot.get("tasks"),
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:24]


def _canonical_schedule(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [
        {"task_id": e["task_id"], "start": e["start"], "end": e["end"]}
        for e in entries
        if isinstance(e, dict) and e.get("task_id") and e.get("start") and e.get("end")
    ]
    normalized.sort(key=lambda item: (item["task_id"], item["start"], item["end"]))
    return normalized


def _preview_token(
    base_revision: int,
    fingerprint: str,
    plan_id: str,
    preview_snapshot: dict[str, Any],
    expected_schedule: list[dict[str, Any]],
) -> str:
    payload = {
        "base_revision": base_revision,
        "fingerprint": fingerprint,
        "plan_id": plan_id,
        "preview_fingerprint": snapshot_fingerprint(preview_snapshot),
        "expected_schedule": _canonical_schedule(expected_schedule),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "rpv1_" + hashlib.sha256(raw).hexdigest()[:24]


def _fmt_span(start_iso: str, end_iso: str) -> str:
    s = _instant(start_iso, "start").astimezone(get_zone())
    e = _instant(end_iso, "end").astimezone(get_zone())
    if s.date() == e.date():
        return f"{s.month}月{s.day}日 {s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
    return f"{s.month}月{s.day}日 {s.strftime('%H:%M')}–{e.month}月{e.day}日 {e.strftime('%H:%M')}"


def _group_blocks(schedule_entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in schedule_entries:
        tid = entry.get("task_id")
        if not tid:
            continue
        start, end = entry["start"], entry["end"]
        mins = entry.get("minutes")
        if mins is None:
            mins = _minutes(_instant(start, "s"), _instant(end, "e"))
        grouped.setdefault(tid, []).append({"start": start, "end": end, "minutes": mins})
    for blks in grouped.values():
        blks.sort(key=lambda b: (b["start"], b["end"]))
    return grouped


def _calendar_work_by_task(cal_result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    work_entries = [
        w for w in cal_result.get("layers", {}).get("work", [])
        if w.get("kind") != "appointment" and w.get("task_id")
    ]
    return _group_blocks(work_entries)


def _compare_calendar_work(
    cal_result: dict[str, Any],
    expected_by_task: dict[str, list[dict[str, Any]]],
    titles_by_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Compare /api/v3/calendar work blocks segment-by-segment against preview expected blocks."""
    actual_by_task = _calendar_work_by_task(cal_result)
    all_ids = sorted(set(expected_by_task) | set(actual_by_task))
    mismatches: list[dict[str, Any]] = []
    titles = titles_by_id or {}
    for tid in all_ids:
        exp_blks = expected_by_task.get(tid, [])
        act_blks = actual_by_task.get(tid, [])
        exp_sig = [(b["start"], b["end"]) for b in exp_blks]
        act_sig = [(b["start"], b["end"]) for b in act_blks]
        if exp_sig != act_sig:
            mismatches.append({
                "task_id": tid,
                "title": titles.get(tid, tid),
                "expected": [{"start": b["start"], "end": b["end"]} for b in exp_blks],
                "actual": [{"start": b["start"], "end": b["end"]} for b in act_blks],
            })
    return mismatches


def _task_moved_minutes(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> int:
    """Compute total displaced minutes for one task between baseline and replanned blocks."""
    if not before and not after:
        return 0
    if not before:
        return sum(b["minutes"] for b in after)
    if not after:
        return sum(b["minutes"] for b in before)
    shared = min(len(before), len(after))
    total = 0
    for idx in range(shared):
        b_s = _instant(before[idx]["start"], "bs")
        a_s = _instant(after[idx]["start"], "as")
        total += abs(_minutes(b_s, a_s))
    for blk in after[shared:]:
        total += blk["minutes"]
    for blk in before[shared:]:
        total += blk["minutes"]
    return total


def _overlaps_any(blocks: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[str]:
    hits: list[str] = []
    for blk in blocks:
        bs, be = _instant(blk["start"], "s"), _instant(blk["end"], "e")
        for it in items:
            if bs < it["end_dt"] and be > it["start_dt"]:
                if it["label"] not in hits:
                    hits.append(it["label"])
    return hits


def _validate_disruptions(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("disruptions 必须是数组")
    result: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError("disruptions 每项必须是对象")
        title = str(entry.get("title") or "临时打断").strip()[:200]
        raw_s = entry.get("start_at") or entry.get("start")
        raw_e = entry.get("end_at") or entry.get("end")
        if not raw_s or not raw_e:
            raise ValueError("disruptions 每项必须包含 start_at 和 end_at")
        s_dt = _instant(raw_s, f"disruptions[{idx}].start_at")
        e_dt = _instant(raw_e, f"disruptions[{idx}].end_at")
        if e_dt <= s_dt:
            raise ValueError("disruptions 的 end_at 必须晚于 start_at")
        result.append({
            "id": str(entry.get("id") or f"e-disrupt-{idx + 1}"),
            "title": title,
            "start_at": _iso(s_dt),
            "end_at": _iso(e_dt),
            "start_dt": s_dt,
            "end_dt": e_dt,
            "location": str(entry["location"]).strip()[:120] if entry.get("location") else None,
            "kind": entry.get("kind") if entry.get("kind") in {"temporary", "appointment"} else "temporary",
            "label": title,
        })
    return result


def _validate_task_updates(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("task_updates 必须是数组")
    updates: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("task_id"), str):
            raise ValueError("task_updates 每项必须包含字符串 task_id")
        patch: dict[str, Any] = {"task_id": entry["task_id"]}
        if "estimate_minutes" in entry and entry["estimate_minutes"] is not None:
            est = entry["estimate_minutes"]
            if not isinstance(est, int) or isinstance(est, bool) or not (1 <= est <= 20160):
                raise ValueError("task_updates.estimate_minutes 必须是 1–20160 的整数")
            patch["estimate_minutes"] = est
        if "deadline_at" in entry and entry["deadline_at"] is not None:
            patch["deadline_at"] = _iso(_instant(entry["deadline_at"], "deadline_at"))
        if "earliest_start_at" in entry:
            patch["earliest_start_at"] = _iso(_instant(entry["earliest_start_at"], "earliest_start_at")) if entry["earliest_start_at"] else None
        if "status" in entry and entry["status"] is not None:
            if entry["status"] not in {"active", "completed", "cancelled"}:
                raise ValueError("task_updates.status 无效")
            patch["status"] = entry["status"]
        updates.append(patch)
    return updates


def _build_candidate_snapshot(
    snapshot: dict[str, Any],
    disruptions: list[dict[str, Any]],
    rest_blocks: list[dict[str, Any]],
    task_updates: list[dict[str, Any]],
    calibrated_estimates: dict[str, int],
) -> dict[str, Any]:
    """Build a detached v3-valid snapshot incorporating disruptions, locked rests, and task patches."""
    nxt = deepcopy(snapshot)
    existing_event_ids = {ev["id"] for ev in nxt["events"]}
    for item in disruptions:
        ev_id = item["id"]
        if ev_id in existing_event_ids:
            ev_id = f"{ev_id}-{len(nxt['events']) + 1}"
        existing_event_ids.add(ev_id)
        nxt["events"].append({
            "id": ev_id,
            "kind": item["kind"],
            "title": item["title"],
            "start_at": item["start_at"],
            "end_at": item["end_at"],
            "location": item["location"],
            "status": "active",
            "source_id": None,
            "evidence": [],
            "notes": ["重排状态变更：临时打断"],
        })
    existing_fa_ids = {fa["id"] for fa in nxt["free_adjustments"]}
    for rb in rest_blocks:
        if not rb["locked"]:
            continue
        fa_id = f"fa-rest-{rb['id']}"
        if fa_id in existing_fa_ids:
            fa_id = f"{fa_id}-{len(nxt['free_adjustments']) + 1}"
        existing_fa_ids.add(fa_id)
        nxt["free_adjustments"].append({
            "id": fa_id,
            "kind": "remove",
            "start_at": rb["start"],
            "end_at": rb["end"],
            "note": f"锁定休息：{rb['label']}",
        })
    updates_by_id = {u["task_id"]: u for u in task_updates}
    for task in nxt["tasks"]:
        tid = task["id"]
        if tid in updates_by_id:
            for key, val in updates_by_id[tid].items():
                if key != "task_id":
                    task[key] = val
        if tid in calibrated_estimates and task["status"] == "active":
            task["estimate_minutes"] = calibrated_estimates[tid]
    validate_snapshot_v3(nxt)
    return nxt


def _materialize_plan_snapshot(
    candidate_snapshot: dict[str, Any],
    after_by_task: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Materialize a chosen plan's start times into a detached v3 snapshot via earliest_start_at.

    For single-segment tasks (and multi-segment tasks whose subsequent segments follow naturally
    after fixed blocks/rests), pinning earliest_start_at makes `/api/v3/calendar` reproduce the
    exact promised schedule. Callers still verify block-for-block equality via `_compare_calendar_work`.
    """
    plan_snapshot = deepcopy(candidate_snapshot)
    for task in plan_snapshot["tasks"]:
        tid = task["id"]
        blks = after_by_task.get(tid)
        if task["status"] == "active" and blks:
            task["earliest_start_at"] = blks[0]["start"]
    validate_snapshot_v3(plan_snapshot)
    return plan_snapshot


def _evaluate_plan_baseline(
    task_changes: list[dict[str, Any]],
    tasks_by_id: dict[str, dict[str, Any]],
    locked_rest_blocks: list[dict[str, Any]],
    calibration_history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate hard-quality baseline: deadline buffer floor + high-cognitive safety."""
    violations: list[str] = []
    floors: list[int] = []
    buffer_ok = True
    for tc in task_changes:
        tid = tc["task_id"]
        task = tasks_by_id[tid]
        est = int(task["estimate_minutes"])
        floor = 30
        if calibration_history:
            prof = compute_calibration_profile(calibration_history, kind_of(task["title"]))
            if prof["sample_count"] >= 2:
                floor = max(0, int(round((prof["factor"] - 1.0) * est)))
        floors.append(floor)
        buf = tc["deadline_buffer_minutes"]
        if buf < floor:
            buffer_ok = False
            violations.append(f"「{tc['title']}」截止余量 {buf} 分钟低于底线 {floor} 分钟")
        if tc["cognitive_load"] == "high":
            f_match = tc["focus_match"]
            if f_match["matched_minutes"].get("low", 0) > 0:
                violations.append(f"高认知任务「{tc['title']}」落入低专注时段")
    return {
        "passes_baseline": len(violations) == 0,
        "buffer_ok": buffer_ok,
        "required_buffer_floor_minutes": max(floors) if floors else 30,
        "baseline_violations": violations,
    }


def _build_tradeoff_text(plan: dict[str, Any], rec: dict[str, Any]) -> str | None:
    """Human-readable cost/gain summary of an alternative plan relative to the recommended plan."""
    if plan["plan_id"] == rec["plan_id"]:
        return None
    dm = plan["metrics"]["moved_minutes"] - rec["metrics"]["moved_minutes"]
    df = plan["metrics"]["focus_score"] - rec["metrics"]["focus_score"]
    db = plan["metrics"]["min_deadline_buffer_minutes"] - rec["metrics"]["min_deadline_buffer_minutes"]

    if dm > 0:
        cost = f"多改 {dm} 分钟"
    elif dm < 0:
        cost = f"少改 {-dm} 分钟"
    else:
        cost = "移动量相同"

    rec_by_tid = {tc["task_id"]: tc for tc in rec["task_changes"]}
    improved_focus: list[str] = []
    for tc in plan["task_changes"]:
        other = rec_by_tid.get(tc["task_id"])
        if other and tc["focus_match"]["score"] > other["focus_match"]["score"] and tc["focus_match"]["matched_labels"]:
            improved_focus.append(f"「{tc['title']}」排进{tc['focus_match']['matched_labels'][0]}")

    if improved_focus:
        gain = f"换来{'、'.join(improved_focus[:2])}"
    elif db > 0:
        gain = f"换来最小截止缓冲多 {db} 分钟"
    elif df > 0:
        gain = f"换来专注匹配分高 {df} 分"
    elif dm < 0:
        gain = f"但最小截止缓冲少 {-db} 分钟" if db < 0 else "保持更少原计划改动"
    else:
        gain = f"最小截止缓冲变化 {db:+d} 分钟"

    suffix = "（未过底线）" if (not plan["metrics"]["passes_baseline"] and rec["metrics"]["passes_baseline"]) else ""
    return f"{cost}，{gain}{suffix}。"


def _explain_move(
    task: dict[str, Any],
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    disruptions: list[dict[str, Any]],
    rest_blocks: list[dict[str, Any]],
    focus_info: dict[str, Any],
    calibrated_from: int | None,
    objective: str,
) -> tuple[bool, str]:
    before_sig = [(b["start"], b["end"]) for b in before]
    after_sig = [(a["start"], a["end"]) for a in after]
    changed = before_sig != after_sig
    if not changed:
        return False, "保持原排程时段未变动。"
    reasons: list[str] = []
    hit_disrupt = _overlaps_any(before, disruptions)
    hit_rest = _overlaps_any(before, [r for r in rest_blocks if r["locked"]])
    if hit_disrupt:
        reasons.append(f"原时段被临时打断「{'、'.join(hit_disrupt)}」占用")
    if hit_rest:
        reasons.append(f"原时段需避让锁定休息「{'、'.join(hit_rest)}」")
    if calibrated_from is not None and calibrated_from != task["estimate_minutes"]:
        reasons.append(f"按历史校准将用时从 {calibrated_from} 分钟调至 {task['estimate_minutes']} 分钟")
    if objective == "focus_first" and focus_info.get("matched_labels") and focus_info["score"] > 0:
        reasons.append(f"匹配「{LEVEL_CN[focus_info['cognitive_load']]}」认知负荷至「{' / '.join(focus_info['matched_labels'][:2])}」")
    elif objective == "minimal_change":
        reasons.append("在硬约束内就近顺延以尽量减少原计划改动")
    if not reasons:
        if not before:
            reasons.append("新纳入重排窗口并满足前置与截止约束")
        elif objective == "buffer_first":
            reasons.append("为增大截止前安全缓冲提前重排")
        else:
            reasons.append("受同周其他任务联锁重排与截止约束顺延调整")
    after_text = "、".join(_fmt_span(a["start"], a["end"]) for a in after)
    return True, f"{'；'.join(reasons)}，改排至 {after_text}。"


# ---- 5-Minute Starter Action (deterministic KINDS + resting flag + optional DeepSeek) ----

STARTER_TEMPLATES: dict[str, str] = {
    "写作": "新建空白文档，只敲出「{title}」的标题和 3 个一级小节提纲，先不纠结措辞（5 分钟）。",
    "作业": "打开「{title}」题目与教材，只抄下第 1 题的已知条件并圈出要用到的核心公式（5 分钟）。",
    "展示": "新建 3 页空白幻灯片，只填「{title}」封面标题与前 3 页的一句话主旨（5 分钟）。",
    "填报": "打开「{title}」对应表单页面，先把姓名、学号、联系方式等固定项填好并检查附件清单（5 分钟）。",
    "复习": "翻开「{title}」目录，用笔勾出最不熟的 2 个知识点并合上书口述一遍定义（5 分钟）。",
    "作品": "建好「{title}」素材文件夹，把第 1 个核心素材或草图框架摆进工程里（5 分钟）。",
    "阅读": "只读「{title}」的摘要、目录与结论，用 3 句话记下核心问题（5 分钟）。",
}

STARTER_SYSTEM = """你是司序的执行起步教练。请为大学生当前要做的这一件事，生成一个零压力、5分钟内就能立刻动手完成的最小起步动作。
要求：具体可执行、一句话（20–65字）、不空泛喊口号、不编造不存在的截止时间或材料。
如果提供材料摘录，把它当作待阅读的资料，在摘录的范围内给出一个能动手的动作。
只输出 JSON 对象：{"action":"具体5分钟起步动作"}。"""


STARTER_MATERIAL_LIMIT = 6000
STARTER_EXCERPT_LIMIT = 160
MATERIAL_REQUIREMENT = re.compile(r"要求|须|至少|提交|评分|字数|格式|选题|截止|\brequire(?:ment|d)?\b|\bsubmit\b", re.I)


def _starter_excerpts(material: object) -> list[str]:
    if not isinstance(material, str) or len(material) > STARTER_MATERIAL_LIMIT:
        raise ValueError(f"material_text 必须为不超过 {STARTER_MATERIAL_LIMIT} 字的文本")
    # Keep verbatim, bounded excerpts. Lift assignment requirements ahead of incidental prose,
    # while keeping the selected excerpts in the order in which they appeared in the source.
    segments = re.split(r"\s*\n\s*|(?<=[。！？；!?;])\s*|(?<=\.)[ \t]+", material)
    excerpts = list(dict.fromkeys(segment.strip()[:STARTER_EXCERPT_LIMIT].rstrip() for segment in segments if segment.strip()))
    priority = sorted(range(len(excerpts)), key=lambda index: not MATERIAL_REQUIREMENT.search(excerpts[index]))
    return [excerpts[index] for index in sorted(priority[:3])]


def _active_rest_at(now_dt: datetime | None, rest_blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if now_dt is None:
        return None
    for rb in rest_blocks:
        if rb["locked"] and rb["start_dt"] <= now_dt < rb["end_dt"]:
            return rb
    return None


def generate_five_minute_starter(payload: object) -> dict[str, Any]:
    """POST /api/v3/starter: deterministic 5-minute starter action using KINDS, resting flag, and optional DeepSeek."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > 200:
        raise ValueError("title 必须为 1–200 字字符串")
    mode = payload.get("mode", "rules")
    if mode not in {"rules", "model"}:
        raise ValueError("mode 只能是 rules 或 model")
    detected_kind = str(payload.get("kind") or "").strip() or kind_of(title)
    template = STARTER_TEMPLATES.get(
        detected_kind or "",
        "打开「{title}」所需文件，把第 1 个可验证的小子步骤写下来并推进 5 分钟。",
    )
    excerpts = _starter_excerpts(payload.get("material_text", ""))
    action = template.format(title=title)
    if excerpts:
        action = {
            "写作": "从下方选一条原文，改成草稿里的一个小节标题，再在标题下写两句你想说明的事。",
            "阅读": "把下方最相关的一段用自己的话改写成一句话，再记下一个想核对的问题。",
            "填报": "从下方原文找出一项需要提交的材料，在草稿里写下准备使用的文件名或还缺的信息。",
        }.get(detected_kind, "从下方选一条原文，改写成一条具体待办，再记下一个还需核对的问题。")

    # Check whether user is currently inside a locked rest block
    resting = bool(payload.get("resting", False))
    resting_label: str | None = payload.get("resting_label")
    resting_until: str | None = payload.get("resting_until")
    if not resting and payload.get("rest_blocks"):
        now_dt = _instant(payload["now"], "now") if payload.get("now") else None
        active_rb = _active_rest_at(now_dt, parse_rest_blocks(payload.get("rest_blocks")))
        if active_rb:
            resting = True
            resting_label = active_rb["label"]
            resting_until = active_rb["end"]

    def _wrap_rest(step: str) -> str:
        if not resting:
            return step
        until_text = ""
        if resting_until:
            end_local = _instant(resting_until, "resting_until").astimezone(get_zone())
            until_text = f"（至 {end_local.strftime('%H:%M')}）"
        label = resting_label or "锁定休息"
        return f"当前正处于你锁定的「{label}」{until_text}，想歇就歇；休息结束后再：{step}"

    if mode == "model":
        prompt_data = {
            "title": title,
            "kind": detected_kind or "通用",
            "estimate_minutes": payload.get("estimate_minutes"),
            "deadline_at": payload.get("deadline_at"),
        }
        if excerpts:
            prompt_data["material_excerpts"] = excerpts
        res = chat_json(STARTER_SYSTEM, json.dumps(prompt_data, ensure_ascii=False), max_tokens=220)
        action = str(res.get("action") or "").strip()
        if not (6 <= len(action) <= 160):
            raise ExtractionServiceError("模型未返回可用的五分钟起步动作。", 502, "model_invalid_starter")

    return {
        "schema_version": 1,
        "task_id": payload.get("task_id"),
        "title": title,
        "kind": detected_kind or "通用",
        "duration_minutes": 5,
        "resting": resting,
        "resting_label": resting_label,
        "resting_until": resting_until,
        "action": _wrap_rest(action),
        "mode_used": mode,
        **({"material_brief": {"excerpts": excerpts}} if excerpts else {}),
    }


# ---- Natural-Language State-Change Draft Parser (rules + optional DeepSeek) -

STATE_PARSE_SYSTEM = """你是司序的状态变更提取器。请把大学生的一句话转成待用户确认的结构化日程状态变更草稿。
严格基于原文提取，绝不编造未提及的时间或任务。返回 JSON 对象，字段必须为：
{
  "disruptions": [{"title": "事件名", "start_at": "YYYY-MM-DDTHH:MM:00+08:00", "end_at": "YYYY-MM-DDTHH:MM:00+08:00", "location": null}],
  "rest_blocks": [{"label": "休息说明", "start": "YYYY-MM-DDTHH:MM:00+08:00", "end": "YYYY-MM-DDTHH:MM:00+08:00", "locked": true}],
  "focus_windows": [{"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM", "level": "high|medium|low", "label": "状态说明"}],
  "summary": "一句简洁中文摘要"
}
没有提及的数组留空 []。只输出 JSON 对象。"""


def _rules_parse_state(text: str, base_dt: datetime) -> dict[str, Any]:
    disruptions: list[dict[str, Any]] = []
    rest_blocks: list[dict[str, Any]] = []
    focus_windows: list[dict[str, Any]] = []
    warnings: list[str] = []
    base_day = base_dt.astimezone(get_zone()).date()
    day_offset = 1 if re.search(r"明天|明早|明晚", text) else 2 if "后天" in text else 0
    target_day = base_day + timedelta(days=day_offset)

    clauses = [c.strip() for c in re.split(r"[，,。；;！!\n]+", text) if c.strip()]
    for clause in clauses:
        clocks = list(CLOCK_RE.finditer(clause))
        dur_matches = list(DURATION.finditer(clause))
        dur_mins = parse_duration_minutes(dur_matches[0]) if dur_matches else None
        is_rest = bool(re.search(r"休息|午休|小睡|喘口气|放松|歇一|躺一|吃饭|晚饭|午饭", clause))
        is_low_energy = bool(re.search(r"(?:有点|有些|很|好|太)?(?:困|累)|犯困|头晕|没精力|状态不好|注意力不集中|低效", clause))
        is_high_energy = bool(re.search(r"状态好|很清醒|专注|效率高|思路清晰", clause))
        is_disrupt = bool(re.search(r"临时|突然|打断|加会|开会|例会|谈话|被叫去|延误|插队|有事|辅导员|老师找", clause))

        start_dt = end_dt = None
        if len(clocks) >= 2 and RANGE_RE.fullmatch(clause[clocks[0].end():clocks[1].start()]):
            h1, m1, s1 = clock_value(clocks[0], None)
            h2, m2, s2 = clock_value(clocks[1], clocks[0]["period"])
            start_dt = datetime.combine(target_day, datetime.min.time(), get_zone()) + timedelta(hours=h1, minutes=m1, seconds=s1)
            end_dt = datetime.combine(target_day, datetime.min.time(), get_zone()) + timedelta(hours=h2, minutes=m2, seconds=s2)
        elif len(clocks) == 1:
            h, m, s = clock_value(clocks[0], None)
            at_dt = datetime.combine(target_day, datetime.min.time(), get_zone()) + timedelta(hours=h, minutes=m, seconds=s)
            if re.search(r"到|至|之前", clause[:clocks[0].start() + 2]):
                start_dt = base_dt if day_offset == 0 and base_dt < at_dt else at_dt - timedelta(minutes=dur_mins or 60)
                end_dt = at_dt
            else:
                start_dt = at_dt
                end_dt = at_dt + timedelta(minutes=dur_mins or 60)
        elif dur_mins:
            start_dt = base_dt if day_offset == 0 else datetime.combine(target_day, datetime.min.time(), get_zone()) + timedelta(hours=19)
            end_dt = start_dt + timedelta(minutes=dur_mins)

        if is_rest and start_dt and end_dt and end_dt > start_dt:
            label = "锁定午休" if "午休" in clause else "锁定吃饭休息" if re.search(r"吃饭|午饭|晚饭", clause) else "锁定临时休息"
            rest_blocks.append({
                "id": f"rest-parsed-{len(rest_blocks) + 1}",
                "label": label,
                "start": _iso(start_dt),
                "end": _iso(end_dt),
                "locked": True,
            })
        elif is_disrupt and start_dt and end_dt and end_dt > start_dt:
            title = re.sub(r"^(?:我|刚才|刚刚|突然|临时|被叫去|有个)+", "", clause)
            title = CLOCK_RE.sub("", title)
            title = re.sub(r"[-~～–—到至]+", "", title).strip(" ，,的") or "临时打断"
            disruptions.append({
                "id": f"e-disrupt-parsed-{len(disruptions) + 1}",
                "title": title[:60],
                "start_at": _iso(start_dt),
                "end_at": _iso(end_dt),
                "location": None,
            })
        elif is_low_energy or is_high_energy:
            level = "low" if is_low_energy else "high"
            s_win = start_dt or base_dt
            e_win = end_dt or (s_win + timedelta(hours=2))
            if e_win > s_win:
                focus_windows.append({
                    "start": _iso(s_win),
                    "end": _iso(e_win),
                    "level": level,
                    "label": "低精力缓冲时段" if level == "low" else "高专注黄金时段",
                })
        elif (is_rest or is_disrupt) and not (start_dt and end_dt):
            warnings.append(f"「{clause}」缺少具体起止时间或时长，请确认后再补填。")

    parts = []
    if disruptions:
        parts.append(f"{len(disruptions)} 项临时打断")
    if rest_blocks:
        parts.append(f"{len(rest_blocks)} 段锁定休息")
    if focus_windows:
        parts.append(f"{len(focus_windows)} 段专注状态变更")
    summary = f"已解析出 {'、'.join(parts)}，请核对后点确认再生成重排预览。" if parts else "暂未识别出明确的时间打断、休息区间或专注状态变更，请补充具体时段或时长。"
    return {
        "disruptions": disruptions,
        "rest_blocks": rest_blocks,
        "focus_windows": focus_windows,
        "task_updates": [],
        "summary": summary,
        "warnings": warnings,
    }


def parse_state_change(payload: object) -> dict[str, Any]:
    """POST /api/v3/replan/parse-state: convert one sentence into a pending state-change draft for user confirmation."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    text = str(payload.get("text") or "").strip()
    if not text or len(text) > 1000:
        raise ValueError("text 必须为 1–1000 字字符串")
    mode = payload.get("mode", "rules")
    if mode not in {"rules", "model"}:
        raise ValueError("mode 只能是 rules 或 model")
    snapshot = payload.get("snapshot")
    if snapshot is not None:
        validate_snapshot_v3(snapshot)
    now_raw = payload.get("now") or (snapshot["reference_time"] if isinstance(snapshot, dict) else None)
    base_dt = _instant(now_raw, "now") if now_raw else datetime.now(get_zone()).replace(second=0, microsecond=0)

    if mode == "rules":
        parsed = _rules_parse_state(text, base_dt)
        return {
            "schema_version": 1,
            "mode_used": "rules",
            "needs_confirmation": True,
            "parsed_changes": {
                "disruptions": parsed["disruptions"],
                "rest_blocks": parsed["rest_blocks"],
                "focus_windows": parsed["focus_windows"],
                "task_updates": parsed["task_updates"],
            },
            "summary": parsed["summary"],
            "warnings": parsed["warnings"],
        }

    prompt = json.dumps({"reference_now": _iso(base_dt), "text": text}, ensure_ascii=False)
    raw = chat_json(STATE_PARSE_SYSTEM, prompt, max_tokens=600)
    disruptions = [
        {"id": d["id"], "title": d["title"], "start_at": d["start_at"], "end_at": d["end_at"], "location": d["location"]}
        for d in _validate_disruptions(raw.get("disruptions") or [])
    ]
    rests = [
        {"id": r["id"], "label": r["label"], "start": r["start"], "end": r["end"], "locked": r["locked"]}
        for r in parse_rest_blocks(raw.get("rest_blocks") or [])
    ]
    focus = []
    for fw in raw.get("focus_windows") or []:
        if isinstance(fw, dict) and fw.get("start") and fw.get("end") and fw.get("level") in VALID_LEVELS:
            focus.append({
                "date": fw.get("date"),
                "start": fw["start"],
                "end": fw["end"],
                "level": fw["level"],
                "label": str(fw.get("label") or f"{LEVEL_CN[fw['level']]}专注时段")[:80],
            })
    summary = str(raw.get("summary") or "模型已整理出待确认的状态变更草稿，请核对后应用。").strip()[:300]
    return {
        "schema_version": 1,
        "mode_used": "model",
        "needs_confirmation": True,
        "parsed_changes": {
            "disruptions": disruptions,
            "rest_blocks": rests,
            "focus_windows": focus,
            "task_updates": [],
        },
        "summary": summary,
        "warnings": [],
    }


# ---- Core Closed Loop: Preview & Calendar-Verified Confirm ------------------

def replan_preview(payload: object) -> dict[str, Any]:
    """POST /api/v3/replan/preview: compare deterministic replan candidates with calendar-verified snapshots."""
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise ValueError("请求必须包含 v3 snapshot 对象")
    snapshot = payload["snapshot"]
    validate_snapshot_v3(snapshot)
    base_revision = int(snapshot["revision"])
    fingerprint = snapshot_fingerprint(snapshot)
    date_range = payload.get("range") or _default_range(snapshot)
    start_day = date.fromisoformat(date_range["from"])
    end_day = date.fromisoformat(date_range["to"])

    transit_minutes = payload.get("transit_minutes", 0)
    dependencies = payload.get("dependencies")
    disruptions = _validate_disruptions(payload.get("disruptions"))
    rest_blocks = parse_rest_blocks(payload.get("rest_blocks"))
    task_updates = _validate_task_updates(payload.get("task_updates"))
    focus_windows_raw = payload.get("focus_windows") or []
    focus_intervals = parse_focus_windows(focus_windows_raw, start_day, end_day)
    task_profiles_raw = payload.get("task_profiles") or {}

    # Optional duration calibration reuse (`calibration.py`)
    calibration_history = payload.get("calibration_history") or []
    apply_calibration = bool(payload.get("apply_calibration", False))
    calibrated_estimates: dict[str, int] = {}
    original_estimates: dict[str, int] = {}
    calibration_notes: list[dict[str, Any]] = []
    if calibration_history:
        for task in snapshot["tasks"]:
            if task["status"] != "active" or task["estimate_minutes"] is None:
                continue
            k = kind_of(task["title"])
            prof = compute_calibration_profile(calibration_history, k)
            if prof["sample_count"] >= 2 and prof["factor"] != 1.0:
                cal_mins = _round_to_5(task["estimate_minutes"] * prof["factor"])
                calibration_notes.append({
                    "task_id": task["id"],
                    "title": task["title"],
                    "kind": k or "通用",
                    "nominal_minutes": task["estimate_minutes"],
                    "calibrated_minutes": cal_mins,
                    "factor": prof["factor"],
                    "applied": apply_calibration,
                })
                if apply_calibration and cal_mins != task["estimate_minutes"]:
                    original_estimates[task["id"]] = task["estimate_minutes"]
                    calibrated_estimates[task["id"]] = cal_mins

    # 1. Baseline schedule ("原时间") directly from /api/v3/calendar so "原时间" matches what 画卷 currently shows
    base_cal_req: dict[str, Any] = {"snapshot": snapshot, "range": date_range}
    if payload.get("now") is not None:
        base_cal_req["now"] = payload["now"]
    base_cal = calendar_snapshot(base_cal_req)
    before_by_task = _calendar_work_by_task(base_cal)
    baseline_starts = {tid: blks[0]["start"] for tid, blks in before_by_task.items() if blks}

    # 2. Candidate snapshot incorporating disruptions, locked rests, task updates, and optional calibration
    candidate_snapshot = _build_candidate_snapshot(
        snapshot, disruptions, rest_blocks, task_updates, calibrated_estimates
    )
    active_schedulable = [
        t for t in candidate_snapshot["tasks"]
        if t["status"] == "active" and t["deadline_at"] and t["estimate_minutes"] is not None
    ]
    tasks_by_id = {t["id"]: t for t in active_schedulable}
    titles_by_id = {tid: t["title"] for tid, t in tasks_by_id.items()}
    cognitive_loads = parse_task_profiles(task_profiles_raw, active_schedulable)

    # 3. Deterministic three-objective comparison inside hard constraints (including locked rests):
    #    focus_matched (专注匹配) vs buffer_first (截止缓冲) vs minimal_change (最少改动)
    objectives = [
        ("focus_matched", "专注匹配优先方案", "focus_first"),
        ("buffer_first", "截止缓冲优先方案", "buffer_first"),
        ("minimal_change", "最少改动方案", "minimal_change"),
    ]
    candidate_plans: list[dict[str, Any]] = []
    omitted_plans: list[dict[str, Any]] = []
    seen_schedule_sigs: dict[tuple[tuple[str, str, str], ...], tuple[str, str]] = {}
    infeasible_result: dict[str, Any] | None = None

    rest_req_list = [
        {"id": r["id"], "label": r["label"], "start": r["start"], "end": r["end"], "locked": r["locked"]}
        for r in rest_blocks
    ]

    for plan_id, plan_name, obj in objectives:
        solved = solve_snapshot({
            "snapshot": candidate_snapshot,
            "range": date_range,
            "now": payload.get("now"),
            "dependencies": dependencies,
            "transit_minutes": transit_minutes,
            "focus_windows": focus_windows_raw,
            "task_profiles": task_profiles_raw,
            "rest_blocks": rest_req_list,
            "baseline_starts": baseline_starts,
            "objective": obj,
        })
        if not solved["validation"]["passed"]:
            infeasible_result = solved
            continue

        after_by_task = _group_blocks(solved["schedule"])
        # Materialize plan into snapshot (earliest_start_at) and verify /api/v3/calendar reproduces it block-for-block!
        plan_snapshot = _materialize_plan_snapshot(candidate_snapshot, after_by_task)
        cal_req: dict[str, Any] = {"snapshot": plan_snapshot, "range": date_range}
        if payload.get("now") is not None:
            cal_req["now"] = payload["now"]
        cal_replay = calendar_snapshot(cal_req)
        mismatches = _compare_calendar_work(cal_replay, after_by_task, titles_by_id)
        if mismatches:
            # If a rare multi-segment placement with an intentional gap cannot be reproduced identically by
            # /api/v3/calendar's earliest_start_at alone, adopt /api/v3/calendar's verified placement only if
            # it still respects every locked rest & deadline, otherwise omit transparently.
            cal_work = _calendar_work_by_task(cal_replay)
            all_present = all(tid in cal_work and sum(b["minutes"] for b in cal_work[tid]) == tasks_by_id[tid]["estimate_minutes"] for tid in tasks_by_id)
            if cal_replay["validation"]["passed"] and all_present:
                after_by_task = cal_work
                rebuilt_schedule = []
                for tid, blks in after_by_task.items():
                    dl_dt = _instant(tasks_by_id[tid]["deadline_at"], "dl")
                    last_e = max(_instant(b["end"], "e") for b in blks)
                    buf_m = _minutes(last_e, dl_dt)
                    f_sc = score_task_focus([(_instant(b["start"], "s"), _instant(b["end"], "e")) for b in blks], cognitive_loads.get(tid, "medium"), focus_intervals)["score"]
                    for b in blks:
                        rebuilt_schedule.append({
                            "task_id": tid, "title": tasks_by_id[tid]["title"], "start": b["start"], "end": b["end"],
                            "minutes": b["minutes"], "cognitive_load": cognitive_loads.get(tid, "medium"),
                            "deadline_buffer_minutes": buf_m, "focus_score": f_sc,
                        })
                rebuilt_schedule.sort(key=lambda item: (item["start"], item["end"], item["task_id"]))
                solved["schedule"] = rebuilt_schedule
                solved["focus_summary"]["total_score"] = sum(
                    score_task_focus([(_instant(b["start"], "s"), _instant(b["end"], "e")) for b in blks], cognitive_loads.get(tid, "medium"), focus_intervals)["score"]
                    for tid, blks in after_by_task.items()
                )
            else:
                omitted_plans.append({
                    "plan_id": plan_id,
                    "name": plan_name,
                    "reason": "calendar_replay_mismatch",
                    "same_as": None,
                    "detail": f"该方案含复杂分段结构，写回 v3 快照后与 /api/v3/calendar 重算结果存在 {len(mismatches)} 处差异，已跳过以免画卷失真。",
                })
                continue

        sig = tuple((s["task_id"], s["start"], s["end"]) for s in solved["schedule"])
        if sig in seen_schedule_sigs:
            dup_id, dup_name = seen_schedule_sigs[sig]
            omitted_plans.append({
                "plan_id": plan_id,
                "name": plan_name,
                "reason": "duplicate_of",
                "same_as": dup_id,
                "detail": f"与「{dup_name}」排程结果完全一致，已合并去重。",
            })
            continue
        seen_schedule_sigs[sig] = (plan_id, plan_name)

        task_changes: list[dict[str, Any]] = []
        moved_count = 0
        total_moved_minutes = 0
        min_buffer: int | None = None
        total_buffer = 0
        for tid, task in tasks_by_id.items():
            before_blks = before_by_task.get(tid, [])
            after_blks = after_by_task.get(tid, [])
            dl_dt = _instant(task["deadline_at"], "deadline")
            last_end = max((_instant(b["end"], "e") for b in after_blks), default=dl_dt)
            buf = _minutes(last_end, dl_dt)
            min_buffer = buf if min_buffer is None else min(min_buffer, buf)
            total_buffer += buf
            f_info = score_task_focus(
                [(_instant(b["start"], "s"), _instant(b["end"], "e")) for b in after_blks],
                cognitive_loads.get(tid, "medium"),
                focus_intervals,
            )
            changed, reason = _explain_move(
                task,
                before_blks,
                after_blks,
                disruptions,
                rest_blocks,
                f_info,
                original_estimates.get(tid),
                obj,
            )
            moved_min = _task_moved_minutes(before_blks, after_blks) if changed else 0
            if changed:
                moved_count += 1
                total_moved_minutes += moved_min
            task_changes.append({
                "task_id": tid,
                "title": task["title"],
                "cognitive_load": cognitive_loads.get(tid, "medium"),
                "before": before_blks,
                "after": after_blks,
                "changed": changed,
                "moved_minutes": moved_min,
                "move_reason": reason,
                "deadline_at": task["deadline_at"],
                "deadline_buffer_minutes": buf,
                "focus_match": f_info,
            })

        expected_schedule = _canonical_schedule(solved["schedule"])
        token = _preview_token(base_revision, fingerprint, plan_id, plan_snapshot, expected_schedule)
        display_name = "保持原排法" if (moved_count == 0 and total_moved_minutes == 0) else plan_name
        seen_schedule_sigs[sig] = (plan_id, display_name)
        baseline_eval = _evaluate_plan_baseline(
            task_changes,
            tasks_by_id,
            [r for r in rest_blocks if r["locked"]],
            calibration_history,
        )
        candidate_plans.append({
            "plan_id": plan_id,
            "name": display_name,
            "objective": obj,
            "recommended": False,
            "tradeoff_vs_recommended": None,
            "summary": (
                f"保持原排程不变（全部 {len(tasks_by_id)} 项任务满足硬约束，专注匹配分 {solved['focus_summary']['total_score']}，最小截止缓冲 {min_buffer or 0} 分钟）。"
                if moved_count == 0 and total_moved_minutes == 0
                else f"全部 {len(tasks_by_id)} 项任务可行（移动 {moved_count} 项、总移动量 {total_moved_minutes} 分钟），"
                f"专注匹配分 {solved['focus_summary']['total_score']}，最小截止缓冲 {min_buffer or 0} 分钟。"
            ),
            "metrics": {
                "scheduled_tasks": len(tasks_by_id),
                "moved_tasks": moved_count,
                "moved_minutes": total_moved_minutes,
                "focus_score": solved["focus_summary"]["total_score"],
                "min_deadline_buffer_minutes": min_buffer or 0,
                "total_deadline_buffer_minutes": total_buffer,
                "required_buffer_floor_minutes": baseline_eval["required_buffer_floor_minutes"],
                "passes_baseline": baseline_eval["passes_baseline"],
                "baseline_violations": baseline_eval["baseline_violations"],
                "locked_rest_minutes": sum(r["minutes"] for r in rest_blocks if r["locked"]),
                "transit_warnings": len(solved["transit_warnings"]),
            },
            "schedule": solved["schedule"],
            "expected_schedule": expected_schedule,
            "task_changes": task_changes,
            "validation": solved["validation"],
            "calendar_verified": True,
            "preview_token": token,
            "preview_snapshot": plan_snapshot,
        })

    if not candidate_plans:
        missing_windows = (infeasible_result or {}).get("missing_windows") or []
        return {
            "schema_version": 1,
            "status": "infeasible",
            "feasible": False,
            "confirmable": False,
            "needs_decision": True,
            "decision_reason": "存在截止前无法排下的任务，需要你决定释放空档、调整锁定休息或延后截止。",
            "base_revision": base_revision,
            "snapshot_fingerprint": fingerprint,
            "range": date_range,
            "reason": (infeasible_result or {}).get("reason") or "在当前硬约束与截止时间前无可行排程。",
            "hard_constraints_checked": [
                "deadlines",
                "confirmed_routines_and_appointments",
                "dag_dependencies",
                "user_locked_rest_blocks",
            ],
            "candidate_plans": [],
            "omitted_plans": omitted_plans,
            "focus_hint": None,
            "missing_windows": missing_windows,
            "calibration_notes": calibration_notes,
            "five_minute_starter": None,
        }

    # Backend-owned recommendation & decision gate:
    # Policy: "先保底线，再少改，最后看专注"
    has_state_change = bool(
        disruptions
        or any(r["locked"] for r in rest_blocks)
        or task_updates
        or calibrated_estimates
    )
    focus_hint: str | None = None
    passing_plans = [p for p in candidate_plans if p["metrics"]["passes_baseline"]]
    if not has_state_change:
        keep_plan = next((p for p in candidate_plans if p["metrics"]["moved_minutes"] == 0 and p["metrics"]["moved_tasks"] == 0), None)
        primary = keep_plan or (
            min(passing_plans, key=lambda p: (p["metrics"]["moved_minutes"], -p["metrics"]["focus_score"], -p["metrics"]["min_deadline_buffer_minutes"]))
            if passing_plans
            else max(candidate_plans, key=lambda p: (p["metrics"]["min_deadline_buffer_minutes"], -p["metrics"]["moved_minutes"], p["metrics"]["focus_score"]))
        )
        needs_decision = False
        decision_reason = "当前没有打断或状态变更，原排程可直接沿用，无需弹出取舍卡。"
        focus_plan = next((p for p in candidate_plans if p["plan_id"] == "focus_matched"), None)
        if focus_plan is not None and focus_plan["metrics"]["moved_tasks"] > 0:
            base_focus = sum(
                score_task_focus(
                    [(_instant(b["start"], "s"), _instant(b["end"], "e")) for b in before_by_task.get(tid, [])],
                    cognitive_loads.get(tid, "medium"),
                    focus_intervals,
                )["score"]
                for tid in tasks_by_id
            )
            if focus_plan["metrics"]["focus_score"] > base_focus:
                focus_hint = (
                    f"当前没有状态变更，原排法可行；若想更贴合专注时段，可按「专注匹配优先方案」调整 "
                    f"{focus_plan['metrics']['moved_tasks']} 项任务（专注分 {base_focus} → {focus_plan['metrics']['focus_score']}）。"
                )
    else:
        if passing_plans:
            primary = min(
                passing_plans,
                key=lambda p: (
                    p["metrics"]["moved_minutes"],
                    -p["metrics"]["focus_score"],
                    -p["metrics"]["min_deadline_buffer_minutes"],
                ),
            )
        else:
            primary = max(
                candidate_plans,
                key=lambda p: (
                    p["metrics"]["min_deadline_buffer_minutes"],
                    -p["metrics"]["moved_minutes"],
                    p["metrics"]["focus_score"],
                ),
            )
        if not passing_plans:
            needs_decision = True
            decision_reason = (
                f"所有可行方案均存在余量不足底线或高认知安全告警（推荐余量最大的「{primary['name']}」，"
                f"最小缓冲 {primary['metrics']['min_deadline_buffer_minutes']} 分钟，底线 {primary['metrics']['required_buffer_floor_minutes']} 分钟），"
                "请确认是否接受或调整截止与休息安排。"
            )
        elif primary["metrics"]["moved_tasks"] == 0:
            needs_decision = False
            decision_reason = "本次变化未挤动任何已排任务，保持原排法即可，无需弹出取舍卡。"
            focus_plan = next((p for p in candidate_plans if p["plan_id"] == "focus_matched"), None)
            if focus_plan is not None and focus_plan["metrics"]["moved_tasks"] > 0 and focus_plan["metrics"]["focus_score"] > primary["metrics"]["focus_score"]:
                focus_hint = (
                    f"本次变化未挤动原排程；若想更贴合专注时段，可按「专注匹配优先方案」调整 "
                    f"{focus_plan['metrics']['moved_tasks']} 项任务（专注分 {primary['metrics']['focus_score']} → {focus_plan['metrics']['focus_score']}）。"
                )
        elif len(candidate_plans) > 1:
            needs_decision = True
            decision_reason = (
                f"本次变化影响了 {primary['metrics']['moved_tasks']} 项任务，按「先保底线、再少改、最后看专注」推荐「{primary['name']}」"
                f"（总移动 {primary['metrics']['moved_minutes']} 分钟、最小缓冲 {primary['metrics']['min_deadline_buffer_minutes']} 分钟），"
                f"共 {len(candidate_plans)} 套备选方案供取舍。"
            )
        else:
            needs_decision = True
            decision_reason = (
                f"本次变化导致 {primary['metrics']['moved_tasks']} 项任务移动"
                f"（总移动量 {primary['metrics']['moved_minutes']} 分钟），需要你预览确认后再写回画卷。"
            )

    for p in candidate_plans:
        p["recommended"] = p["plan_id"] == primary["plan_id"]
    candidate_plans.sort(key=lambda p: (not p["recommended"], p["metrics"]["moved_minutes"], -p["metrics"]["focus_score"]))
    for p in candidate_plans:
        p["tradeoff_vs_recommended"] = _build_tradeoff_text(p, primary)

    # Optional 5-minute starter for the earliest scheduled task in the recommended plan (with resting check!)
    starter = None
    if payload.get("include_starter", False) and primary["schedule"]:
        first_entry = primary["schedule"][0]
        first_task = tasks_by_id.get(first_entry["task_id"], {})
        current_now_iso = payload.get("now") or snapshot["reference_time"]
        starter = generate_five_minute_starter({
            "task_id": first_entry["task_id"],
            "title": first_entry["title"],
            "estimate_minutes": first_task.get("estimate_minutes"),
            "deadline_at": first_task.get("deadline_at"),
            "now": current_now_iso,
            "rest_blocks": rest_req_list,
            "mode": payload.get("starter_mode", "rules"),
        })

    return {
        "schema_version": 1,
        "status": "ready",
        "feasible": True,
        "confirmable": True,
        "needs_decision": needs_decision,
        "decision_reason": decision_reason,
        "focus_hint": focus_hint,
        "base_revision": base_revision,
        "snapshot_fingerprint": fingerprint,
        "range": date_range,
        "recommended_plan_id": primary["plan_id"],
        "hard_constraints_checked": [
            "deadlines",
            "confirmed_routines_and_appointments",
            "dag_dependencies",
            "user_locked_rest_blocks",
        ],
        "candidate_plans": candidate_plans,
        "omitted_plans": omitted_plans,
        "missing_windows": [],
        "calibration_notes": calibration_notes,
        "five_minute_starter": starter,
        "confirm_hint": {
            "endpoint": "/api/v3/replan/confirm",
            "required_fields": [
                "snapshot", "base_revision", "snapshot_fingerprint", "plan_id",
                "preview_token", "preview_snapshot", "expected_schedule",
            ],
        },
    }


def replan_confirm(payload: object) -> dict[str, Any]:
    """POST /api/v3/replan/confirm: verify revision, fingerprint, token, AND exact /api/v3/calendar replay match."""
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise ValueError("请求必须包含当前最新的 v3 snapshot 对象")
    current_snapshot = payload["snapshot"]
    validate_snapshot_v3(current_snapshot)
    current_revision = int(current_snapshot["revision"])

    base_revision = payload.get("base_revision")
    if not isinstance(base_revision, int) or isinstance(base_revision, bool):
        raise ValueError("base_revision 必须是整数")
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        raise ValueError("plan_id 不能为空")
    preview_token = str(payload.get("preview_token") or "").strip()
    if not preview_token:
        raise ValueError("preview_token 不能为空")
    preview_snapshot = payload.get("preview_snapshot")
    if not isinstance(preview_snapshot, dict):
        raise ValueError("preview_snapshot 必须是 v3 snapshot 对象")
    expected_schedule_raw = payload.get("expected_schedule")
    if not isinstance(expected_schedule_raw, list):
        raise ValueError("expected_schedule 必须是预览方案返回的排程块数组")
    expected_schedule = _canonical_schedule(expected_schedule_raw)

    # 1. Optimistic revision check (reject stale preview with HTTP 409)
    if current_revision != base_revision:
        raise ReplanConflictError(
            f"清单在生成预览后已发生变化（预览基于版本 {base_revision}，当前已是版本 {current_revision}），已拒绝应用过期预览，请重新生成重排预览。",
            expected_revision=base_revision,
            current_revision=current_revision,
        )

    # 2. Fingerprint check (catches mutations even if client forgot to bump revision)
    current_fp = snapshot_fingerprint(current_snapshot)
    expected_fp = payload.get("snapshot_fingerprint")
    if expected_fp is not None and expected_fp != current_fp:
        raise ReplanConflictError(
            "当前清单内容与生成预览时不一致，已拒绝应用过期预览，请重新生成重排预览。",
            expected_revision=base_revision,
            current_revision=current_revision,
        )

    # 3. Validate preview snapshot & verify preview_token integrity (binds expected_schedule too!)
    validate_snapshot_v3(preview_snapshot)
    expected_token = _preview_token(base_revision, expected_fp or current_fp, plan_id, preview_snapshot, expected_schedule)
    if preview_token != expected_token:
        raise ReplanConflictError(
            "preview_token 与预览快照或预期排程不匹配，预览内容可能已被篡改或损坏，已拒绝确认，请重新生成重排预览。",
            expected_revision=base_revision,
            current_revision=current_revision,
            code="preview_tampered",
        )

    confirmed = deepcopy(preview_snapshot)
    confirmed["revision"] = current_revision + 1
    validate_snapshot_v3(confirmed)
    date_range = payload.get("range") or _default_range(confirmed)
    cal_req: dict[str, Any] = {"snapshot": confirmed, "range": date_range}
    if payload.get("now") is not None:
        cal_req["now"] = payload["now"]
    cal_check = calendar_snapshot(cal_req)

    # 4. Strict block-for-block /api/v3/calendar replay verification against preview expected_schedule!
    titles_by_id = {t["id"]: t["title"] for t in confirmed["tasks"]}
    expected_by_task = _group_blocks(expected_schedule)
    mismatches = _compare_calendar_work(cal_check, expected_by_task, titles_by_id)
    # validation.passed is None when there is no work to schedule (nothing to check), which is not a failure.
    if mismatches or cal_check["validation"]["passed"] is False:
        diff_desc = "；".join(
            f"「{m['title']}」预期 {[(b['start'], b['end']) for b in m['expected']]}，画卷实际 {[(b['start'], b['end']) for b in m['actual']]}"
            for m in mismatches[:3]
        ) or "画卷校验未通过"
        raise ReplanConflictError(
            f"写回快照后经 /api/v3/calendar 重算与所选预览方案不一致，已拒绝确认：{diff_desc}",
            expected_revision=base_revision,
            current_revision=current_revision,
            code="schedule_mismatch",
            extra={"mismatches": mismatches},
        )

    return {
        "schema_version": 1,
        "confirmed": True,
        "plan_id": plan_id,
        "previous_revision": current_revision,
        "new_revision": confirmed["revision"],
        "snapshot": confirmed,
        "calendar_validation": cal_check["validation"],
        "calendar_match": {
            "matched": True,
            "verified_tasks": len(expected_by_task),
            "mismatches": [],
        },
        "summary": f"已确认应用方案「{plan_id}」并通过画卷逐段核对，清单版本由 {current_revision} 升级至 {confirmed['revision']}。",
    }
