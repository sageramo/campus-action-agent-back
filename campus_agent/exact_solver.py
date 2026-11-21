"""Exact constraint solver + cross-building transit buffers + Pareto trade-off plans (v3).

Pure-Python, zero-external-dependency solver designed for Render free-tier RAM/CPU limits:
1. Supports task precedence DAG (`dependencies: [{"before": id_a, "after": id_b}]`) with cycle checks.
2. Detects cross-building transit squeezes and optionally enforces `transit_minutes` buffers
   between consecutive fixed items at different non-empty campus locations.
3. Solves bounded task placement (<= 40 tasks, <= 31 days) using exact branch-and-bound search
   (`exact_branch_and_bound_v1`), finding globally feasible plans even in greedy-trap layouts
   where Earliest-Deadline-First fails.
4. Independently verifies every produced schedule with a zero-shared-logic validator.
5. Generates Pareto-non-dominated multi-objective trade-off plans (e.g. maximizing free/rest
   and preserving courses vs preserving temporary events) when conflicts occur.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any

from .conflict import merge_intervals
from .layers import (
    DEFAULT_MIN_BLOCK,
    MAX_PLANNED_TASKS,
    MAX_RANGE_DAYS,
    calendar_snapshot,
    validate_snapshot_v3,
)
from .snapshot import _instant
from .time_utils import get_zone

Interval = tuple[datetime, datetime]
MAX_SEARCH_NODES = 10_000
MAX_PARETO_CONFLICTS = 4
DEFAULT_STEP_MINUTES = 15
VALID_LEVELS = {"high", "medium", "low"}
FOCUS_WEIGHTS: dict[str, dict[str, int]] = {
    "high": {"high": 3, "medium": 1, "neutral": 0, "low": -2},
    "medium": {"high": 1, "medium": 2, "neutral": 1, "low": 0},
    "low": {"high": 0, "medium": 1, "neutral": 1, "low": 2},
}
LEVEL_CN = {"high": "高", "medium": "中", "low": "低", "neutral": "常规"}


def _parse_hhmm(value: str, day: date, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 HH:MM 或 ISO8601 字符串")
    if "T" in value:
        return _instant(value, field)
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"{field} 格式应为 HH:MM 或 ISO8601")
    hour, minute = int(parts[0]), int(parts[1])
    if hour == 24 and minute == 0:
        return datetime.combine(day + timedelta(days=1), datetime.min.time(), get_zone())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{field} 超出 00:00–24:00 范围")
    return datetime.combine(day, datetime.min.time(), get_zone()) + timedelta(hours=hour, minutes=minute)


def parse_focus_windows(raw_windows: Any, start_day: date, end_day: date) -> list[dict[str, Any]]:
    """Expand optional focus windows into concrete timezone-aware intervals within [start_day, end_day]."""
    if raw_windows is None:
        return []
    if not isinstance(raw_windows, list):
        raise ValueError("focus_windows 必须是数组")
    expanded: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw_windows):
        if not isinstance(entry, dict):
            raise ValueError("focus_windows 每项必须是对象")
        level = entry.get("level", "high")
        if level not in VALID_LEVELS:
            raise ValueError("focus_windows.level 只能是 high、medium 或 low")
        label = str(entry.get("label") or f"{LEVEL_CN[level]}专注时段").strip()[:80]
        raw_start, raw_end = entry.get("start"), entry.get("end")
        if not raw_start or not raw_end:
            raise ValueError("focus_windows 每项必须包含 start 和 end")
        if isinstance(raw_start, str) and "T" in raw_start and isinstance(raw_end, str) and "T" in raw_end:
            s_dt = _instant(raw_start, f"focus_windows[{idx}].start")
            e_dt = _instant(raw_end, f"focus_windows[{idx}].end")
            if e_dt <= s_dt:
                raise ValueError("focus_windows 的 end 必须晚于 start")
            expanded.append({"start": s_dt, "end": e_dt, "level": level, "label": label})
            continue
        target_days: list[date] = []
        if entry.get("date"):
            target_days = [date.fromisoformat(str(entry["date"]))]
        else:
            raw_wd = entry.get("weekdays") if "weekdays" in entry else ([entry["weekday"]] if "weekday" in entry else None)
            if raw_wd is None:
                allowed = {1, 2, 3, 4, 5, 6, 7}
            else:
                if not isinstance(raw_wd, list) or not all(isinstance(w, int) and not isinstance(w, bool) and 1 <= w <= 7 for w in raw_wd):
                    raise ValueError("focus_windows.weekday(s) 必须是 1–7 的整数")
                allowed = set(raw_wd)
            cur = start_day
            while cur <= end_day:
                if cur.isoweekday() in allowed:
                    target_days.append(cur)
                cur += timedelta(days=1)
        for day in target_days:
            s_dt = _parse_hhmm(raw_start, day, f"focus_windows[{idx}].start")
            e_dt = _parse_hhmm(raw_end, day, f"focus_windows[{idx}].end")
            if e_dt <= s_dt:
                raise ValueError("focus_windows 的 end 必须晚于 start")
            expanded.append({"start": s_dt, "end": e_dt, "level": level, "label": label})
    expanded.sort(key=lambda item: (item["start"], item["end"]))
    return expanded


def parse_task_profiles(raw_profiles: Any, tasks: list[dict]) -> dict[str, str]:
    """Resolve optional cognitive_load per task ('high' | 'medium' | 'low', default 'medium')."""
    if raw_profiles is not None and not isinstance(raw_profiles, dict):
        raise ValueError("task_profiles 必须是按 task_id 索引的对象")
    profiles = raw_profiles or {}
    result: dict[str, str] = {}
    for task in tasks:
        tid = task["id"]
        entry = profiles.get(tid)
        if entry is not None and not isinstance(entry, dict):
            raise ValueError(f"task_profiles['{tid}'] 必须是对象")
        level = (entry or {}).get("cognitive_load") or task.get("cognitive_load") or "medium"
        if level not in VALID_LEVELS:
            raise ValueError(f"任务「{tid}」的 cognitive_load 只能是 high、medium 或 low")
        result[tid] = level
    return result


def parse_rest_blocks(raw_rests: Any) -> list[dict[str, Any]]:
    """Validate optional temporary rest intervals; locked=True rest intervals are hard constraints."""
    if raw_rests is None:
        return []
    if not isinstance(raw_rests, list):
        raise ValueError("rest_blocks 必须是数组")
    parsed: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw_rests):
        if not isinstance(entry, dict):
            raise ValueError("rest_blocks 每项必须是对象")
        raw_start = entry.get("start") or entry.get("start_at")
        raw_end = entry.get("end") or entry.get("end_at")
        if not raw_start or not raw_end:
            raise ValueError("rest_blocks 每项必须包含 start 和 end")
        s_dt = _instant(raw_start, f"rest_blocks[{idx}].start")
        e_dt = _instant(raw_end, f"rest_blocks[{idx}].end")
        if e_dt <= s_dt:
            raise ValueError("rest_blocks 的 end 必须晚于 start")
        locked = entry.get("locked", True)
        if not isinstance(locked, bool):
            raise ValueError("rest_blocks.locked 必须是布尔值")
        parsed.append({
            "id": str(entry.get("id") or f"rest-{idx + 1}"),
            "label": str(entry.get("label") or "临时休息").strip()[:80],
            "start": _iso(s_dt),
            "end": _iso(e_dt),
            "start_dt": s_dt,
            "end_dt": e_dt,
            "locked": locked,
            "minutes": _minutes(s_dt, e_dt),
        })
    parsed.sort(key=lambda item: (item["start_dt"], item["end_dt"]))
    return parsed


def score_task_focus(
    blocks: list[Interval], cognitive_load: str, focus_intervals: list[dict[str, Any]]
) -> dict[str, Any]:
    """Deterministic focus-matching score for one task's placed blocks."""
    weights = FOCUS_WEIGHTS.get(cognitive_load, FOCUS_WEIGHTS["medium"])
    by_level = {"high": 0, "medium": 0, "low": 0, "neutral": 0}
    matched_labels: list[str] = []
    for b_start, b_end in blocks:
        total = _minutes(b_start, b_end)
        covered = 0
        for fw in focus_intervals:
            ov_s = max(b_start, fw["start"])
            ov_e = min(b_end, fw["end"])
            if ov_e > ov_s:
                mins = _minutes(ov_s, ov_e)
                by_level[fw["level"]] += mins
                covered += mins
                if fw["label"] not in matched_labels:
                    matched_labels.append(fw["label"])
        by_level["neutral"] += max(0, total - covered)
    score = sum(by_level[lvl] * weights[lvl] for lvl in by_level)
    dominant = max(by_level, key=lambda lvl: (by_level[lvl], weights[lvl])) if any(by_level.values()) else "neutral"
    if not focus_intervals:
        summary = f"认知负荷「{LEVEL_CN[cognitive_load]}」，未设置专注时段。"
    elif matched_labels:
        summary = f"认知负荷「{LEVEL_CN[cognitive_load]}」匹配至「{' / '.join(matched_labels[:2])}」（匹配分 {score}）。"
    else:
        summary = f"认知负荷「{LEVEL_CN[cognitive_load]}」落在常规留白时段（匹配分 {score}）。"
    return {
        "cognitive_load": cognitive_load,
        "score": score,
        "matched_minutes": by_level,
        "dominant_focus_level": dominant,
        "matched_labels": matched_labels,
        "summary": summary,
    }


def diagnose_missing_windows(
    tasks: list[dict],
    free_slots: list[Interval],
    blocking_items: list[dict[str, Any]],
    reference: datetime,
) -> list[dict[str, Any]]:
    """Explicitly report what time windows are missing before each infeasible task's deadline."""
    diagnostics: list[dict[str, Any]] = []
    ordered = sorted(tasks, key=lambda t: _instant(t["deadline_at"], "deadline"))
    cumulative_need = 0
    for task in ordered:
        rel = max(reference, _instant(task["earliest_start_at"], "earliest")) if task.get("earliest_start_at") else reference
        dl = _instant(task["deadline_at"], "deadline")
        need = int(task["estimate_minutes"])
        cumulative_need += need
        clipped: list[Interval] = []
        for s, e in free_slots:
            cs, ce = max(s, rel), min(e, dl)
            if ce > cs:
                clipped.append((cs, ce))
        avail = sum(_minutes(s, e) for s, e in clipped)
        longest = max((_minutes(s, e) for s, e in clipped), default=0)
        # Also check cumulative capacity from reference up to this deadline
        cum_clipped = [(max(s, reference), min(e, dl)) for s, e in free_slots if min(e, dl) > max(s, reference)]
        cum_avail = sum(_minutes(s, e) for s, e in cum_clipped)
        own_short = max(0, need - avail)
        contig_short = 0 if task["splittable"] else max(0, need - longest)
        cum_short = max(0, cumulative_need - cum_avail)
        shortfall = max(own_short, contig_short, cum_short)
        if shortfall <= 0 and dl > rel:
            continue
        blockers = [
            {
                "title": b["title"],
                "kind": b["kind"],
                "start": b["start"],
                "end": b["end"],
            }
            for b in blocking_items
            if b["end_dt"] > rel and b["start_dt"] < dl
        ][:6]
        if dl <= rel:
            summary = f"「{task['title']}」截止时间 {dl.strftime('%m月%d日 %H:%M')} 不晚于当前起点 {rel.strftime('%m月%d日 %H:%M')}，已无可用排程窗口。"
        elif not task["splittable"] and own_short == 0 and contig_short > 0:
            summary = (
                f"「{task['title']}」不可拆分，需连续 {need} 分钟，但 {rel.strftime('%m月%d日 %H:%M')}–{dl.strftime('%m月%d日 %H:%M')} "
                f"内最长连续留白仅 {longest} 分钟（缺少至少一段连续 {need} 分钟窗口）。"
            )
        elif own_short > 0:
            summary = (
                f"「{task['title']}」在 {dl.strftime('%m月%d日 %H:%M')} 截止前需要 {need} 分钟，"
                f"但 {rel.strftime('%m月%d日 %H:%M')}–{dl.strftime('%m月%d日 %H:%M')} 内仅剩 {avail} 分钟可用留白，"
                f"缺少至少 {own_short} 分钟时间窗口。"
            )
        else:
            summary = (
                f"截止到 {dl.strftime('%m月%d日 %H:%M')} 的累计待办共需 {cumulative_need} 分钟，"
                f"但该截止前总可用留白仅 {cum_avail} 分钟（累计缺少 {cum_short} 分钟窗口，连带影响「{task['title']}」）。"
            )
        diagnostics.append({
            "task_id": task["id"],
            "title": task["title"],
            "window_start": _iso(rel),
            "deadline_at": _iso(dl),
            "required_minutes": need,
            "available_minutes": avail,
            "longest_contiguous_minutes": longest,
            "shortfall_minutes": max(shortfall, need if dl <= rel else 0),
            "splittable": bool(task["splittable"]),
            "blocking_items": blockers,
            "summary": summary,
        })
    if not diagnostics and ordered:
        # Fragmentation / dependency coupling infeasibility across multiple tasks
        first_dl = _instant(ordered[-1]["deadline_at"], "deadline")
        avail = sum(_minutes(max(s, reference), min(e, first_dl)) for s, e in free_slots if min(e, first_dl) > max(s, reference))
        diagnostics.append({
            "task_id": ordered[0]["id"],
            "title": ordered[0]["title"],
            "window_start": _iso(reference),
            "deadline_at": _iso(first_dl),
            "required_minutes": sum(int(t["estimate_minutes"]) for t in ordered),
            "available_minutes": avail,
            "longest_contiguous_minutes": max((_minutes(s, e) for s, e in free_slots), default=0),
            "shortfall_minutes": max(15, sum(int(t["estimate_minutes"]) for t in ordered) - avail),
            "splittable": all(bool(t["splittable"]) for t in ordered),
            "blocking_items": [{"title": b["title"], "kind": b["kind"], "start": b["start"], "end": b["end"]} for b in blocking_items[:6]],
            "summary": "受连续时长、依赖顺序或锁定休息切分影响，现有留白碎片无法同时容纳全部任务，请释放一段连续空档或延后部分截止。",
        })
    return diagnostics


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _normalize_location(loc: str | None) -> str:
    return "".join(str(loc or "").split()).lower()


def _default_range(snapshot: dict) -> dict[str, str]:
    ref = _instant(snapshot["reference_time"], "reference_time").astimezone(get_zone()).date()
    monday = ref - timedelta(days=ref.weekday())
    return {"from": monday.isoformat(), "to": (monday + timedelta(days=6)).isoformat()}


def _cut_intervals(intervals: list[Interval], blocked: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in intervals:
        pieces = [(start, end)]
        for b_start, b_end in blocked:
            nxt: list[Interval] = []
            for p_start, p_end in pieces:
                if b_end <= p_start or b_start >= p_end:
                    nxt.append((p_start, p_end))
                    continue
                if p_start < b_start:
                    nxt.append((p_start, b_start))
                if b_end < p_end:
                    nxt.append((b_end, p_end))
            pieces = nxt
        result.extend((s, e) for s, e in pieces if e > s)
    return merge_intervals(result)


def _detect_transit_warnings_and_buffers(
    cal_result: dict, transit_minutes: int
) -> tuple[list[dict], list[Interval]]:
    """Detect tight transitions across distinct non-empty campus buildings and derive transit buffers."""
    if transit_minutes <= 0:
        return [], []
    fixed_items: list[dict] = []
    for layer_name in ("routine", "temporary"):
        for item in cal_result["layers"][layer_name]:
            fixed_items.append({
                "id": item["id"],
                "title": item["title"],
                "layer": layer_name,
                "start": _instant(item["start"], "start"),
                "end": _instant(item["end"], "end"),
                "location": item.get("location"),
            })
    for item in cal_result["layers"]["work"]:
        if item.get("kind") == "appointment":
            fixed_items.append({
                "id": item["id"],
                "title": item["title"],
                "layer": "appointment",
                "start": _instant(item["start"], "start"),
                "end": _instant(item["end"], "end"),
                "location": item.get("location"),
            })
    fixed_items.sort(key=lambda it: (it["start"], it["end"]))
    warnings: list[dict] = []
    buffers: list[Interval] = []
    delta = timedelta(minutes=transit_minutes)
    for idx in range(len(fixed_items) - 1):
        first, second = fixed_items[idx], fixed_items[idx + 1]
        loc_a, loc_b = _normalize_location(first["location"]), _normalize_location(second["location"])
        if not loc_a or not loc_b or loc_a == loc_b:
            continue
        gap = _minutes(first["end"], second["start"])
        if gap < 0:
            continue  # Already an overlap conflict handled by layers.
        if gap < transit_minutes:
            warnings.append({
                "type": "transit_tight",
                "from_id": first["id"],
                "from_title": first["title"],
                "from_location": first["location"],
                "to_id": second["id"],
                "to_title": second["title"],
                "to_location": second["location"],
                "gap_minutes": gap,
                "required_transit_minutes": transit_minutes,
                "reason": (
                    f"「{first['title']}」（{first['location']}）结束后仅隔 {gap} 分钟就要到"
                    f"「{second['title']}」（{second['location']}），小于设定的 {transit_minutes} 分钟跨楼通勤缓冲。"
                ),
            })
        else:
            # Reserve transit buffer right after first item so tasks do not pack against cross-building travel.
            buffers.append((first["end"], first["end"] + delta))
    return warnings, merge_intervals(buffers)


def _validate_dependencies(
    raw_deps: Any, task_ids: set[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[dict[str, str]]]:
    if raw_deps is None:
        return {tid: [] for tid in task_ids}, {tid: [] for tid in task_ids}, []
    if not isinstance(raw_deps, list):
        raise ValueError("dependencies 必须是数组")
    preds: dict[str, list[str]] = {tid: [] for tid in task_ids}
    succs: dict[str, list[str]] = {tid: [] for tid in task_ids}
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for entry in raw_deps:
        if not isinstance(entry, dict):
            raise ValueError("dependencies 每项必须是包含 before 和 after 的对象")
        before, after = entry.get("before"), entry.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            raise ValueError("dependencies 的 before 与 after 必须是任务 ID 字符串")
        if before == after:
            raise ValueError(f"任务「{before}」不能依赖自身")
        if before not in task_ids or after not in task_ids:
            raise ValueError(f"前置依赖引用了不存在的任务 ID（{before} -> {after}）")
        if (before, after) not in seen_edges:
            seen_edges.add((before, after))
            preds[after].append(before)
            succs[before].append(after)
            edges.append({"before": before, "after": after})
    # Kahn topological check for cycles
    indeg = {tid: len(preds[tid]) for tid in task_ids}
    queue = sorted(tid for tid, deg in indeg.items() if deg == 0)
    visited = 0
    while queue:
        cur = queue.pop(0)
        visited += 1
        for nxt in succs[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if visited != len(task_ids):
        raise ValueError("任务前置依赖存在循环，无法排程")
    return preds, succs, edges


def _allocate_split_greedy(
    slots: list[Interval], release: datetime, deadline: datetime, required_min: int, min_block: int
) -> tuple[list[Interval], list[Interval]] | None:
    placed: list[Interval] = []
    left = required_min
    for s_start, s_end in slots:
        start = max(s_start, release)
        end = min(s_end, deadline)
        cap = _minutes(start, end)
        if cap < min_block:
            continue
        take = min(cap, left)
        if 0 < left - take < min_block:
            take = left - min_block
        if take < min_block:
            continue
        placed.append((start, start + timedelta(minutes=take)))
        left -= take
        if left == 0:
            break
    if left > 0:
        return None
    return placed, _cut_intervals(slots, placed)


def _candidate_placements(
    slots: list[Interval],
    release: datetime,
    deadline: datetime,
    required_min: int,
    splittable: bool,
    min_block: int,
    step_min: int = DEFAULT_STEP_MINUTES,
    focus_intervals: list[dict[str, Any]] | None = None,
    preferred_start: datetime | None = None,
) -> list[tuple[list[Interval], list[Interval]]]:
    """Generate exact candidate placements for one task: earliest, preferred/baseline, focus-aligned, late-aligned, stepped."""
    candidates: list[tuple[list[Interval], list[Interval]]] = []
    seen_keys: set[tuple[tuple[datetime, datetime], ...]] = set()
    req_td = timedelta(minutes=required_min)

    def add(blocks: list[Interval]) -> None:
        key = tuple(blocks)
        if key not in seen_keys:
            seen_keys.add(key)
            candidates.append((blocks, _cut_intervals(slots, blocks)))

    # 1. Single-block placements across all feasible slots (earliest, preferred-start, focus-aligned, end-aligned, stepped).
    for s_start, s_end in slots:
        start = max(s_start, release)
        end = min(s_end, deadline)
        cap = _minutes(start, end)
        if cap < required_min:
            continue
        # Keep original start position if it still fits cleanly in this slot (crucial for minimal_change)
        if preferred_start is not None:
            clamped = max(start, min(preferred_start, end - req_td))
            add([(clamped, clamped + req_td)])
        # Earliest placement in this slot
        add([(start, start + req_td)])
        # Focus-window aligned placements inside this slot so high-cognitive tasks can land on focus windows
        for fw in focus_intervals or ():
            fw_s = max(start, fw["start"])
            fw_e = min(end, fw["end"])
            if fw_e > fw_s:
                if fw_s + req_td <= end:
                    add([(fw_s, fw_s + req_td)])
                if fw_e - req_td >= start:
                    add([(fw_e - req_td, fw_e)])
        # End-aligned placement in this slot (crucial for leaving clean front room for other tasks)
        if cap > required_min:
            add([(end - req_td, end)])
        # Intermediate stepped placements (bounded to keep branching compact)
        if cap >= required_min + step_min:
            offset = step_min
            added_steps = 0
            while offset <= cap - required_min and added_steps < 4:
                p_start = start + timedelta(minutes=offset)
                add([(p_start, p_start + req_td)])
                offset += step_min
                added_steps += 1

    # 2. If splittable, also include exact earliest-fit split placement.
    if splittable:
        split_fit = _allocate_split_greedy(slots, release, deadline, required_min, min_block)
        if split_fit is not None:
            add(split_fit[0])

    return candidates


def _verify_solution(
    tasks_by_id: dict[str, dict],
    free_slots: list[Interval],
    placement: dict[str, list[Interval]],
    edges: list[dict[str, str]],
    reference: datetime,
    locked_rests: list[Interval] | None = None,
) -> dict[str, Any]:
    """Independent zero-shared-logic verifier for exact solver schedules."""
    errors: list[str] = []
    all_blocks: list[tuple[datetime, datetime, str]] = []
    rest_list = locked_rests or []
    for tid, task in tasks_by_id.items():
        blocks = placement.get(tid, [])
        if not blocks:
            errors.append(f"任务 {tid} 未被安排")
            continue
        total = 0
        release = max(reference, _instant(task["earliest_start_at"], "earliest")) if task.get("earliest_start_at") else reference
        deadline = _instant(task["deadline_at"], "deadline")
        if not task["splittable"] and len(blocks) > 1:
            errors.append(f"不可拆任务 {tid} 被拆成多段")
        min_block = task.get("min_block_minutes") or DEFAULT_MIN_BLOCK
        for start, end in blocks:
            dur = _minutes(start, end)
            if dur <= 0:
                errors.append(f"任务 {tid} 存在非正时长分段")
            if len(blocks) > 1 and dur < min_block:
                errors.append(f"任务 {tid} 分段 {dur} 分钟小于最短连续块 {min_block} 分钟")
            if start < release or end > deadline:
                errors.append(f"任务 {tid} 超出允许窗口 [{release.isoformat()}, {deadline.isoformat()}]")
            if not any(fs <= start and end <= fe for fs, fe in free_slots):
                errors.append(f"任务 {tid} 的时段 {start.isoformat()}–{end.isoformat()} 不在可用留白内")
            if any(start < re_end and end > re_start for re_start, re_end in rest_list):
                errors.append(f"任务 {tid} 的时段 {start.isoformat()}–{end.isoformat()} 侵犯了用户锁定的休息区间")
            total += dur
            all_blocks.append((start, end, tid))
        if total != task["estimate_minutes"]:
            errors.append(f"任务 {tid} 安排总时长 {total} 分钟不等于预计 {task['estimate_minutes']} 分钟")
    all_blocks.sort(key=lambda b: (b[0], b[1]))
    for idx in range(len(all_blocks) - 1):
        if all_blocks[idx][1] > all_blocks[idx + 1][0]:
            errors.append(f"任务 {all_blocks[idx][2]} 与 {all_blocks[idx + 1][2]} 发生时间重叠")
    for edge in edges:
        b_blocks = placement.get(edge["before"], [])
        a_blocks = placement.get(edge["after"], [])
        if b_blocks and a_blocks and max(e for _, e in b_blocks) > min(s for s, _ in a_blocks):
            errors.append(f"违反前置依赖：{edge['before']} 晚于 {edge['after']} 开始")
    checked = [
        "inside_derived_free_time",
        "no_work_overlap",
        "exact_estimate_minutes",
        "earliest_and_deadline_bounds",
        "splittable_and_min_block",
        "dag_precedence_dependencies",
    ]
    if rest_list:
        checked.append("respects_locked_rest_blocks")
    return {
        "passed": not errors,
        "errors": errors,
        "checked_constraints": checked,
    }


def _solve_exact(
    tasks: list[dict],
    free_slots: list[Interval],
    preds: dict[str, list[str]],
    succs: dict[str, list[str]],
    edges: list[dict[str, str]],
    reference: datetime,
    *,
    focus_intervals: list[dict[str, Any]] | None = None,
    cognitive_loads: dict[str, str] | None = None,
    locked_rests: list[Interval] | None = None,
    blocking_items: list[dict[str, Any]] | None = None,
    baseline_starts: dict[str, datetime] | None = None,
    objective: str = "default",
) -> dict[str, Any]:
    """Exact branch-and-bound constraint search over schedulable active tasks."""
    focus_list = focus_intervals or []
    loads = cognitive_loads or {}
    base_starts = baseline_starts or {}
    effective_obj = "focus_first" if objective == "default" and focus_list else objective
    if not tasks:
        return {
            "status": "optimal",
            "nodes_explored": 0,
            "placement": {},
            "focus_score": 0,
            "missing_windows": [],
            "validation": _verify_solution({}, free_slots, {}, edges, reference, locked_rests=locked_rests),
            "reason": "当前没有需要排程的待办任务。",
        }
    tasks_by_id = {t["id"]: t for t in tasks}
    deadlines_by_id = {t["id"]: _instant(t["deadline_at"], "deadline") for t in tasks}
    # Quick total capacity lower-bound check
    total_free = sum(_minutes(s, e) for s, e in free_slots)
    total_need = sum(t["estimate_minutes"] for t in tasks)
    if total_need > total_free:
        missing = diagnose_missing_windows(tasks, free_slots, blocking_items or [], reference)
        return {
            "status": "infeasible",
            "nodes_explored": 1,
            "placement": {},
            "focus_score": 0,
            "missing_windows": missing,
            "validation": {"passed": False, "errors": ["可用留白总时长不足以容纳全部任务"], "checked_constraints": []},
            "shortage_minutes": total_need - total_free,
            "reason": f"可用留白共 {total_free} 分钟，小于任务总需 {total_need} 分钟（至少差 {total_need - total_free} 分钟）。",
        }

    # Propagate tightened latest deadlines backwards through DAG
    latest = dict(deadlines_by_id)
    indeg = {tid: len(preds[tid]) for tid in tasks_by_id}
    topo: list[str] = []
    q = sorted(tid for tid, d in indeg.items() if d == 0)
    while q:
        u = q.pop(0)
        topo.append(u)
        for v in succs[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    for tid in reversed(topo):
        limit = latest[tid] - timedelta(minutes=tasks_by_id[tid]["estimate_minutes"])
        for dep in preds[tid]:
            latest[dep] = min(latest[dep], limit)

    nodes = 0
    budget_hit = False
    best_solution: dict[str, list[Interval]] | None = None
    best_score: tuple[int, ...] | None = None
    best_focus_score = 0

    def rank_solution(placed: dict[str, list[Interval]]) -> tuple[tuple[int, ...], int]:
        frags = sum(max(0, len(blks) - 1) for blks in placed.values())
        makespan = max((_minutes(reference, max(e for _, e in blks)) for blks in placed.values()), default=0)
        f_score = (
            sum(score_task_focus(blks, loads.get(tid, "medium"), focus_list)["score"] for tid, blks in placed.items())
            if focus_list
            else 0
        )
        buffers = [_minutes(max(e for _, e in blks), deadlines_by_id[tid]) for tid, blks in placed.items()]
        min_buf = min(buffers) if buffers else 0
        total_buf = sum(buffers)
        moved_mins = 0
        moved_cnt = 0
        for tid, blks in placed.items():
            if tid in base_starts and blks:
                delta = abs(_minutes(base_starts[tid], blks[0][0]))
                moved_mins += delta
                if delta > 0:
                    moved_cnt += 1
        if effective_obj == "minimal_change":
            return (moved_mins, moved_cnt, frags, -f_score, -min_buf, makespan), f_score
        if effective_obj == "focus_first":
            return (-f_score, frags, -min_buf, moved_mins, makespan), f_score
        if effective_obj == "buffer_first":
            return (-min_buf, frags, -total_buf, makespan, -f_score), f_score
        return (frags, makespan), f_score

    def search(
        remaining_ids: set[str],
        cur_slots: list[Interval],
        placed: dict[str, list[Interval]],
        end_times: dict[str, datetime],
    ) -> None:
        nonlocal nodes, budget_hit, best_solution, best_score, best_focus_score
        if not remaining_ids:
            score, f_score = rank_solution(placed)
            if best_score is None or score < best_score:
                best_score = score
                best_focus_score = f_score
                best_solution = {tid: list(blks) for tid, blks in placed.items()}
            return
        if nodes >= MAX_SEARCH_NODES:
            budget_hit = True
            return
        # Lower-bound pruning: remaining free minutes must cover remaining required minutes
        rem_free = sum(_minutes(s, e) for s, e in cur_slots)
        rem_need = sum(tasks_by_id[tid]["estimate_minutes"] for tid in remaining_ids)
        if rem_need > rem_free:
            return
        # Ready tasks (all predecessors placed)
        ready = [tid for tid in remaining_ids if all(dep in end_times for dep in preds[tid])]
        if not ready:
            return
        # Most-Constrained-Variable (MRV) ordering: pick ready task with fewest candidate placements first!
        scored_ready: list[tuple[int, datetime, str, list[tuple[list[Interval], list[Interval]]]]] = []
        for tid in ready:
            task = tasks_by_id[tid]
            rel = max([reference, *(end_times[d] for d in preds[tid])])
            if task.get("earliest_start_at"):
                rel = max(rel, _instant(task["earliest_start_at"], "earliest"))
            dl = latest[tid]
            if rel >= dl:
                return  # Dead end immediately
            cands = _candidate_placements(
                cur_slots,
                rel,
                dl,
                task["estimate_minutes"],
                bool(task["splittable"]),
                task.get("min_block_minutes") or DEFAULT_MIN_BLOCK,
                focus_intervals=focus_list,
                preferred_start=base_starts.get(tid),
            )
            if not cands:
                return  # Dead end immediately
            if effective_obj == "minimal_change" and tid in base_starts:
                orig_s = base_starts[tid]
                cands.sort(key=lambda item: (abs(_minutes(orig_s, item[0][0][0])), item[0][0][0]))
            elif effective_obj == "focus_first" and focus_list:
                c_load = loads.get(tid, "medium")
                cands.sort(key=lambda item: (-score_task_focus(item[0], c_load, focus_list)["score"], item[0][0][0]))
            scored_ready.append((len(cands), dl, tid, cands))
        scored_ready.sort(key=lambda item: (item[0], item[1], item[2]))
        _, _, pick_tid, pick_cands = scored_ready[0]
        next_remaining = remaining_ids - {pick_tid}
        for blocks, next_slots in pick_cands:
            nodes += 1
            if nodes > MAX_SEARCH_NODES:
                budget_hit = True
                return
            placed[pick_tid] = blocks
            end_times[pick_tid] = max(e for _, e in blocks)
            search(next_remaining, next_slots, placed, end_times)
            del placed[pick_tid]
            del end_times[pick_tid]
            # Early stop: allow wider exploration when ranking by focus/buffer/minimal_change
            stop_limit = 256 if effective_obj in {"focus_first", "buffer_first", "minimal_change"} else 64
            if best_score is not None and effective_obj == "default" and best_score[0] == 0 and nodes >= stop_limit:
                return
            if best_score is not None and effective_obj != "default" and nodes >= stop_limit:
                return

    search(set(tasks_by_id), list(free_slots), {}, {})
    if best_solution is not None:
        validation = _verify_solution(tasks_by_id, free_slots, best_solution, edges, reference, locked_rests=locked_rests)
        return {
            "status": "optimal" if not budget_hit else "feasible",
            "nodes_explored": nodes,
            "placement": best_solution,
            "focus_score": best_focus_score,
            "missing_windows": [],
            "validation": validation,
            "reason": "精确约束求解器已找到并通过独立校验的可行排程。" if validation["passed"] else "独立校验未通过。",
        }
    missing = diagnose_missing_windows(tasks, free_slots, blocking_items or [], reference)
    return {
        "status": "budget_exhausted" if budget_hit else "infeasible",
        "nodes_explored": nodes,
        "placement": {},
        "focus_score": 0,
        "missing_windows": missing,
        "validation": {"passed": False, "errors": ["未找到满足全部约束的排程"], "checked_constraints": []},
        "reason": "达到搜索节点上限仍未找到解，不能断言数学无解。" if budget_hit else "在给定留白、连续块、锁定休息、通勤与前置依赖约束下无可行解。",
    }


def _apply_strategy(snapshot: dict, strategy: dict) -> dict:
    """Apply one v3 strategy action to a detached snapshot copy."""
    nxt = deepcopy(snapshot)
    action = strategy["action"]
    params = strategy.get("params") or {}
    if action == "cancel_event":
        for ev in nxt["events"]:
            if ev["id"] == params.get("event_id"):
                ev["status"] = "cancelled"
    elif action == "skip_routine_date":
        for rt in nxt["routines"]:
            if rt["id"] == params.get("routine_id"):
                day = params.get("date")
                if day and day not in rt["skip_dates"]:
                    rt["skip_dates"].append(day)
    elif action == "move_event":
        for ev in nxt["events"]:
            if ev["id"] == params.get("event_id"):
                ev["start_at"] = params["start_at"]
                ev["end_at"] = params["end_at"]
    elif action == "update_task":
        for task in nxt["tasks"]:
            if task["id"] == params.get("task_id"):
                task.update(params.get("patch") or {})
    elif action == "add_free":
        nxt["free_adjustments"].append({
            "id": f"fa-solve-{len(nxt['free_adjustments']) + 1}",
            "kind": "add",
            "start_at": params["start_at"],
            "end_at": params["end_at"],
            "note": params.get("note") or "求解器建议补充留白",
        })
    nxt["revision"] = int(nxt.get("revision", 0)) + 1
    return nxt


def _pareto_plans(
    snapshot: dict,
    date_range: dict[str, str],
    base_cal: dict,
    transit_minutes: int,
) -> list[dict]:
    """Generate Pareto-non-dominated trade-off plans for actionable conflicts."""
    actionable = [
        conflict for conflict in base_cal["conflicts"]
        if any(s["action"] != "review" for s in conflict.get("strategies", []))
    ][:MAX_PARETO_CONFLICTS]
    if not actionable:
        return []

    # Enumerate concrete strategy combinations (bounded <= 2^4 = 16)
    candidates: list[tuple[list[dict], dict]] = [([], deepcopy(snapshot))]
    for conflict in actionable:
        concrete = [s for s in conflict["strategies"] if s["action"] != "review"][:2]
        next_cands: list[tuple[list[dict], dict]] = []
        for actions, snap in candidates:
            for strat in concrete:
                next_cands.append(([*actions, strat], _apply_strategy(snap, strat)))
        candidates = next_cands

    evaluated: list[dict] = []
    for actions, snap in candidates:
        cal = calendar_snapshot({"snapshot": snap, "range": date_range})
        free_mins = sum(_minutes(_instant(f["start"], "s"), _instant(f["end"], "e")) for f in cal["layers"]["free"])
        work_mins = sum(
            _minutes(_instant(w["start"], "s"), _instant(w["end"], "e"))
            for w in cal["layers"]["work"]
            if w.get("kind") != "appointment"
        )
        net_free = free_mins - work_mins
        longest_free = max(
            (_minutes(_instant(f["start"], "s"), _instant(f["end"], "e")) for f in cal["layers"]["free"]),
            default=0,
        )
        kept_temp = sum(1 for ev in snap["events"] if ev["status"] == "active" and ev.get("kind", "temporary") == "temporary")
        skipped_routines = sum(len(rt["skip_dates"]) for rt in snap["routines"])
        transit_warns, _ = _detect_transit_warnings_and_buffers(cal, transit_minutes)
        evaluated.append({
            "actions": [
                {"action": a["action"], "label": a["label"], "detail": a["detail"], "params": a["params"]}
                for a in actions
            ],
            "metrics": {
                "remaining_conflicts": len(cal["conflicts"]),
                "net_free_minutes": net_free,
                "longest_free_minutes": longest_free,
                "kept_temporary_events": kept_temp,
                "skipped_routine_occurrences": skipped_routines,
                "transit_warnings": len(transit_warns),
            },
            "work_blocks": [
                {"id": w.get("task_id") or w.get("id"), "title": w["title"], "start": w["start"], "end": w["end"]}
                for w in cal["layers"]["work"]
            ],
            "validation_passed": cal["validation"]["passed"],
        })

    # Pareto dominance filter maximizing:
    # (-remaining_conflicts, net_free_minutes, kept_temporary_events, -skipped_routine_occurrences, -transit_warnings)
    def vec(item: dict) -> tuple[int, int, int, int, int]:
        m = item["metrics"]
        return (
            -m["remaining_conflicts"],
            m["net_free_minutes"],
            m["kept_temporary_events"],
            -m["skipped_routine_occurrences"],
            -m["transit_warnings"],
        )

    pareto: list[dict] = []
    for idx, item in enumerate(evaluated):
        v = vec(item)
        dominated = False
        for jdx, other in enumerate(evaluated):
            if idx == jdx:
                continue
            ov = vec(other)
            if all(o >= x for o, x in zip(ov, v)) and any(o > x for o, x in zip(ov, v)):
                dominated = True
                break
        if not dominated:
            pareto.append(item)

    # Sort & label clearly
    pareto.sort(key=lambda it: (it["metrics"]["remaining_conflicts"], -it["metrics"]["net_free_minutes"]))
    for plan in pareto:
        m = plan["metrics"]
        if m["skipped_routine_occurrences"] == 0 and m["remaining_conflicts"] == 0:
            plan["name"] = "保作息与课表方案"
            plan["summary"] = f"叠墨清零，保留全部课表并最大化留白（本周排后可用 {m['net_free_minutes']} 分钟）。"
        elif m["kept_temporary_events"] > 0 and m["remaining_conflicts"] == 0:
            plan["name"] = "保临时活动方案"
            plan["summary"] = f"叠墨清零，优先参加临时点墨活动（需请假 {m['skipped_routine_occurrences']} 次课，本周排后可用 {m['net_free_minutes']} 分钟）。"
        else:
            plan["name"] = "折中权衡方案"
            plan["summary"] = f"剩余叠墨 {m['remaining_conflicts']} 处，本周排后可用 {m['net_free_minutes']} 分钟。"
    return pareto[:3]


def _collect_blocking_items(cal_result: dict, rest_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for layer_name, kind_label in (("routine", "routine"), ("temporary", "temporary")):
        for item in cal_result["layers"][layer_name]:
            blockers.append({
                "title": item["title"],
                "kind": kind_label,
                "start": item["start"],
                "end": item["end"],
                "start_dt": _instant(item["start"], "start"),
                "end_dt": _instant(item["end"], "end"),
            })
    for item in cal_result["layers"].get("appointment", []):
        blockers.append({
            "title": item["title"],
            "kind": "appointment",
            "start": item["start"],
            "end": item["end"],
            "start_dt": _instant(item["start"], "start"),
            "end_dt": _instant(item["end"], "end"),
        })
    for rb in rest_blocks:
        if rb["locked"]:
            blockers.append({
                "title": f"锁定休息：{rb['label']}",
                "kind": "locked_rest",
                "start": rb["start"],
                "end": rb["end"],
                "start_dt": rb["start_dt"],
                "end_dt": rb["end_dt"],
            })
    blockers.sort(key=lambda it: (it["start_dt"], it["end_dt"]))
    return blockers


def solve_snapshot(payload: object) -> dict[str, Any]:
    """POST /api/v3/solve entry point: exact constraint solver + focus matching + locked rests + transit buffers + Pareto plans."""
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise ValueError("请求必须包含 v3 snapshot 对象")
    snapshot = payload["snapshot"]
    validate_snapshot_v3(snapshot)
    date_range = payload.get("range") or _default_range(snapshot)
    start_day = date.fromisoformat(date_range["from"])
    end_day = date.fromisoformat(date_range["to"])
    if end_day < start_day or (end_day - start_day).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"range 无效或超过 {MAX_RANGE_DAYS} 天上限")

    transit_minutes = payload.get("transit_minutes", 0)
    if not isinstance(transit_minutes, int) or isinstance(transit_minutes, bool) or not (0 <= transit_minutes <= 120):
        raise ValueError("transit_minutes 必须是 0 到 120 之间的整数")

    objective = payload.get("objective", "default")
    if objective not in {"default", "focus_first", "buffer_first", "minimal_change"}:
        raise ValueError("objective 只能是 default、focus_first、buffer_first 或 minimal_change")
    raw_baseline = payload.get("baseline_starts") or {}
    if not isinstance(raw_baseline, dict):
        raise ValueError("baseline_starts 必须是按 task_id 索引的对象")
    baseline_starts: dict[str, datetime] = {
        tid: _instant(val, f"baseline_starts.{tid}")
        for tid, val in raw_baseline.items()
        if isinstance(val, str) and val
    }

    # Derive base calendar layers (reuses v3 routine/temporary/free derivation)
    cal_req: dict[str, Any] = {"snapshot": snapshot, "range": date_range}
    if payload.get("now") is not None:
        cal_req["now"] = payload["now"]
    base_cal = calendar_snapshot(cal_req)
    transit_warnings, transit_buffers = _detect_transit_warnings_and_buffers(base_cal, transit_minutes)

    reference = _instant(snapshot["reference_time"], "reference_time")
    # Collect active schedulable tasks (with deadline & estimate)
    schedulable = [
        task for task in snapshot["tasks"]
        if task["status"] == "active" and task["deadline_at"] and task["estimate_minutes"] is not None
    ]
    if len(schedulable) > MAX_PLANNED_TASKS:
        raise ValueError(f"单次精确求解最多支持 {MAX_PLANNED_TASKS} 件待办任务")
    schedulable_ids = {t["id"] for t in schedulable}
    preds, succs, edges = _validate_dependencies(payload.get("dependencies"), schedulable_ids)

    focus_intervals = parse_focus_windows(payload.get("focus_windows"), start_day, end_day)
    cognitive_loads = parse_task_profiles(payload.get("task_profiles"), schedulable)
    rest_blocks = parse_rest_blocks(payload.get("rest_blocks"))
    locked_rests = [(rb["start_dt"], rb["end_dt"]) for rb in rest_blocks if rb["locked"]]
    blocking_items = _collect_blocking_items(base_cal, rest_blocks)

    # Free intervals from base calendar, deducting transit buffers and user-locked rest intervals (hard constraints)
    raw_free = [(_instant(f["start"], "s"), _instant(f["end"], "e")) for f in base_cal["layers"]["free"]]
    hard_deductions = merge_intervals([*transit_buffers, *locked_rests])
    effective_free = _cut_intervals(raw_free, hard_deductions) if hard_deductions else raw_free

    exact = _solve_exact(
        schedulable,
        effective_free,
        preds,
        succs,
        edges,
        reference,
        focus_intervals=focus_intervals,
        cognitive_loads=cognitive_loads,
        locked_rests=locked_rests,
        blocking_items=blocking_items,
        baseline_starts=baseline_starts,
        objective=objective,
    )
    schedule_entries: list[dict] = []
    focus_breakdown: dict[str, dict[str, Any]] = {}
    if exact["validation"]["passed"]:
        tasks_by_id = {t["id"]: t for t in schedulable}
        for tid, blocks in exact["placement"].items():
            dl = _instant(tasks_by_id[tid]["deadline_at"], "deadline")
            last_end = max(e for _, e in blocks)
            buf = _minutes(last_end, dl)
            f_info = score_task_focus(blocks, cognitive_loads.get(tid, "medium"), focus_intervals)
            focus_breakdown[tid] = f_info
            for s, e in blocks:
                schedule_entries.append({
                    "task_id": tid,
                    "title": tasks_by_id[tid]["title"],
                    "start": _iso(s),
                    "end": _iso(e),
                    "minutes": _minutes(s, e),
                    "cognitive_load": cognitive_loads.get(tid, "medium"),
                    "deadline_buffer_minutes": buf,
                    "focus_score": f_info["score"],
                })
        schedule_entries.sort(key=lambda item: (item["start"], item["end"], item["task_id"]))

    pareto = _pareto_plans(snapshot, date_range, base_cal, transit_minutes)

    return {
        "schema_version": 1,
        "revision": snapshot["revision"],
        "range": date_range,
        "algorithm": {
            "name": "exact_branch_and_bound_v1",
            "status": exact["status"],
            "nodes_explored": exact["nodes_explored"],
            "max_nodes": MAX_SEARCH_NODES,
            "transit_minutes": transit_minutes,
            "dependency_count": len(edges),
            "focus_window_count": len(focus_intervals),
            "locked_rest_count": len(locked_rests),
            "objective": "focus_first" if objective == "default" and focus_intervals else objective,
        },
        "schedule": schedule_entries,
        "validation": exact["validation"],
        "reason": exact["reason"],
        "focus_summary": {
            "total_score": exact.get("focus_score", 0),
            "by_task": focus_breakdown,
        },
        "missing_windows": exact.get("missing_windows", []),
        "transit_warnings": transit_warnings,
        "pareto_plans": pareto,
        "base_conflict_count": len(base_cal["conflicts"]),
    }
