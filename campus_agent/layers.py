"""Four time layers on a calendar (snapshot v3): routine, free, work, temporary.

User-facing names (书画): routine 底色, free 留白, work 落笔 (deadline work plus
appointments the user makes), temporary 点墨, conflict 叠墨, undefined 待书.

Free time is derived (daily window minus routine and temporary occupancy, plus
manual adjustments). Work is placed only inside free time by the v2 planner and
checked by its independent validator. Conflicts are judged only among routine,
work and temporary layers. Missing inputs stay "undefined" instead of blocking
other items; nothing here calls a model or stores the snapshot.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .conflict import free_intervals, merge_intervals
from .planner import plan_snapshot
from .snapshot import _instant
from .strategies import attach_strategies
from .time_utils import get_zone

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/task-snapshot.v3.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
MAX_RANGE_DAYS = 31
MAX_HORIZON_DAYS = 31
MAX_PLANNED_TASKS = 40
MAX_FREE_INTERVALS = 200
DEFAULT_PRIORITY = 50
DEFAULT_MIN_BLOCK = 30
LIMITATIONS = [
    "工作时间按截止从早到晚逐件放入自由时间，每次都由独立校验器核对；排不下的单独报缺口，不保证全局最优。",
    "自由时间 = 作息范围 − 例行占用 − 临时占用 ± 手动调整；未自动预留路程、吃饭和休息，请用手动调整扣除。",
    "本版不含事项之间的前置依赖，也不在办理窗口内自动择时。",
]
Interval = tuple[datetime, datetime]


def _clock(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _at(day: date, minutes: int) -> datetime:
    return datetime.combine(day, time(0), tzinfo=get_zone()) + timedelta(minutes=minutes)


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _iso(value: datetime) -> str:
    return value.isoformat()


def validate_snapshot_v3(snapshot: object, *, time_issues: dict[str, str] | None = None) -> None:
    """Raise ValueError for malformed or contradictory data; null fields are allowed."""
    error = next(VALIDATOR.iter_errors(snapshot), None)
    if error is not None:
        path = ".".join(str(part) for part in error.absolute_path) or "snapshot"
        raise ValueError(f"v3快照结构无效（{path}，{error.validator}）")
    assert isinstance(snapshot, dict)
    _instant(snapshot["reference_time"], "reference_time")
    sources = {item["id"]: item["text"] for item in snapshot["sources"]}
    if len(sources) != len(snapshot["sources"]):
        raise ValueError("来源ID重复")
    ids = [item["id"] for key in ("routines", "free_adjustments", "events", "tasks") for item in snapshot[key]]
    if len(ids) != len(set(ids)):
        raise ValueError("例行、调整、临时与工作条目的ID必须互不重复")
    for index, window in enumerate(snapshot["daily_window"] or []):
        if window and _clock(window["end"]) <= _clock(window["start"]):
            raise ValueError(f"daily_window[{index}] 结束必须晚于开始")
    for item in snapshot["routines"]:
        if _clock(item["end"]) <= _clock(item["start"]):
            raise ValueError(f"例行占用「{item['title']}」结束必须晚于开始")
        try:
            first = date.fromisoformat(item["from"])
            last = date.fromisoformat(item["until"]) if item["until"] else None
            for skipped in item["skip_dates"]:
                date.fromisoformat(skipped)
        except ValueError as exc:
            raise ValueError(f"例行占用「{item['title']}」日期无效") from exc
        if last and last < first:
            raise ValueError(f"例行占用「{item['title']}」结束日期早于开始日期")
    for item in snapshot["free_adjustments"]:
        if _instant(item["end_at"], "end_at") <= _instant(item["start_at"], "start_at"):
            raise ValueError("自由时间调整的结束必须晚于开始")
    def invalid_time(item: dict, reason: str) -> None:
        if time_issues is None:
            raise ValueError(f"「{item['title']}」{reason}")
        time_issues[item["id"]] = reason

    for item in snapshot["events"]:
        try:
            start = _instant(item["start_at"], "开始") if item["start_at"] else None
            end = _instant(item["end_at"], "结束") if item["end_at"] else None
        except ValueError:
            invalid_time(item, "活动时间无效，请核对起止。")
            continue
        if end and not start:
            invalid_time(item, "只有结束没有开始，请补填开始时间。")
        elif end and end <= start:
            invalid_time(item, "结束必须晚于开始，请核对活动起止。")
    for item in snapshot["tasks"]:
        try:
            earliest = _instant(item["earliest_start_at"], "最早开始") if item["earliest_start_at"] else None
            deadline = _instant(item["deadline_at"], "截止") if item["deadline_at"] else None
        except ValueError:
            invalid_time(item, "任务时间无效，请核对截止和最早开始。")
            continue
        if deadline and earliest and earliest >= deadline:
            invalid_time(item, "最早开始必须早于截止，请核对任务时间。")
        elif item["splittable"] and item["min_block_minutes"] and item["estimate_minutes"] and item["min_block_minutes"] > item["estimate_minutes"]:
            invalid_time(item, "最小连续时长超过预计用时，请核对用时。")
    for item in [*snapshot["routines"], *snapshot["events"], *snapshot["tasks"]]:
        if item["source_id"] is not None and item["source_id"] not in sources:
            raise ValueError(f"「{item['title']}」引用了不存在的来源")
        for evidence in item.get("evidence", []):
            if evidence["origin"] == "source_text":
                text = sources.get(evidence["source_id"])
                if text is None or not evidence["quote"] or evidence["quote"] not in text:
                    raise ValueError(f"「{item['title']}」的原文依据不在对应来源中")


def _range(payload: dict[str, Any], reference: datetime) -> tuple[date, date]:
    requested = payload.get("range")
    if requested is None:
        monday = reference.date() - timedelta(days=reference.isoweekday() - 1)
        return monday, monday + timedelta(days=6)
    if not isinstance(requested, dict) or set(requested) != {"from", "to"}:
        raise ValueError("range 需要 from 与 to 两个日期")
    try:
        first, last = date.fromisoformat(requested["from"]), date.fromisoformat(requested["to"])
    except (TypeError, ValueError) as exc:
        raise ValueError("range 日期必须是 YYYY-MM-DD") from exc
    if last < first or (last - first).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"range 必须从早到晚且最长{MAX_RANGE_DAYS}天")
    return first, last


def _days(start: datetime, end: datetime) -> list[date]:
    day, last = start.date(), end.date()
    days = []
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days


def _routine_occurrences(routines: list[dict], days: list[date]) -> list[dict]:
    occurrences = []
    for item in routines:
        first = date.fromisoformat(item["from"])
        last = date.fromisoformat(item["until"]) if item["until"] else None
        anchor = first - timedelta(days=first.isoweekday() - 1)
        for day in days:
            if day < first or (last and day > last) or day.isoformat() in item["skip_dates"] or day.isoweekday() not in item["weekdays"]:
                continue
            if item["week_interval"] == 2 and ((day - anchor).days // 7) % 2:
                continue
            occurrences.append({"id": f"{item['id']}@{day.isoformat()}", "routine_id": item["id"], "title": item["title"],
                                "start": _at(day, _clock(item["start"])), "end": _at(day, _clock(item["end"])), "location": item["location"]})
    return sorted(occurrences, key=lambda item: (item["start"], item["id"]))


def _subtract(intervals: list[Interval], busy: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    return free_intervals(min(start for start, _ in intervals), max(end for _, end in intervals), intervals, busy)


def _overlaps(left: list[dict], right: list[dict], same: bool) -> list[tuple[dict, dict, datetime, datetime]]:
    found = []
    for index, first in enumerate(left):
        for second in (left[index + 1:] if same else right):
            start, end = max(first["start"], second["start"]), min(first["end"], second["end"])
            if start < end:
                found.append((first, second, start, end))
    return found


def _v2_task(task: dict) -> dict:
    estimate = task["estimate_minutes"]
    minimum = (task["min_block_minutes"] or min(DEFAULT_MIN_BLOCK, estimate)) if task["splittable"] else None
    required = ["title", "kind", "must_do", "priority", "deadline_at", "remaining_minutes", "splittable"]
    if task["splittable"]:
        required.append("min_block_minutes")
    if task["earliest_start_at"]:
        required.append("earliest_start_at")
    return {"id": task["id"], "source_id": None, "title": task["title"], "kind": "flexible", "status": "confirmed",
            "earliest_start_at": task["earliest_start_at"], "deadline_at": task["deadline_at"], "fixed_start_at": None,
            "fixed_end_at": None, "visit_windows": [], "remaining_minutes": estimate, "splittable": task["splittable"],
            "min_block_minutes": minimum, "must_do": True,
            "priority": DEFAULT_PRIORITY if task["priority"] is None else task["priority"],
            "location": None, "materials": [], "depends_on": [], "confirmed_fields": required, "evidence": [], "uncertainties": []}


def _plan(snapshot: dict, reference: datetime, free: list[Interval], tasks: list[dict]) -> dict:
    internal = {"schema_version": 2, "revision": snapshot["revision"], "timezone": "Asia/Shanghai",
                "reference_time": _iso(reference), "sources": [], "tasks": [_v2_task(task) for task in tasks],
                "availability": [{"start": _iso(start), "end": _iso(end)} for start, end in free], "availability_confirmed": True}
    return plan_snapshot(internal)


def _shortage(task: dict, reference: datetime, free: list[Interval], used: list[Interval]) -> int:
    release = max(reference, _instant(task["earliest_start_at"], "earliest")) if task["earliest_start_at"] else reference
    deadline = _instant(task["deadline_at"], "deadline")
    gaps = [(max(start, release), min(end, deadline)) for start, end in _subtract(free, used)]
    gaps = [_minutes(start, end) for start, end in gaps if start < end]
    available = sum(gaps) if task["splittable"] else max(gaps, default=0)
    return max(task["estimate_minutes"] - available, 0)


def calendar_snapshot(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) - {"snapshot", "range"} or "snapshot" not in payload:
        raise ValueError("请求体需要 snapshot，可选 range")
    snapshot = payload["snapshot"]
    time_issues: dict[str, str] = {}
    validate_snapshot_v3(snapshot, time_issues=time_issues)
    reference = _instant(snapshot["reference_time"], "reference_time")
    first, last = _range(payload, reference)
    view_start, view_end = _at(first, 0), _at(last + timedelta(days=1), 0)
    limit = reference + timedelta(days=MAX_HORIZON_DAYS)
    free_defined = snapshot["daily_window"] is not None
    undefined: list[dict] = []
    conflicts: list[dict] = []
    warnings: list[str] = []
    if not free_defined:
        undefined.append({"type": "free_time_undefined", "id": None, "title": None,
                          "reason": "还没有设置每天的作息范围，自由时间未定义，暂不生成工作时间。"})
    # 底色 is optional: without routines the week is still computed and conflicts among
    # 点墨, 约定 and 落笔 are still judged; definition.routines just reports it.

    # Classify tasks before expanding time so the planning horizon is known.
    states: dict[str, dict] = {}
    schedulable = []
    for task in snapshot["tasks"]:
        entry = {"id": task["id"], "state": None, "estimate_minutes": task["estimate_minutes"], "scheduled_minutes": 0, "assumptions": []}
        states[task["id"]] = entry
        deadline = _instant(task["deadline_at"], "deadline") if task["deadline_at"] and task["id"] not in time_issues else None
        if task["status"] != "active":
            entry["state"] = task["status"]
        elif task["id"] in time_issues:
            entry["state"] = "time_invalid"
            undefined.append({"type": "task_time_invalid", "id": task["id"], "title": task["title"],
                              "reason": time_issues[task["id"]]})
        elif deadline is None:
            entry["state"] = "deadline_undefined"
            undefined.append({"type": "deadline_undefined", "id": task["id"], "title": task["title"], "reason": "截止时间未定义，无法判断何时完成。"})
        elif deadline <= reference:
            entry["state"] = "overdue"
            conflicts.append({"type": "overdue", "layers": ["work"], "items": [{"layer": "work", "id": task["id"], "title": task["title"]}],
                              "start": None, "end": _iso(deadline), "minutes": None, "reason": "截止已过，事项仍未完成；请标记完成、取消或修改截止。"})
        elif task["estimate_minutes"] is None:
            entry["state"] = "estimate_undefined"
            undefined.append({"type": "estimate_undefined", "id": task["id"], "title": task["title"], "reason": "预计用时未定义，只显示截止，不划工作时间。"})
        elif deadline > limit:
            entry["state"] = "beyond_horizon"
            undefined.append({"type": "beyond_horizon", "id": task["id"], "title": task["title"], "reason": f"截止超出{MAX_HORIZON_DAYS}天排程范围，临近时再安排。"})
        elif not free_defined:
            entry["state"] = "free_time_undefined"
        else:
            schedulable.append(task)
    schedulable.sort(key=lambda task: (_instant(task["deadline_at"], "deadline"), -(task["priority"] or DEFAULT_PRIORITY), task["id"]))
    if len(schedulable) > MAX_PLANNED_TASKS:
        for task in schedulable[MAX_PLANNED_TASKS:]:
            states[task["id"]]["state"] = "not_attempted"
            undefined.append({"type": "task_limit", "id": task["id"], "title": task["title"], "reason": f"一次最多安排{MAX_PLANNED_TASKS}件工作，较晚截止的先不安排。"})
        schedulable = schedulable[:MAX_PLANNED_TASKS]
    horizon = max([reference, *(_instant(task["deadline_at"], "deadline") for task in schedulable)])

    span_start, span_end = min(view_start, reference), max(view_end, horizon)
    days = _days(span_start, span_end)
    routines = _routine_occurrences(snapshot["routines"], days)
    temporary = []
    markers = []
    for item in snapshot["events"]:
        if item["status"] != "active":
            continue
        if item["id"] in time_issues:
            undefined.append({"type": "event_time_invalid", "id": item["id"], "title": item["title"],
                              "reason": time_issues[item["id"]]})
        elif item["start_at"] and item["end_at"]:
            temporary.append({"id": item["id"], "title": item["title"], "start": _instant(item["start_at"], "start"),
                              "end": _instant(item["end_at"], "end"), "location": item["location"], "layer": item.get("kind", "temporary")})
        elif item["start_at"]:
            start = _instant(item["start_at"], "start")
            if view_start <= start < view_end:
                markers.append({"type": "event_start_only", "id": item["id"], "title": item["title"], "at": _iso(start), "state": "end_undefined"})
            undefined.append({"type": "event_end_undefined", "id": item["id"], "title": item["title"], "reason": "只有开始时间，结束未定义，暂不计入占用。"})
        else:
            undefined.append({"type": "event_time_pending", "id": item["id"], "title": item["title"], "reason": "时间待定，公布后补填开始与结束。"})

    busy = [(item["start"], item["end"]) for item in [*routines, *temporary]]
    free: list[Interval] = []
    window_only: list[Interval] = []
    if free_defined:
        for day in days:
            window = snapshot["daily_window"][day.isoweekday() - 1]
            if window:
                window_only.append((_at(day, _clock(window["start"])), _at(day, _clock(window["end"]))))
        added = [(_instant(item["start_at"], "start"), _instant(item["end_at"], "end")) for item in snapshot["free_adjustments"] if item["kind"] == "add"]
        removed = [(_instant(item["start_at"], "start"), _instant(item["end_at"], "end")) for item in snapshot["free_adjustments"] if item["kind"] == "remove"]
        free = _subtract(merge_intervals([*window_only, *added]), [*busy, *removed])

    # Work: add tasks in deadline order; keep each one only if the validated plan still holds.
    accepted: list[dict] = []
    plan: dict | None = None
    planning_free = [(max(start, reference), min(end, horizon)) for start, end in free if end > reference and start < horizon]
    if len(planning_free) > MAX_FREE_INTERVALS:
        cutoff = planning_free[MAX_FREE_INTERVALS - 1][1]
        planning_free = planning_free[:MAX_FREE_INTERVALS]
        warnings.append("自由时间段过于零碎，本次只在前200段内安排工作。")
    else:
        cutoff = horizon
    for task in schedulable:
        if _instant(task["deadline_at"], "deadline") > cutoff:
            states[task["id"]]["state"] = "not_attempted"
            undefined.append({"type": "task_limit", "id": task["id"], "title": task["title"], "reason": "自由时间段过于零碎，这件工作暂未安排。"})
            continue
        trial = _plan(snapshot, reference, planning_free, [*accepted, task])
        if trial["status"] == "feasible_found":
            accepted.append(task)
            plan = trial
            continue
        used = [(_instant(entry["start"], "start"), _instant(entry["end"], "end")) for entry in (plan or {"schedule": []})["schedule"]]
        missing = _shortage(task, reference, planning_free, used)
        states[task["id"]]["state"] = "short"
        reason = (f"截止前的自由时间还差约{missing}分钟。" if missing else
                  "截止前的空档总量够，但连续时长不够；可以允许拆开做、减小最小连续时长或增加自由时间。")
        conflicts.append({"type": "work_shortage", "layers": ["work", "routine", "temporary"],
                          "items": [{"layer": "work", "id": task["id"], "title": task["title"]}],
                          "start": None, "end": task["deadline_at"], "minutes": missing or None, "reason": reason})
    titles = {task["id"]: task for task in snapshot["tasks"]}
    work = []
    for entry in (plan or {"schedule": []})["schedule"]:
        start, end = _instant(entry["start"], "start"), _instant(entry["end"], "end")
        states[entry["task_id"]]["scheduled_minutes"] += _minutes(start, end)
        if start < view_end and end > view_start:
            task = titles[entry["task_id"]]
            work.append({"task_id": task["id"], "title": task["title"], "start": _iso(start), "end": _iso(end), "deadline_at": task["deadline_at"]})
    for task in accepted:
        states[task["id"]]["state"] = "scheduled"
        if task["priority"] is None:
            states[task["id"]]["assumptions"].append(f"优先级未填，按{DEFAULT_PRIORITY}计。")
        if task["splittable"] and task["min_block_minutes"] is None:
            states[task["id"]]["assumptions"].append(f"最小连续时长未填，按{min(DEFAULT_MIN_BLOCK, task['estimate_minutes'])}分钟计。")
    for task in snapshot["tasks"]:
        if task["status"] == "active" and task["deadline_at"] and task["id"] not in time_issues:
            at = _instant(task["deadline_at"], "deadline")
            if view_start <= at < view_end:
                markers.append({"type": "deadline", "id": task["id"], "title": task["title"], "at": _iso(at), "state": states[task["id"]]["state"]})

    def visible(items: list[dict]) -> list[dict]:
        return [item for item in items if item["start"] < view_end and item["end"] > view_start]

    def ref(layer: str, item: dict) -> dict:
        return {"layer": layer, "id": item["id"], "title": item["title"], "start": _iso(item["start"]), "end": _iso(item["end"])}

    # Routines repeat weekly, so their own overlaps are judged in the shown week. Each
    # temporary item is checked on its own days even outside the week, so an item
    # never looks conflict-free just because another week is on screen.
    event_days = sorted({day for item in temporary for day in _days(item["start"], item["end"] - timedelta(microseconds=1))})
    event_routines = _routine_occurrences(snapshot["routines"], event_days)
    order = ["routine", "work", "appointment", "temporary"]

    def add_conflict(kind, one, other, start, end, reason):
        items = [ref(one.get("layer", "routine"), one), ref(other.get("layer", "routine"), other)]
        layers = sorted({item["layer"] for item in items}, key=order.index)
        conflicts.append({"type": kind, "layers": layers, "items": items, "start": _iso(start), "end": _iso(end),
                          "minutes": _minutes(start, end), "reason": reason})

    for one, other, start, end in _overlaps(visible(routines), [], True):
        add_conflict("routine_overlap", one, other, start, end, "两个例行占用时间重叠，请核对课表或例会安排。")
    for one, other, start, end in _overlaps(temporary, event_routines, False):
        if one["layer"] == "appointment":
            add_conflict("appointment_routine_overlap", one, other, start, end, "约好的事与例行占用重叠，需要改约或请假。")
        else:
            add_conflict("temporary_routine_overlap", one, other, start, end, "临时占用与例行占用重叠，需要请假、调课或放弃其中之一。")
    for one, other, start, end in _overlaps(temporary, [], True):
        if "appointment" in {one["layer"], other["layer"]}:
            if one["layer"] != "appointment":
                one, other = other, one
            add_conflict("appointment_overlap", one, other, start, end, "约好的事与另一件有固定时间的事重叠，需要改约或二选一。")
        else:
            add_conflict("temporary_overlap", one, other, start, end, "两个临时占用时间重叠，只能参加其中之一。")

    used_work = [(_instant(entry["start"], "start"), _instant(entry["end"], "end")) for entry in (plan or {"schedule": []})["schedule"]]
    all_routines = _routine_occurrences(snapshot["routines"], _days(span_start, span_end + timedelta(days=8)))
    attach_strategies(conflicts, snapshot=snapshot, reference=reference, free=free, work=used_work,
                      busy=[*all_routines, *temporary])

    def output(items: list[dict]) -> list[dict]:
        return [{key: value for key, value in {**item, "start": _iso(item["start"]), "end": _iso(item["end"])}.items() if key != "layer"}
                for item in visible(items)]

    free_out = []
    for start, end in free:
        if start < view_end and end > view_start:
            inside_window = any(start < window_end and end > window_start for window_start, window_end in window_only)
            free_out.append({"start": _iso(start), "end": _iso(end), "origin": "daily_window" if inside_window else "added"})
    validation = plan["validation"] if plan else {"passed": None, "errors": [], "checked_constraints": []}
    return {
        "schema_version": 3, "revision": snapshot["revision"],
        "status": "conflicts" if conflicts else "incomplete" if undefined else "clear",
        "range": {"from": first.isoformat(), "to": last.isoformat()},
        "definition": {"free_time": "defined" if free_defined else "undefined",
                       "routines": "confirmed" if snapshot["routines_confirmed"] else "unconfirmed"},
        "layers": {"routine": output(routines), "free": free_out, "work": work, "appointment": output([item for item in temporary if item["layer"] == "appointment"]),
                   "temporary": output([item for item in temporary if item["layer"] == "temporary"])},
        "markers": sorted(markers, key=lambda item: (item["at"], item["id"])),
        "tasks": [states[task["id"]] for task in snapshot["tasks"]],
        "conflicts": conflicts, "undefined": undefined,
        "validation": {"passed": validation["passed"], "errors": validation["errors"]},
        "warnings": warnings, "limitations": LIMITATIONS,
    }
