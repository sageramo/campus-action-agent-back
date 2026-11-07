"""Rule-based strategies for 叠墨 (conflicts), attached to every calendar result.

Each conflict type maps to a fixed set of rules. A rule either names a concrete
change the page can apply in one click (skip one routine date, cancel or move an
event, change a task field, add free time) or points to the page where the user
checks the data. Times for moves and added free time are searched here, inside
the free time already derived, so an applied strategy does not create a new
overlap with routine or temporary occupancy. No model is called.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .conflict import merge_intervals
from .snapshot import _instant
from .time_utils import get_zone

Interval = tuple[datetime, datetime]
MOVE_SEARCH_DAYS = 3
ADD_FREE_SEARCH_DAYS = 7
SMALL_BLOCK = 15


def _iso(value: datetime) -> str:
    return value.isoformat()


def _clock(value: datetime) -> str:
    return value.astimezone(get_zone()).strftime("%H:%M")


def _when(value: datetime) -> str:
    local = value.astimezone(get_zone())
    return f"{local.month}月{local.day}日{['周一', '周二', '周三', '周四', '周五', '周六', '周日'][local.weekday()]} {local.strftime('%H:%M')}"


def _cut(intervals: list[Interval], busy: list[Interval]) -> list[Interval]:
    result = []
    for start, end in intervals:
        pieces = [(start, end)]
        for busy_start, busy_end in busy:
            next_pieces = []
            for piece_start, piece_end in pieces:
                if busy_end <= piece_start or busy_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if piece_start < busy_start:
                    next_pieces.append((piece_start, busy_start))
                if busy_end < piece_end:
                    next_pieces.append((busy_end, piece_end))
            pieces = next_pieces
        result.extend(pieces)
    return result


def _move_slots(start: datetime, end: datetime, open_time: list[Interval], reference: datetime, count: int = 2) -> list[Interval]:
    """Up to `count` slots of the same length, each on a different day, closest to the original time."""
    length = end - start
    earliest = max(reference, start - timedelta(days=MOVE_SEARCH_DAYS))
    latest = start + timedelta(days=MOVE_SEARCH_DAYS + 1)
    options: list[tuple[timedelta, datetime]] = []
    for gap_start, gap_end in open_time:
        gap_start = max(gap_start, earliest)
        gap_end = min(gap_end, latest)
        if gap_end - gap_start < length:
            continue
        local_gap = gap_start.astimezone(get_zone())
        wanted = start.astimezone(get_zone()).replace(year=local_gap.year, month=local_gap.month, day=local_gap.day)
        begin = min(max(wanted, gap_start), gap_end - length)
        options.append((abs(begin - start), begin))
    picked: list[Interval] = []
    days: set[date] = set()
    for _, begin in sorted(options):
        day = begin.astimezone(get_zone()).date()
        if day in days:
            continue
        days.add(day)
        picked.append((begin, begin + length))
        if len(picked) == count:
            break
    return picked


def _extra_free(snapshot: dict, deadline: datetime, minutes: int, reference: datetime, busy: list[Interval]) -> Interval | None:
    """A block of `minutes` right after (or before) a day's window, before the deadline and clear of occupancy."""
    zone = get_zone()
    windows = snapshot["daily_window"] or []
    day = deadline.astimezone(zone).date()
    for back in range(ADD_FREE_SEARCH_DAYS + 1):
        current = day - timedelta(days=back)
        window = windows[current.isoweekday() - 1] if windows else None
        if not window:
            continue
        midnight = datetime.combine(current, datetime.min.time(), tzinfo=zone)
        to_minutes = lambda text: int(text[:2]) * 60 + int(text[3:])
        candidates = [(midnight + timedelta(minutes=to_minutes(window["end"])), 1), (midnight + timedelta(minutes=to_minutes(window["start"])), -1)]
        for edge, direction in candidates:
            start, end = (edge, edge + timedelta(minutes=minutes)) if direction == 1 else (edge - timedelta(minutes=minutes), edge)
            if start < midnight or end > midnight + timedelta(days=1) or start < reference or end > deadline:
                continue
            if any(busy_start < end and busy_end > start for busy_start, busy_end in busy):
                continue
            return start, end
    return None


def _strategy(action: str, label: str, detail: str, params: dict[str, Any], recommended: bool = False) -> dict:
    return {"action": action, "label": label, "detail": detail, "params": params, "recommended": recommended}


def attach_strategies(conflicts: list[dict], *, snapshot: dict, reference: datetime, free: list[Interval],
                      work: list[Interval], busy: list[dict]) -> None:
    """Give every conflict an id, a one-line rule summary (advice) and its strategies, in place."""
    busy_spans = [(item["start"], item["end"]) for item in busy]
    tasks = {task["id"]: task for task in snapshot["tasks"]}
    events = {event["id"]: event for event in snapshot["events"]}
    for index, conflict in enumerate(conflicts):
        conflict["id"] = f"c{index + 1}"
        kind = conflict["type"]
        items = conflict["items"]
        strategies: list[dict] = []
        advice = ""
        if kind in ("appointment_routine_overlap", "appointment_overlap"):
            mine, other = items[0], items[1]
            start, end = _instant(mine["start"], "start"), _instant(mine["end"], "end")
            advice = "约定是自己定的，最容易挪：先找附近同样长的留白改约。"
            # The appointment's own time counts as open: it is the thing being moved.
            others = [(item["start"], item["end"]) for item in busy if item["id"] != mine["id"]]
            open_time = _cut(_cut(merge_intervals([*free, (start, end)]), others), work)
            for number, (slot_start, slot_end) in enumerate(_move_slots(start, end, open_time, reference)):
                strategies.append(_strategy("move_event", f"改约到{_when(slot_start)}", f"「{mine['title']}」挪到 {_when(slot_start)}–{_clock(slot_end)}，这段是空着的留白。",
                                            {"event_id": mine["id"], "start_at": _iso(slot_start), "end_at": _iso(slot_end)}, recommended=number == 0))
            if other["layer"] == "routine":
                routine_id, day = other["id"].rsplit("@", 1)
                strategies.append(_strategy("skip_routine_date", f"这次不上「{other['title']}」", f"在底色里记下 {day} 这一次不占用，记得事先请假。",
                                            {"routine_id": routine_id, "date": day}))
            else:
                strategies.append(_strategy("cancel_event", f"放弃「{other['title']}」", "保留约定，划掉另一件。", {"event_id": other["id"]}))
            strategies.append(_strategy("cancel_event", f"取消约定「{mine['title']}」", "约定不去了，从画卷上划掉。", {"event_id": mine["id"]}, recommended=not strategies))
        elif kind == "temporary_routine_overlap":
            event, routine = items[0], items[1]
            routine_id, day = routine["id"].rsplit("@", 1)
            advice = "点墨是临时、突发的事，错过就没有了；底色每周都有，缺一次的代价通常更小。"
            strategies.append(_strategy("skip_routine_date", f"这次不上「{routine['title']}」", f"去参加「{event['title']}」，底色里记下 {day} 这一次不占用，记得事先向老师请假。",
                                        {"routine_id": routine_id, "date": day}, recommended=True))
            strategies.append(_strategy("cancel_event", f"不去「{event['title']}」", f"照常上「{routine['title']}」，把这处点墨划掉。", {"event_id": event["id"]}))
        elif kind == "temporary_overlap":
            first, second = items[0], items[1]
            advice = "两处点墨都是别人定的时间，只能二选一；重叠不长时也可以先去一处再赶去另一处。"
            for keep, drop in ((first, second), (second, first)):
                strategies.append(_strategy("cancel_event", f"去「{keep['title']}」", f"划掉「{drop['title']}」。", {"event_id": drop["id"]}))
        elif kind == "routine_overlap":
            first, second = items[0], items[1]
            advice = "底色每周重复，两门课撞在一起多半是课表录入有误（例如单双周没标），先核对课表。"
            strategies.append(_strategy("review", "去铺纸核对课表", "检查这两项的星期、节次和单双周。", {"page": "time"}, recommended=True))
            for keep, drop in ((first, second), (second, first)):
                drop_id, day = drop["id"].rsplit("@", 1)
                strategies.append(_strategy("skip_routine_date", f"这次只上「{keep['title']}」", f"底色里记下 {day} 不上「{drop['title']}」。", {"routine_id": drop_id, "date": day}))
        elif kind == "work_shortage":
            task = tasks.get(items[0]["id"])
            missing = conflict["minutes"]
            if task:
                deadline = _instant(task["deadline_at"], "deadline")
                if not task["splittable"]:
                    advice = "这件事要求一口气做完，留白却是零碎的；允许拆开做最省事。"
                    strategies.append(_strategy("update_task", "允许拆开做", "分几段放进截止前的留白。", {"task_id": task["id"], "patch": {"splittable": True}}, recommended=True))
                elif not missing and (task["min_block_minutes"] or 30) > SMALL_BLOCK:
                    advice = "空档总量够，只是每段太短；把每段最短时长调小。"
                    strategies.append(_strategy("update_task", f"每段最短{SMALL_BLOCK}分钟", "更短的空档也能用上。", {"task_id": task["id"], "patch": {"min_block_minutes": SMALL_BLOCK}}, recommended=True))
                if missing:
                    advice = advice or f"截止前的留白还差约{missing}分钟，补一段留白或压缩用时。"
                    slot = _extra_free(snapshot, deadline, missing, reference, busy_spans)
                    if slot:
                        strategies.append(_strategy("add_free", f"{_when(slot[0])}起加{missing}分钟留白", f"把作息往外延一点：{_when(slot[0])}–{_clock(slot[1])}。",
                                                    {"start_at": _iso(slot[0]), "end_at": _iso(slot[1]), "note": f"为「{task['title']}」补的留白"}, recommended=not strategies))
                    if task["estimate_minutes"] and task["estimate_minutes"] - missing >= SMALL_BLOCK:
                        reduced = task["estimate_minutes"] - missing
                        strategies.append(_strategy("update_task", f"预计用时压到{reduced}分钟", "如果能做得更快，按留白够用的时长来做。", {"task_id": task["id"], "patch": {"estimate_minutes": reduced}}))
                strategies.append(_strategy("review", "去笔墨改截止或用时", "比如联系老师延期后改截止。", {"page": "items", "item": task["id"]}))
        elif kind == "overdue":
            task = tasks.get(items[0]["id"])
            advice = "截止已过：做完了就标记完成，没做也不必再做就取消，延期了就改截止。"
            if task:
                strategies.append(_strategy("update_task", "已经做完", "标记完成，不再提醒。", {"task_id": task["id"], "patch": {"status": "completed"}}, recommended=True))
                strategies.append(_strategy("update_task", "不做了", "标记取消。", {"task_id": task["id"], "patch": {"status": "cancelled"}}))
                strategies.append(_strategy("review", "延期了，去改截止", "在笔墨里改截止时间。", {"page": "items", "item": task["id"]}))
        conflict["advice"] = advice
        conflict["strategies"] = [strategy for strategy in strategies
                                  if strategy["action"] != "cancel_event" or strategy["params"]["event_id"] in events]
