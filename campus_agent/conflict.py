"""Necessary capacity checks, not a complete scheduling feasibility solver."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .models import ConflictTask
from .time_utils import DEFAULT_TIMEZONE, get_zone, parse_dt, require_datetime

Interval = tuple[datetime, datetime]
EPSILON = 1e-7


def overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if end <= start:
            raise ValueError("时间区间结束必须晚于开始")
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def free_intervals(start: datetime, end: datetime, availability: list[Interval] | None, fixed: list[Interval]) -> list[Interval]:
    if end <= start:
        return []
    source = [(start, end)] if availability is None else availability
    clipped = [(max(a, start), min(b, end)) for a, b in source if a < end and b > start]
    busy = merge_intervals(fixed)
    busy_index = 0
    result: list[Interval] = []
    for a, b in merge_intervals(clipped):
        cursor = a
        while busy_index < len(busy) and busy[busy_index][1] <= a:
            busy_index += 1
        for busy_start, busy_end in busy[busy_index:]:
            if busy_end <= cursor:
                continue
            if busy_start >= b:
                break
            if busy_start > cursor:
                result.append((cursor, busy_start))
            cursor = max(cursor, busy_end)
            if cursor >= b:
                break
        if cursor < b:
            result.append((cursor, b))
    return result


def working_capacity(start: datetime, end: datetime, *, availability: list[Interval] | None = None, fixed: list[Interval] | None = None) -> float:
    """Hours after interval clipping/subtraction. None is a wall-clock upper bound.

    An omitted availability is NOT evidence of personal availability, and callers
    must not interpret this upper bound as a feasible plan.
    """
    return sum((b - a).total_seconds() for a, b in free_intervals(start, end, availability, fixed or [])) / 3600


def read_intervals(data: object, field: str, timezone: str) -> list[Interval]:
    if not isinstance(data, list) or len(data) > 1000:
        raise ValueError(f"{field} 必须是最多1000项的区间数组")
    result = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or set(item) != {"start", "end"}:
            raise ValueError(f"{field}[{index}] 必须包含且仅包含start和end")
        start = require_datetime(item["start"], f"{field}[{index}].start", timezone)
        end = require_datetime(item["end"], f"{field}[{index}].end", timezone)
        if end <= start:
            raise ValueError(f"{field}[{index}] 结束必须晚于开始")
        result.append((start, end))
    return merge_intervals(result)


def detect_conflicts(tasks_data: list[dict[str, Any]], now: str | None = None, *, availability: list[dict[str, str]] | None = None, timezone: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
    zone = get_zone(timezone)
    if not isinstance(tasks_data, list) or len(tasks_data) > 500:
        raise ValueError("tasks 必须是最多500项的任务数组")
    ref_now = datetime.now(zone) if now is None else require_datetime(now, "now", timezone)
    tasks = [ConflictTask.from_dict(item) for item in tasks_data]
    ids = [task.task_id for task in tasks if task.task_id]
    if len(ids) != len(set(ids)):
        raise ValueError("非空task_id不可重复，不能重复累计同一任务")
    available = None if availability is None else read_intervals(availability, "availability", timezone)
    warnings: list[str] = []
    missing: list[dict[str, Any]] = []
    if available is None:
        warnings.append("未提供个人空闲时段；容量仅为扣除已知固定占用后的物理时间上限，包含睡眠等未知占用，不能据此确认可行。")
    fixed = []
    deadlines: dict[int, datetime] = {}
    for index, task in enumerate(tasks):
        if task.deadline is not None:
            deadlines[index] = require_datetime(task.deadline, f"tasks[{index}].deadline", timezone)
        if task.fixed_time:
            start = require_datetime(task.fixed_time.start, f"tasks[{index}].fixed_time.start", timezone)
            end = require_datetime(task.fixed_time.end, f"tasks[{index}].fixed_time.end", timezone)
            if end <= start:
                raise ValueError(f"tasks[{index}].fixed_time 结束必须晚于开始")
            if task.status not in {"completed", "cancelled"} and end > ref_now:
                fixed.append((task, max(start, ref_now), end))
            if task.estimated_hours or task.remaining_minutes:
                warnings.append(f"{task.name}：固定事项只计占用区间，不另加预计耗时；若有准备工作，请拆成独立任务。")
        elif task.status not in {"completed", "cancelled"}:
            if not task._remaining_supplied and task.estimated_hours is not None:
                warnings.append(f"{task.name}：未提供剩余分钟，暂将预计耗时全部作为剩余工作量，需核对进度。")
            fields = []
            if task.work_minutes is None:
                fields.append("remaining_minutes或estimated_hours")
            if index not in deadlines and task.work_minutes != 0:
                fields.append("deadline")
            if fields:
                missing.append({"task_id": task.task_id, "name": task.name, "fields": fields})

    conflicts: list[dict[str, Any]] = []
    for i, (a, a_start, a_end) in enumerate(fixed):
        for b, b_start, b_end in fixed[i + 1:]:
            if overlap(a_start, a_end, b_start, b_end):
                conflicts.append({
                    "type": "hard_time_overlap", "severity": "high" if a.must_do and b.must_do else "medium",
                    "tasks": [a.name, b.name], "task_ids": [a.task_id, b.task_id],
                    "reason": f"{a.name} 与 {b.name} 的固定时间重叠，必须改期或由用户决定取舍。",
                })

    active = [(i, t) for i, t in enumerate(tasks) if t.status not in {"completed", "cancelled"} and not t.fixed_time]
    for index, task in active:
        deadline = deadlines.get(index)
        if deadline is not None and deadline <= ref_now and task.work_minutes != 0:
            conflicts.append({"type": "deadline_passed", "severity": "high", "tasks": [task.name], "task_ids": [task.task_id], "reason": f"{task.name} 截止时间已到且未标记完成；不能继续按未来可用时间计算准时完成。"})

    checks = []
    busy = [(a, b) for _, a, b in fixed]
    for deadline in sorted({deadlines[i] for i, t in active if i in deadlines and deadlines[i] > ref_now and t.work_minutes != 0}):
        due = [(i, t) for i, t in active if i in deadlines and deadlines[i] <= deadline and t.work_minutes != 0]
        known = [t for _, t in due if t.work_minutes is not None]
        needed = sum(t.work_minutes for t in known)
        free = free_intervals(ref_now, deadline, available, busy)
        capacity = sum((b - a).total_seconds() / 60 for a, b in free)
        insufficient = needed > capacity + EPSILON
        # Volume is only a necessary condition. Fragmentation, dependencies,
        # travel and minimum blocks are not yet tested by this endpoint.
        checks.append({
            "deadline": deadline.isoformat(), "task_ids": [t.task_id for _, t in due],
            "known_required_minutes": round(needed, 6), "available_minutes": round(capacity, 6),
            "unknown_workload_count": len(due) - len(known),
            "basis": "provided_availability" if available is not None else "physical_upper_bound",
            "result": "shortage" if insufficient else "necessary_bound_passed",
            "free_intervals": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in free],
        })
        if insufficient:
            conflicts.append({
                "type": "capacity_shortage", "severity": "high", "tasks": [t.name for t in known], "task_ids": [t.task_id for t in known],
                "reason": f"截至 {deadline:%Y-%m-%d %H:%M}，按所填耗时累计需 {needed:g} 分钟，{'所填空闲时段' if available is not None else '物理时间上限'}扣除固定占用后仅 {capacity:g} 分钟。",
            })
        elif available is not None and capacity > 0 and needed > capacity * .9:
            warnings.append(f"截至 {deadline:%Y-%m-%d %H:%M}，已知工作量超过可用时长的90%，缓冲较少；这不是已经证明的容量不足。")

    status = "conflicts_detected" if conflicts else "information_needed" if available is None or missing else "no_conflict_detected"
    return {
        "tasks": [task.to_dict() for task in tasks], "conflicts": conflicts,
        "recommendations": recommend(tasks, conflicts), "status": status,
        "reference_time": ref_now.isoformat(), "timezone": timezone,
        "capacity_checks": checks, "missing_information": missing,
        "warnings": list(dict.fromkeys(warnings)),
        "limitations": ["仅检查固定重叠和截止前累计工作量；未验证不可拆分时长、依赖、办理窗口、路程或完整排程，因此不输出‘可行’结论。"],
    }


def recommend(tasks: list[ConflictTask], conflicts: list[dict[str, Any]]) -> dict[str, list[str]]:
    implicated_ids = {task_id for item in conflicts for task_id in item["task_ids"] if task_id}
    unnamed_ids = {name for item in conflicts for name, task_id in zip(item["tasks"], item["task_ids"]) if not task_id}
    keep, adjust, drop = [], [], []
    for task in sorted(tasks, key=lambda item: (not item.must_do, -item.priority)):
        if task.status in {"completed", "cancelled"}:
            continue
        implicated = task.task_id in implicated_ids if task.task_id else task.name in unnamed_ids
        if not implicated:
            keep.append(task.name)
        elif task.must_do:
            keep.append(task.name)
            adjust.append(f"{task.name}：必须完成且存在冲突，请确认改期、缩减范围或寻求协作，优先级排序本身不能消除冲突。")
        elif task.priority >= 55:
            adjust.append(f"{task.name}：存在冲突，可考虑调整时间或范围，由你确认。")
        else:
            drop.append(f"{task.name}：存在冲突，可考虑延期或委托，是否放弃由你确认。")
    return {"keep": keep, "adjust": adjust, "drop_or_delegate": drop}
