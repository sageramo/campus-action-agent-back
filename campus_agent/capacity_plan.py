"""Deterministic multi-activity capacity试算 ("排不排得开") on top of snapshot v3.

Reuses `layers.calendar_snapshot` so remaining free slots match `/api/v3/calendar`
and frontend `agenda.js` / `calendar.js` to the minute. Never mutates or writes back
the snapshot; never calls an LLM.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from .layers import MAX_RANGE_DAYS, _at, _iso, _minutes, _subtract, calendar_snapshot, validate_snapshot_v3
from .snapshot import _instant
from .time_utils import get_zone

Interval = tuple[datetime, datetime]

VALID_SCOPES = {"today", "tomorrow", "weekend", "next_weekend", "weekday", "week", "next"}
VALID_CATEGORIES = {"study", "club", "leisure", "rest", "other"}
VALID_PREFERRED = {"weekend", "evening", "daytime", "morning", "afternoon", "any"}
WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
CATEGORY_TRIM_ORDER = {"leisure": 0, "other": 1, "club": 2, "rest": 3, "study": 4}
CATEGORY_DEFER_ORDER = {"club": 0, "leisure": 1, "other": 2, "study": 3, "rest": 4}
MAX_ACTIVITIES = 20
MAX_ACTIVITY_MINUTES = 7 * 24 * 60  # 10080 minutes (1 week)

ALLOWED_PLAN_KEYS = {
    "snapshot",
    "now",
    "scope",
    "weekday_index",
    "weekdayIndex",
    "weekday_next",
    "weekdayNext",
    "from",
    "to",
    "range",
    "activities",
    "reserve_rest_minutes",
}


def _ceil_minute(dt: datetime) -> datetime:
    local = dt.astimezone(get_zone())
    if local.second or local.microsecond:
        return (local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return local.replace(second=0, microsecond=0)


def _minutes_label(minutes: int) -> str:
    minutes = max(0, int(minutes))
    hours, rem = divmod(minutes, 60)
    if hours and rem:
        return f"{hours} 小时 {rem} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{rem} 分钟"


def _clock_end(start: datetime, end: datetime) -> str:
    s_loc = start.astimezone(get_zone())
    e_loc = end.astimezone(get_zone())
    if e_loc.date() > s_loc.date() and e_loc.hour == 0 and e_loc.minute == 0:
        return "24:00"
    return e_loc.strftime("%H:%M")


def _slot_label(start: datetime, end: datetime) -> str:
    s_loc = start.astimezone(get_zone())
    wd = WEEKDAY_LABELS[s_loc.isoweekday() - 1]
    return f"{s_loc.month}月{s_loc.day}日 {wd} {s_loc.strftime('%H:%M')}–{_clock_end(start, end)}"


def _slot_dict(start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "start": _iso(start.astimezone(get_zone())),
        "end": _iso(end.astimezone(get_zone())),
        "minutes": _minutes(start, end),
        "label": _slot_label(start, end),
    }


def _parse_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if not (minimum <= value <= maximum):
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.isoweekday() - 1)


def _parse_bound_endpoint(value: object, name: str, *, is_end: bool) -> tuple[datetime, bool]:
    """Parse either YYYY-MM-DD (returns date_only=True) or RFC3339 instant."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是 YYYY-MM-DD 日期或带时区的 ISO 时间")
    raw = value.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            d = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 日期无效，须为 YYYY-MM-DD") from exc
        if is_end:
            return _at(d + timedelta(days=1), 0), True
        return _at(d, 0), True
    return _instant(raw, name).astimezone(get_zone()), False


def resolve_scope(
    *,
    now_dt: datetime,
    scope: object = None,
    weekday_index: object = None,
    weekday_next: object = False,
    from_raw: object = None,
    to_raw: object = None,
    range_raw: object = None,
) -> dict[str, Any]:
    """Resolve scope/from/to into exact Beijing-time [from, to) matching agenda.js."""
    now_min = _ceil_minute(now_dt)
    today = now_min.date()
    today_start = _at(today, 0)
    monday = _week_monday(today)

    # Allow scope to be a structured object like {"type": "weekday", "weekday_index": 5} or {"from": ..., "to": ...}
    if isinstance(scope, dict):
        if "from" in scope or "to" in scope:
            from_raw = scope.get("from", from_raw)
            to_raw = scope.get("to", to_raw)
            scope = None
        else:
            if weekday_index is None:
                weekday_index = scope.get("weekday_index", scope.get("weekdayIndex"))
            if not weekday_next:
                weekday_next = scope.get("weekday_next", scope.get("weekdayNext", False))
            scope = scope.get("type", scope.get("scope"))

    if range_raw is not None:
        if not isinstance(range_raw, dict) or set(range_raw) != {"from", "to"}:
            raise ValueError("range 需要 from 与 to 两个字段")
        from_raw = range_raw["from"]
        to_raw = range_raw["to"]

    if from_raw is not None or to_raw is not None:
        if from_raw is None or to_raw is None:
            raise ValueError("自定义范围必须同时提供 from 与 to")
        start_dt, start_date_only = _parse_bound_endpoint(from_raw, "from", is_end=False)
        end_dt, end_date_only = _parse_bound_endpoint(to_raw, "to", is_end=True)
        if end_dt <= start_dt:
            raise ValueError("范围结束时间必须晚于开始时间")
        span_days = ((end_dt - timedelta(microseconds=1)).date() - start_dt.date()).days + 1
        if span_days > MAX_RANGE_DAYS:
            raise ValueError(f"试算范围最长不能超过 {MAX_RANGE_DAYS} 天")
        effective_from = max(start_dt, now_min) if start_dt < now_min < end_dt else start_dt
        if end_dt <= now_min:
            effective_from = end_dt
        s_day = start_dt.date()
        e_day = (end_dt - timedelta(microseconds=1)).date()
        if s_day == e_day:
            label = f"{s_day.month}月{s_day.day}日"
        else:
            label = f"{s_day.month}月{s_day.day}日–{e_day.month}月{e_day.day}日"
        return {
            "type": "custom",
            "label": label,
            "from_dt": effective_from,
            "to_dt": end_dt,
            "raw_from_dt": start_dt,
            "cal_week_monday": _week_monday(s_day),
            "cal_end_date": e_day,
            "date_only": start_date_only and end_date_only,
        }

    scope_str = "week" if scope is None else str(scope).strip().lower()
    if scope_str.startswith("weekday:") or scope_str.startswith("next_weekday:"):
        prefix, idx_str = scope_str.split(":", 1)
        try:
            weekday_index = int(idx_str)
        except ValueError as exc:
            raise ValueError("weekday 索引必须是 0–6（0=周一，6=周日）") from exc
        if prefix == "next_weekday":
            weekday_next = True
        scope_str = "weekday"

    if scope_str not in VALID_SCOPES:
        raise ValueError(f"不支持的 scope「{scope_str}」，可选：today / tomorrow / weekend / next_weekend / weekday / week / next")

    if scope_str == "today":
        from_dt, to_dt, label = now_min, today_start + timedelta(days=1), "今天"
        cal_monday, cal_end = monday, monday + timedelta(days=6)
    elif scope_str == "tomorrow":
        tom = today + timedelta(days=1)
        from_dt, to_dt, label = _at(tom, 0), _at(tom + timedelta(days=1), 0), "明天"
        tom_mon = _week_monday(tom)
        cal_monday, cal_end = tom_mon, tom_mon + timedelta(days=6)
    elif scope_str in {"weekend", "next_weekend"}:
        base_mon = monday + timedelta(days=7 if scope_str == "next_weekend" else 0)
        sat = _at(base_mon + timedelta(days=5), 0)
        sun_end = _at(base_mon + timedelta(days=7), 0)
        from_dt = max(sat, now_min) if now_min < sun_end else sun_end
        to_dt = sun_end
        label = "下周末" if scope_str == "next_weekend" else "本周末"
        cal_monday, cal_end = base_mon, base_mon + timedelta(days=6)
    elif scope_str == "weekday":
        if weekday_index is None:
            raise ValueError("scope=weekday 时必须提供 weekday_index（0=周一，6=周日）")
        wd_idx = _parse_int(weekday_index, "weekday_index", minimum=0, maximum=6)
        if not isinstance(weekday_next, bool):
            raise ValueError("weekday_next 必须是布尔值")
        today_idx = today.isoweekday() - 1
        offset_weeks = 1 if (weekday_next or wd_idx < today_idx) else 0
        base_mon = monday + timedelta(days=offset_weeks * 7)
        target_day = base_mon + timedelta(days=wd_idx)
        day_start = _at(target_day, 0)
        day_end = _at(target_day + timedelta(days=1), 0)
        from_dt = max(day_start, now_min) if now_min < day_end else day_end
        to_dt = day_end
        label = f"{'下' if offset_weeks else '本'}{WEEKDAY_LABELS[wd_idx]}"
        cal_monday, cal_end = base_mon, base_mon + timedelta(days=6)
    elif scope_str == "next":
        next_mon = monday + timedelta(days=7)
        from_dt, to_dt, label = _at(next_mon, 0), _at(next_mon + timedelta(days=7), 0), "下周"
        cal_monday, cal_end = next_mon, next_mon + timedelta(days=6)
    else:  # "week"
        week_start_dt = _at(monday, 0)
        week_end_dt = _at(monday + timedelta(days=7), 0)
        from_dt = max(week_start_dt, now_min) if now_min < week_end_dt else week_end_dt
        to_dt = week_end_dt
        label = "本周"
        cal_monday, cal_end = monday, monday + timedelta(days=6)

    return {
        "type": scope_str,
        "label": label,
        "from_dt": from_dt,
        "to_dt": to_dt,
        "raw_from_dt": from_dt,
        "cal_week_monday": cal_monday,
        "cal_end_date": cal_end,
        "date_only": False,
    }


def _split_midnight(intervals: list[Interval], bound_from: datetime, bound_to: datetime) -> list[Interval]:
    """Clip intervals to [bound_from, bound_to) and split at Beijing midnight so days never merge."""
    if bound_from >= bound_to:
        return []
    result: list[Interval] = []
    for start, end in sorted(intervals):
        s = max(start.astimezone(get_zone()), bound_from)
        e = min(end.astimezone(get_zone()), bound_to)
        if s >= e:
            continue
        cursor = s
        while cursor < e:
            next_midnight = _at(cursor.date() + timedelta(days=1), 0)
            seg_end = min(e, next_midnight)
            if _minutes(cursor, seg_end) > 0:
                result.append((cursor, seg_end))
            cursor = seg_end
    return result


def _compute_calendar_slots(
    snapshot: dict[str, Any],
    *,
    now_dt: datetime,
    bounds: dict[str, Any],
) -> dict[str, Any]:
    """Run calendar_snapshot and extract free-after-work slots matching agenda.js/calendar.js."""
    now_min = _ceil_minute(now_dt)
    saved_ref = _instant(snapshot["reference_time"], "reference_time").astimezone(get_zone())
    # Exact match with agenda.js line 122:
    # if user asks about a window before a future demo reference_time, calculate from real `now`.
    effective_ref = now_min if saved_ref >= bounds["to_dt"] else max(now_min, saved_ref)

    cal_from_date = bounds["cal_week_monday"]
    cal_to_date = bounds["cal_end_date"]
    if (cal_to_date - cal_from_date).days + 1 > MAX_RANGE_DAYS:
        cal_from_date = bounds["raw_from_dt"].date()
        cal_to_date = min(cal_to_date, cal_from_date + timedelta(days=MAX_RANGE_DAYS - 1))

    cal_snap = {**snapshot, "reference_time": _iso(effective_ref)}
    cal_result = calendar_snapshot(
        {
            "snapshot": cal_snap,
            "range": {"from": cal_from_date.isoformat(), "to": cal_to_date.isoformat()},
            "now": _iso(now_min),
        }
    )

    free_defined = cal_result.get("definition", {}).get("free_time") == "defined"
    if not free_defined:
        return {
            "free_time_defined": False,
            "slots": [],
            "surrounding_slots": [],
            "warnings": ["还没有设置每天的作息范围，暂时算不出可用时间。请先在铺纸设置作息。"],
            "cal_result": cal_result,
        }

    free_bands = [
        (_instant(item["start"], "free.start"), _instant(item["end"], "free.end"))
        for item in cal_result["layers"]["free"]
    ]
    work_blocks = [
        (_instant(item["start"], "work.start"), _instant(item["end"], "work.end"))
        for item in cal_result["layers"]["work"]
    ]
    remaining_all = _subtract(free_bands, work_blocks)
    slots = _split_midnight(remaining_all, bounds["from_dt"], bounds["to_dt"])

    # Also compute surrounding week free slots (outside [from_dt, to_dt) and >= now_min) for shift tradeoffs.
    week_start_dt = max(_at(cal_from_date, 0), now_min)
    week_end_dt = _at(cal_to_date + timedelta(days=1), 0)
    surrounding_raw = _subtract(remaining_all, [(bounds["from_dt"], bounds["to_dt"])]) if bounds["from_dt"] < bounds["to_dt"] else remaining_all
    surrounding_slots = _split_midnight(surrounding_raw, week_start_dt, week_end_dt)

    warnings: list[str] = []
    from_dt, to_dt = bounds["from_dt"], bounds["to_dt"]
    by_id = {item["id"]: item for item in [*snapshot["tasks"], *snapshot["events"]]}

    def _affects(issue: dict[str, Any]) -> bool:
        item = by_id.get(issue.get("id"))
        if not item:
            return False
        issue_type = str(issue.get("type") or "")
        if issue_type.endswith("_time_invalid") or issue_type == "event_time_pending":
            return True
        if item.get("start_at"):
            try:
                return _instant(item["start_at"], "start_at") < to_dt
            except ValueError:
                return True
        earliest = None
        if item.get("earliest_start_at"):
            try:
                earliest = _instant(item["earliest_start_at"], "earliest_start_at")
            except ValueError:
                earliest = None
        deadline = None
        if item.get("deadline_at"):
            try:
                deadline = _instant(item["deadline_at"], "deadline_at")
            except ValueError:
                deadline = None
        return (earliest is None or earliest < to_dt) and (deadline is None or deadline >= from_dt)

    pending = sum(1 for issue in cal_result.get("undefined", []) if _affects(issue))
    shortages = sum(
        1
        for issue in cal_result.get("conflicts", [])
        if issue.get("type") == "work_shortage" and any(_affects(it) for it in issue.get("items", []))
    )
    undefined_ids = {issue.get("id") for issue in cal_result.get("undefined", [])}
    unestimated = sum(
        1
        for task in snapshot["tasks"]
        if task.get("status") == "active"
        and task.get("estimate_minutes") is None
        and task.get("deadline_at") is not None
        and task.get("id") not in undefined_ids
        and _affects({"id": task["id"], "type": "estimate_undefined"})
    )
    if pending or shortages:
        warnings.append(f"还有 {pending + shortages} 件待补时间或尚未排下的事，补齐后空档可能减少。")
    if unestimated:
        warnings.append(f"另有 {unestimated} 件未估时的截止事项未占空档，若耗时较多需自行预留。")
    if not snapshot["routines"]:
        warnings.append("当前清单还没有课表或固定例会。")
    elif not snapshot["routines_confirmed"]:
        warnings.append("请确认课表和固定例会已经补齐。")

    return {
        "free_time_defined": True,
        "slots": slots,
        "surrounding_slots": surrounding_slots,
        "warnings": warnings,
        "cal_result": cal_result,
    }


def check_scope_capacity(payload: object) -> dict[str, Any]:
    """Deterministic bare capacity check for a scope (used by chat tool `check_capacity`)."""
    if not isinstance(payload, dict) or "snapshot" not in payload:
        raise ValueError("需要提供 snapshot")
    snapshot = payload["snapshot"]
    validate_snapshot_v3(snapshot)
    if "now" in payload and payload["now"] is None:
        raise ValueError("now 不能显式传 null；请提供当前时刻或省略")
    now_dt = _instant(payload["now"], "now") if payload.get("now") is not None else _instant(snapshot["reference_time"], "reference_time")
    bounds = resolve_scope(
        now_dt=now_dt,
        scope=payload.get("scope"),
        weekday_index=payload.get("weekday_index", payload.get("weekdayIndex")),
        weekday_next=payload.get("weekday_next", payload.get("weekdayNext", False)),
        from_raw=payload.get("from"),
        to_raw=payload.get("to"),
        range_raw=payload.get("range"),
    )
    computed = _compute_calendar_slots(snapshot, now_dt=now_dt, bounds=bounds)
    slots: list[Interval] = computed["slots"]
    total = sum(_minutes(s, e) for s, e in slots)
    longest = max((_minutes(s, e) for s, e in slots), default=0)
    scope_meta = {
        "type": bounds["type"],
        "label": bounds["label"],
        "from": _iso(bounds["from_dt"]),
        "to": _iso(bounds["to_dt"]),
    }
    if not computed["free_time_defined"]:
        summary = "还没有设置每天的作息范围，暂时算不出可用时间。请先在铺纸设置作息。"
    elif total == 0:
        summary = f"{bounds['label']}已无剩余留白（0 分钟），已扣除已录入的固定占用和已排任务。"
    else:
        summary = f"{bounds['label']}还剩 {_minutes_label(total)}留白，最长连续 {_minutes_label(longest)}（已扣除已录入的固定占用和已排任务）。"
    return {
        "tool": "check_capacity",
        "schema_version": 1,
        "now": _iso(_ceil_minute(now_dt)),
        "scope": scope_meta,
        "free_time_defined": computed["free_time_defined"],
        "available_minutes": total,
        "leftover_minutes": total,
        "longest_free_minutes": longest,
        "free_slots": [_slot_dict(s, e) for s, e in slots],
        "warnings": computed["warnings"],
        "summary": summary,
    }


def _normalize_activities(raw_activities: object) -> list[dict[str, Any]]:
    if not isinstance(raw_activities, list) or not raw_activities:
        raise ValueError("activities 必须是包含 1 至 20 项活动的非空数组")
    if len(raw_activities) > MAX_ACTIVITIES:
        raise ValueError(f"单次最多试算 {MAX_ACTIVITIES} 项活动")
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_activities):
        if not isinstance(item, dict):
            raise ValueError(f"activities[{idx}] 必须是对象")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise ValueError(f"activities[{idx}].title 必须为 1–120 字的非空字符串")
        title = title.strip()
        if "minutes" not in item or item["minutes"] is None:
            raise ValueError(f"活动「{title}」缺少时长（minutes）")
        minutes = _parse_int(item["minutes"], f"活动「{title}」的 minutes", minimum=1, maximum=MAX_ACTIVITY_MINUTES)
        category = str(item.get("category") or "other").strip().lower()
        if category not in VALID_CATEGORIES:
            raise ValueError(f"活动「{title}」的 category 无效，可选：study / club / leisure / rest / other")
        splittable = item.get("splittable", False)
        if not isinstance(splittable, bool):
            raise ValueError(f"活动「{title}」的 splittable 必须是布尔值")
        min_block_raw = item.get("min_block_minutes")
        if min_block_raw is None:
            min_block = min(30, minutes) if splittable else minutes
        else:
            min_block = _parse_int(min_block_raw, f"活动「{title}」的 min_block_minutes", minimum=1, maximum=MAX_ACTIVITY_MINUTES)
            if splittable and min_block > minutes:
                raise ValueError(f"活动「{title}」的 min_block_minutes 不能超过 minutes")
            if not splittable:
                min_block = minutes
        preferred = str(item.get("preferred") or "any").strip().lower()
        if preferred not in VALID_PREFERRED:
            raise ValueError(f"活动「{title}」的 preferred 无效，可选：weekend / evening / daytime / any")
        normalized.append(
            {
                "index": idx,
                "title": title,
                "minutes": minutes,
                "category": category,
                "splittable": splittable,
                "min_block_minutes": min_block,
                "preferred": preferred,
            }
        )
    return normalized


def _preferred_subinterval(slot: Interval, preferred: str) -> Interval | None:
    s, e = slot
    if preferred == "any":
        return slot
    if preferred == "weekend":
        return slot if s.isoweekday() in (6, 7) else None
    day = s.date()
    if preferred == "daytime":
        lo, hi = _at(day, 8 * 60), _at(day, 18 * 60)
    elif preferred == "evening":
        lo, hi = _at(day, 18 * 60), _at(day + timedelta(days=1), 0)
    elif preferred == "morning":
        lo, hi = _at(day, 6 * 60), _at(day, 12 * 60)
    elif preferred == "afternoon":
        lo, hi = _at(day, 12 * 60), _at(day, 18 * 60)
    else:
        return slot
    ns, ne = max(s, lo), min(e, hi)
    return (ns, ne) if ns < ne else None


def _carve_interval(free_slots: list[Interval], start: datetime, end: datetime) -> list[Interval]:
    updated: list[Interval] = []
    for s, e in free_slots:
        if e <= start or s >= end:
            updated.append((s, e))
            continue
        if s < start:
            updated.append((s, start))
        if end < e:
            updated.append((end, e))
    return updated


def _place_once(
    free_slots: list[Interval],
    activities: list[dict[str, Any]],
    *,
    tightest_fit: bool = False,
) -> tuple[list[dict[str, Any]], list[Interval]]:
    """Deterministic greedy placement; returns placements (in original index order) and remaining slots."""
    remaining = list(free_slots)
    ordered = sorted(
        activities,
        key=lambda a: (
            a["splittable"],          # non-splittable first
            -a["minutes"],            # largest continuous blocks first
            a["preferred"] == "any",  # preferred constraints before unconstrained
            a["index"],               # stable deterministic tie-break
        ),
    )
    placed_by_idx: dict[int, dict[str, Any]] = {}

    for act in ordered:
        need = act["minutes"]
        pref = act["preferred"]
        blocks: list[Interval] = []
        matched_pref = True

        if not act["splittable"]:
            # Pass 1: try preferred sub-intervals
            candidates: list[tuple[int, datetime, datetime]] = []
            for s, e in remaining:
                sub = _preferred_subinterval((s, e), pref)
                if sub is not None and _minutes(sub[0], sub[1]) >= need:
                    candidates.append((_minutes(sub[0], sub[1]), sub[0], sub[0] + timedelta(minutes=need)))
            chosen: Interval | None = None
            if candidates:
                if tightest_fit:
                    candidates.sort(key=lambda item: (item[0], item[1]))
                else:
                    candidates.sort(key=lambda item: item[1])
                chosen = (candidates[0][1], candidates[0][2])
            else:
                # Pass 2: fallback to any remaining free slot
                fallback: list[tuple[int, datetime, datetime]] = []
                for s, e in remaining:
                    if _minutes(s, e) >= need:
                        fallback.append((_minutes(s, e), s, s + timedelta(minutes=need)))
                if fallback:
                    if tightest_fit:
                        fallback.sort(key=lambda item: (item[0], item[1]))
                    else:
                        fallback.sort(key=lambda item: item[1])
                    chosen = (fallback[0][1], fallback[0][2])
                    if pref != "any":
                        matched_pref = False
            if chosen is not None:
                blocks.append(chosen)
                remaining = _carve_interval(remaining, chosen[0], chosen[1])
        else:
            min_block = act["min_block_minutes"]
            rem_need = need

            def _allocate_from(candidates_list: list[Interval], *, is_preferred_pass: bool) -> None:
                nonlocal rem_need, remaining, matched_pref
                for cand_s, cand_e in candidates_list:
                    if rem_need <= 0:
                        break
                    avail = _minutes(cand_s, cand_e)
                    if avail < min_block:
                        continue
                    if avail >= rem_need:
                        take = rem_need
                    else:
                        # Avoid leaving a tiny unusable tail (< min_block) if a slightly smaller chunk prevents it
                        leftover_need = rem_need - avail
                        if 0 < leftover_need < min_block and (rem_need - min_block) >= min_block:
                            take = rem_need - min_block
                        else:
                            take = avail
                    if take < min_block:
                        continue
                    seg_end = cand_s + timedelta(minutes=take)
                    blocks.append((cand_s, seg_end))
                    remaining = _carve_interval(remaining, cand_s, seg_end)
                    rem_need -= take
                    if not is_preferred_pass and pref != "any":
                        matched_pref = False

            if pref != "any":
                pref_cands = [
                    sub
                    for slot in list(remaining)
                    if (sub := _preferred_subinterval(slot, pref)) is not None and _minutes(sub[0], sub[1]) >= min_block
                ]
                _allocate_from(pref_cands, is_preferred_pass=True)
            if rem_need > 0:
                other_cands = [slot for slot in list(remaining) if _minutes(slot[0], slot[1]) >= min_block]
                _allocate_from(other_cands, is_preferred_pass=(pref == "any"))

            # If we couldn't place the whole splittable activity, check whether partial placement left
            # an unplaceable remainder; partial blocks are still reported transparently as "partial".

        blocks.sort(key=lambda item: item[0])
        placed_mins = sum(_minutes(s, e) for s, e in blocks)
        shortfall_mins = max(0, need - placed_mins)
        status = "placed" if shortfall_mins == 0 else ("partial" if placed_mins > 0 else "unplaced")
        note = None
        if status == "unplaced":
            longest_rem = max((_minutes(s, e) for s, e in remaining), default=0)
            if not act["splittable"] and sum(_minutes(s, e) for s, e in remaining) >= need and longest_rem < need:
                note = f"剩余总量够，但最长连续空档仅 {_minutes_label(longest_rem)}，放不下连续 {_minutes_label(need)}"
            else:
                note = f"当前范围内剩余空档不足，还差 {_minutes_label(shortfall_mins)}"
        elif status == "partial":
            note = f"仅放入 {_minutes_label(placed_mins)}，还差 {_minutes_label(shortfall_mins)}"
        elif not matched_pref and pref != "any":
            note = "首选时段空档不足，已暂放到其他可用时段"

        placed_by_idx[act["index"]] = {
            "index": act["index"],
            "title": act["title"],
            "category": act["category"],
            "requested_minutes": need,
            "placed_minutes": placed_mins,
            "shortfall_minutes": shortfall_mins,
            "status": status,
            "splittable": act["splittable"],
            "preferred": pref,
            "matched_preferred": matched_pref if placed_mins > 0 else False,
            "blocks": [_slot_dict(s, e) for s, e in blocks],
            **({"note": note} if note else {}),
        }

    ordered_placements = [placed_by_idx[i] for i in range(len(activities))]
    return ordered_placements, remaining


def _simulate_plan(
    free_slots: list[Interval],
    activities: list[dict[str, Any]],
    reserve_rest_minutes: int,
) -> dict[str, Any]:
    """Run earliest-first placement and try tightest-fit fallback if needed."""
    placements, remaining = _place_once(free_slots, activities, tightest_fit=False)
    shortfall_total = sum(p["shortfall_minutes"] for p in placements)
    if shortfall_total > 0:
        alt_placements, alt_remaining = _place_once(free_slots, activities, tightest_fit=True)
        alt_shortfall = sum(p["shortfall_minutes"] for p in alt_placements)
        if alt_shortfall < shortfall_total:
            placements, remaining = alt_placements, alt_remaining
            shortfall_total = alt_shortfall

    leftover = sum(_minutes(s, e) for s, e in remaining)
    longest = max((_minutes(s, e) for s, e in remaining), default=0)
    rest_shortfall = max(0, reserve_rest_minutes - leftover)
    feasible = (shortfall_total == 0) and (rest_shortfall == 0)
    shortfall = [
        {
            "index": p["index"],
            "title": p["title"],
            "requested_minutes": p["requested_minutes"],
            "placed_minutes": p["placed_minutes"],
            "missing_minutes": p["shortfall_minutes"],
        }
        for p in placements
        if p["shortfall_minutes"] > 0
    ]
    return {
        "feasible": feasible,
        "placements": placements,
        "shortfall": shortfall,
        "total_shortfall_minutes": shortfall_total,
        "rest_satisfied": rest_shortfall == 0,
        "rest_shortfall_minutes": rest_shortfall,
        "leftover_minutes": leftover,
        "longest_free_minutes": longest,
        "remaining_slots": remaining,
    }


def _generate_tradeoffs(
    *,
    free_slots: list[Interval],
    surrounding_slots: list[Interval],
    activities: list[dict[str, Any]],
    reserve_rest_minutes: int,
    base_sim: dict[str, Any],
    scope_label: str,
) -> list[dict[str, Any]]:
    """Generate 2–3 deterministic, simulation-verified tradeoffs when base plan is infeasible."""
    if base_sim["feasible"]:
        return []
    available = sum(_minutes(s, e) for s, e in free_slots)
    requested = sum(a["minutes"] for a in activities)
    base_missing = base_sim["total_shortfall_minutes"] + base_sim["rest_shortfall_minutes"]
    candidates: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    seen_titles: set[str] = set()

    def _add_candidate(
        *,
        kind: str,
        title: str,
        consequence: str,
        sim: dict[str, Any],
        changes: list[dict[str, Any]],
        cut_minutes: int,
        priority_rank: int,
    ) -> None:
        if title in seen_titles:
            return
        seen_titles.add(title)
        score = (
            0 if sim["feasible"] else 1,
            sim["total_shortfall_minutes"] + sim["rest_shortfall_minutes"],
            cut_minutes * 10 + priority_rank,
        )
        candidates.append(
            (
                score,
                {
                    "kind": kind,
                    "title": title,
                    "consequence": consequence,
                    "feasible": sim["feasible"],
                    "leftover_minutes": sim["leftover_minutes"],
                    "longest_free_minutes": sim["longest_free_minutes"],
                    "changes": changes,
                    "placements": sim["placements"],
                },
            )
        )

    # 1. Split a non-splittable activity (especially study/other/club, or any unplaced non-splittable item)
    if available > 0:
        non_split = [a for a in activities if not a["splittable"] and a["minutes"] >= 60]
        # Prefer splitting unplaced ones first, then study/other/club/leisure
        unplaced_indices = {p["index"] for p in base_sim["placements"] if p["shortfall_minutes"] > 0}
        non_split.sort(key=lambda a: (0 if a["index"] in unplaced_indices else 1, 0 if a["category"] == "study" else 1, -a["minutes"], a["index"]))
        for act in non_split:
            min_blk = min(60, max(30, act["minutes"] // 2))
            mutated = [
                {**a, "splittable": True, "min_block_minutes": min_blk} if a["index"] == act["index"] else a
                for a in activities
            ]
            sim = _simulate_plan(free_slots, mutated, reserve_rest_minutes)
            if sim["feasible"] or (sim["total_shortfall_minutes"] + sim["rest_shortfall_minutes"] < base_missing):
                placed_entry = sim["placements"][act["index"]]
                seg_count = max(2, len(placed_entry["blocks"]))
                if sim["feasible"]:
                    consequence = (
                        f"全部排得开，排完后还剩 {_minutes_label(sim['leftover_minutes'])}留白"
                        f"（最长连续 {_minutes_label(sim['longest_free_minutes'])}）；「{act['title']}」不再连续完成，改为分 {seg_count} 段做。"
                    )
                else:
                    rem_miss = sim["total_shortfall_minutes"] + sim["rest_shortfall_minutes"]
                    consequence = (
                        f"「{act['title']}」改为分 {seg_count} 段后，缺口从 {_minutes_label(base_missing)}降到 {_minutes_label(rem_miss)}，"
                        f"排完已放入部分后剩 {_minutes_label(sim['leftover_minutes'])}留白。"
                    )
                _add_candidate(
                    kind="split_activity",
                    title=f"把「{act['title']}」拆成 {seg_count} 段来做",
                    consequence=consequence,
                    sim=sim,
                    changes=[{"activity": act["title"], "field": "splittable", "before": False, "after": True}],
                    cut_minutes=0,
                    priority_rank=1,
                )
                break

    # 2. Trim duration of a flexible activity (leisure -> other -> club -> rest -> study)
    if available > 0:
        total_deficit = max(0, requested + reserve_rest_minutes - available)
        trim_order = sorted(activities, key=lambda a: (CATEGORY_TRIM_ORDER.get(a["category"], 5), -a["minutes"], a["index"]))
        feasible_trims = 0
        for act in trim_order:
            if feasible_trims >= 2:
                break
            old_m = act["minutes"]
            if old_m <= 30:
                continue
            # Determine target trimmed minutes: cover total_deficit or fit largest slot if continuity issue
            placed_info = base_sim["placements"][act["index"]]
            max_slot = max((_minutes(s, e) for s, e in free_slots), default=0)
            needed_cut = total_deficit
            if needed_cut == 0 and placed_info["shortfall_minutes"] > 0 and not act["splittable"]:
                needed_cut = max(30, old_m - max_slot)
            if needed_cut <= 0:
                needed_cut = max(30, base_missing)
            # Round cut up to 30-min granularity when old_m >= 90 and doing so leaves >= 30 min
            new_m = old_m - needed_cut
            if new_m >= 30 and needed_cut % 30 != 0 and (old_m - ((needed_cut // 30 + 1) * 30)) >= 30:
                new_m = old_m - ((needed_cut // 30 + 1) * 30)
            if new_m < 30:
                # Try halving (rounded to 30m) as partial relief
                new_m = max(30, (old_m // 2 // 30) * 30 or 30)
            if new_m >= old_m:
                continue
            mutated = [
                {**a, "minutes": new_m, "min_block_minutes": min(a["min_block_minutes"], new_m)} if a["index"] == act["index"] else a
                for a in activities
            ]
            sim = _simulate_plan(free_slots, mutated, reserve_rest_minutes)
            if sim["feasible"] or (sim["total_shortfall_minutes"] + sim["rest_shortfall_minutes"] < base_missing):
                cut = old_m - new_m
                if sim["feasible"]:
                    consequence = (
                        f"「{act['title']}」缩短 {_minutes_label(cut)}，其余活动全部排得开，"
                        f"排完后还剩 {_minutes_label(sim['leftover_minutes'])}留白（最长连续 {_minutes_label(sim['longest_free_minutes'])}）。"
                    )
                    feasible_trims += 1
                else:
                    rem_miss = sim["total_shortfall_minutes"] + sim["rest_shortfall_minutes"]
                    consequence = (
                        f"「{act['title']}」缩短 {_minutes_label(cut)}后，总缺口从 {_minutes_label(base_missing)}减至 {_minutes_label(rem_miss)}，"
                        f"剩余留白 {_minutes_label(sim['leftover_minutes'])}。"
                    )
                _add_candidate(
                    kind="trim_activity",
                    title=f"把「{act['title']}」从 {_minutes_label(old_m)}缩到 {_minutes_label(new_m)}",
                    consequence=consequence,
                    sim=sim,
                    changes=[{"activity": act["title"], "field": "minutes", "before": old_m, "after": new_m}],
                    cut_minutes=cut,
                    priority_rank=2 + feasible_trims,
                )

    # 3. Shift/defer one activity out of the overloaded scope (preferably to an actual free slot in the same week!)
    if len(activities) >= 1:
        unplaced_indices = {p["index"] for p in base_sim["placements"] if p["shortfall_minutes"] > 0}
        defer_order = sorted(
            activities,
            key=lambda a: (
                0 if a["index"] in unplaced_indices else 1,
                CATEGORY_DEFER_ORDER.get(a["category"], 5),
                a["index"],
            ),
        )
        for act in defer_order[:2]:
            # Check if there is a concrete free slot in surrounding_slots (same week outside this scope)
            alt_slot: Interval | None = None
            for s, e in surrounding_slots:
                if _minutes(s, e) >= act["minutes"]:
                    alt_slot = (s, s + timedelta(minutes=act["minutes"]))
                    break
            remaining_acts = [a for a in activities if a["index"] != act["index"]]
            if remaining_acts:
                reindexed = [{**a, "index": i} for i, a in enumerate(remaining_acts)]
                sim = _simulate_plan(free_slots, reindexed, reserve_rest_minutes)
            else:
                sim = {
                    "feasible": (available >= reserve_rest_minutes) if alt_slot is not None else False,
                    "placements": [],
                    "shortfall": [],
                    "total_shortfall_minutes": 0,
                    "rest_satisfied": available >= reserve_rest_minutes,
                    "rest_shortfall_minutes": max(0, reserve_rest_minutes - available),
                    "leftover_minutes": available,
                    "longest_free_minutes": max((_minutes(s, e) for s, e in free_slots), default=0),
                    "remaining_slots": free_slots,
                }
            if alt_slot is not None:
                alt_label = _slot_label(alt_slot[0], alt_slot[1])
                title = f"把「{act['title']}」改到 {alt_label}"
                if remaining_acts and sim["feasible"]:
                    consequence = (
                        f"「{act['title']}」挪到周内空档 {alt_label}，{scope_label}内其余 {len(remaining_acts)} 项全部排得开，"
                        f"{scope_label}还剩 {_minutes_label(sim['leftover_minutes'])}留白。"
                    )
                elif not remaining_acts:
                    consequence = f"{scope_label}空档不足，可改到本周另一空档 {alt_label} 完成「{act['title']}」。"
                else:
                    rem_miss = sim["total_shortfall_minutes"] + sim["rest_shortfall_minutes"]
                    consequence = (
                        f"「{act['title']}」改到 {alt_label} 后，{scope_label}缺口降到 {_minutes_label(rem_miss)}，"
                        f"剩余留白 {_minutes_label(sim['leftover_minutes'])}。"
                    )
                _add_candidate(
                    kind="shift_to_other_slot",
                    title=title,
                    consequence=consequence,
                    sim=sim,
                    changes=[{"activity": act["title"], "field": "slot", "before": scope_label, "after": alt_label}],
                    cut_minutes=0,
                    priority_rank=3,
                )
                break
            elif remaining_acts and (sim["feasible"] or sim["total_shortfall_minutes"] + sim["rest_shortfall_minutes"] < base_missing):
                title = f"把「{act['title']}」先移出{scope_label}（改到下周或另择时间）"
                if sim["feasible"]:
                    consequence = (
                        f"少安排「{act['title']}」（{_minutes_label(act['minutes'])}），"
                        f"其余 {len(remaining_acts)} 项全部排得开，排完后还剩 {_minutes_label(sim['leftover_minutes'])}留白。"
                    )
                else:
                    rem_miss = sim["total_shortfall_minutes"] + sim["rest_shortfall_minutes"]
                    consequence = (
                        f"移出「{act['title']}」后，缺口从 {_minutes_label(base_missing)}降到 {_minutes_label(rem_miss)}，"
                        f"剩余留白 {_minutes_label(sim['leftover_minutes'])}。"
                    )
                _add_candidate(
                    kind="defer_activity",
                    title=title,
                    consequence=consequence,
                    sim=sim,
                    changes=[{"activity": act["title"], "field": "deferred", "before": False, "after": True}],
                    cut_minutes=act["minutes"],
                    priority_rank=5,
                )
                break

    # 4. Reduce reserved rest if rest_shortfall_minutes > 0 and activities themselves fit
    if reserve_rest_minutes > 0 and base_sim["rest_shortfall_minutes"] > 0 and base_sim["total_shortfall_minutes"] == 0:
        new_rest = base_sim["leftover_minutes"]
        sim = _simulate_plan(free_slots, activities, new_rest)
        cut_r = reserve_rest_minutes - new_rest
        _add_candidate(
            kind="reduce_rest",
            title=f"把预留休息从 {_minutes_label(reserve_rest_minutes)}调到 {_minutes_label(new_rest)}",
            consequence=(
                f"预留休息减少 {_minutes_label(cut_r)}，全部 {len(activities)} 项活动均可排入，"
                f"排完后仍保留 {_minutes_label(sim['leftover_minutes'])}留白。"
            ),
            sim=sim,
            changes=[{"activity": "预留休息", "field": "reserve_rest_minutes", "before": reserve_rest_minutes, "after": new_rest}],
            cut_minutes=cut_r,
            priority_rank=4,
        )

    # Sort best tradeoffs first and select up to 3, preferring distinct kinds when possible
    candidates.sort(key=lambda item: item[0])
    chosen: list[dict[str, Any]] = []
    used_kinds: set[str] = set()
    for _, item in candidates:
        if len(chosen) >= 3:
            break
        if item["kind"] not in used_kinds:
            chosen.append(item)
            used_kinds.add(item["kind"])
    for _, item in candidates:
        if len(chosen) >= 3:
            break
        if item not in chosen:
            chosen.append(item)
    return [{"id": f"tradeoff-{i + 1}", **item} for i, item in enumerate(chosen)]


def plan_capacity(payload: object) -> dict[str, Any]:
    """POST /api/v3/capacity/plan: deterministic multi-activity capacity试算 + tradeoffs."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    unknown = set(payload) - ALLOWED_PLAN_KEYS
    if unknown:
        raise ValueError(f"包含不支持的字段：{', '.join(sorted(unknown))}")
    if "snapshot" not in payload:
        raise ValueError("缺少必填字段 snapshot")
    snapshot = payload["snapshot"]
    validate_snapshot_v3(snapshot)

    if "now" in payload and payload["now"] is None:
        raise ValueError("now 不能显式传 null；请提供当前时刻或省略")
    now_dt = _instant(payload["now"], "now") if payload.get("now") is not None else _instant(snapshot["reference_time"], "reference_time")

    bounds = resolve_scope(
        now_dt=now_dt,
        scope=payload.get("scope"),
        weekday_index=payload.get("weekday_index", payload.get("weekdayIndex")),
        weekday_next=payload.get("weekday_next", payload.get("weekdayNext", False)),
        from_raw=payload.get("from"),
        to_raw=payload.get("to"),
        range_raw=payload.get("range"),
    )
    activities = _normalize_activities(payload.get("activities"))
    reserve_rest_minutes = _parse_int(
        payload.get("reserve_rest_minutes", 0),
        "reserve_rest_minutes",
        minimum=0,
        maximum=MAX_ACTIVITY_MINUTES,
    )

    computed = _compute_calendar_slots(snapshot, now_dt=now_dt, bounds=bounds)
    scope_meta = {
        "type": bounds["type"],
        "label": bounds["label"],
        "from": _iso(bounds["from_dt"]),
        "to": _iso(bounds["to_dt"]),
    }
    requested_total = sum(a["minutes"] for a in activities)

    if not computed["free_time_defined"]:
        placements = [
            {
                "index": a["index"],
                "title": a["title"],
                "category": a["category"],
                "requested_minutes": a["minutes"],
                "placed_minutes": 0,
                "shortfall_minutes": a["minutes"],
                "status": "unplaced",
                "splittable": a["splittable"],
                "preferred": a["preferred"],
                "matched_preferred": False,
                "blocks": [],
                "note": "还没有设置作息范围，无法计算可用时段",
            }
            for a in activities
        ]
        shortfall = [
            {
                "index": a["index"],
                "title": a["title"],
                "requested_minutes": a["minutes"],
                "placed_minutes": 0,
                "missing_minutes": a["minutes"],
            }
            for a in activities
        ]
        return {
            "tool": "plan_activities",
            "schema_version": 1,
            "now": _iso(_ceil_minute(now_dt)),
            "scope": scope_meta,
            "free_time_defined": False,
            "available_minutes": 0,
            "initial_longest_free_minutes": 0,
            "reserve_rest_minutes": reserve_rest_minutes,
            "requested_minutes": requested_total,
            "placed_minutes": 0,
            "feasible": False,
            "rest_satisfied": reserve_rest_minutes == 0,
            "rest_shortfall_minutes": reserve_rest_minutes,
            "total_shortfall_minutes": requested_total,
            "placements": placements,
            "shortfall": shortfall,
            "leftover_minutes": 0,
            "longest_free_minutes": 0,
            "free_slots": [],
            "tradeoffs": [],
            "warnings": computed["warnings"],
            "summary": "还没有设置每天的作息范围，暂时算不出排不排得开；请先在铺纸里设置作息。",
        }

    free_slots: list[Interval] = computed["slots"]
    surrounding_slots: list[Interval] = computed["surrounding_slots"]
    available_minutes = sum(_minutes(s, e) for s, e in free_slots)
    initial_longest = max((_minutes(s, e) for s, e in free_slots), default=0)

    sim = _simulate_plan(free_slots, activities, reserve_rest_minutes)
    tradeoffs = _generate_tradeoffs(
        free_slots=free_slots,
        surrounding_slots=surrounding_slots,
        activities=activities,
        reserve_rest_minutes=reserve_rest_minutes,
        base_sim=sim,
        scope_label=bounds["label"],
    )

    placed_total = sum(p["placed_minutes"] for p in sim["placements"])
    label = bounds["label"]
    count = len(activities)
    rest_note = f"（含预留休息 {_minutes_label(reserve_rest_minutes)}）" if reserve_rest_minutes else ""

    if sim["feasible"]:
        summary = (
            f"{label}排得开：{count} 项活动共需 {_minutes_label(requested_total)}{rest_note}，"
            f"全部可放入，排完后还剩 {_minutes_label(sim['leftover_minutes'])}留白"
            f"（最长连续 {_minutes_label(sim['longest_free_minutes'])}）。"
        )
    elif available_minutes == 0:
        summary = (
            f"{label}排不开：范围内已无剩余留白（0 分钟），"
            f"{count} 项活动共需 {_minutes_label(requested_total)}均无法放入。"
        )
    elif sim["total_shortfall_minutes"] == 0 and sim["rest_shortfall_minutes"] > 0:
        summary = (
            f"{label}排不开：{count} 项活动（共 {_minutes_label(requested_total)}）放得下，"
            f"但排完后只剩 {_minutes_label(sim['leftover_minutes'])}留白，离预留休息 {_minutes_label(reserve_rest_minutes)}还差 {_minutes_label(sim['rest_shortfall_minutes'])}。"
        )
    else:
        unfit_names = "、".join(f"「{s['title']}」（差 {_minutes_label(s['missing_minutes'])}）" for s in sim["shortfall"][:3])
        summary = (
            f"{label}排不开：当前剩余留白 {_minutes_label(available_minutes)}（最长连续 {_minutes_label(initial_longest)}），"
            f"{unfit_names}放不下；已按规则给出 {len(tradeoffs)} 个取舍方案。"
            if tradeoffs
            else f"{label}排不开：当前剩余留白 {_minutes_label(available_minutes)}（最长连续 {_minutes_label(initial_longest)}），{unfit_names}放不下。"
        )

    return {
        "tool": "plan_activities",
        "schema_version": 1,
        "now": _iso(_ceil_minute(now_dt)),
        "scope": scope_meta,
        "free_time_defined": True,
        "available_minutes": available_minutes,
        "initial_longest_free_minutes": initial_longest,
        "reserve_rest_minutes": reserve_rest_minutes,
        "requested_minutes": requested_total,
        "placed_minutes": placed_total,
        "feasible": sim["feasible"],
        "rest_satisfied": sim["rest_satisfied"],
        "rest_shortfall_minutes": sim["rest_shortfall_minutes"],
        "total_shortfall_minutes": sim["total_shortfall_minutes"],
        "placements": sim["placements"],
        "shortfall": sim["shortfall"],
        "leftover_minutes": sim["leftover_minutes"],
        "longest_free_minutes": sim["longest_free_minutes"],
        "free_slots": [_slot_dict(s, e) for s, e in sim["remaining_slots"]],
        "tradeoffs": tradeoffs,
        "warnings": computed["warnings"],
        "summary": summary,
    }
