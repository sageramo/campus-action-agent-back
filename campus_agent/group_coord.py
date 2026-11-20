"""Privacy-preserving multi-agent group scheduling (v3).

Two-stage decentralized protocol:
1. `export_availability_mask` (`POST /api/v3/group/export-mask`):
   Runs on a student's own v3 snapshot and exports ONLY a binary `'0'/'1'` availability
   bitmask across discrete time slots (15 / 30 / 60 min). Zero course titles, personal
   event names, locations, or task texts ever leave the student's local node.
2. `coordinate_group_slots` (`POST /api/v3/group/coordinate`):
   Aggregates N anonymous participants' bitmasks (with optional priority weights, e.g.
   organizer=2, member=1) to find the top-K contiguous meeting windows minimizing weighted
   conflicts and maximizing attendance, plus a per-slot availability heatmap.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import uuid
from typing import Any

from .layers import calendar_snapshot, validate_snapshot_v3
from .snapshot import _instant
from .time_utils import get_zone

ALLOWED_SLOT_MINUTES = {15, 30, 60}
MAX_GROUP_DAYS = 14
MAX_PARTICIPANTS = 200


def _parse_range(raw_range: Any) -> tuple[date, date]:
    if not isinstance(raw_range, dict) or not isinstance(raw_range.get("from"), str) or not isinstance(raw_range.get("to"), str):
        raise ValueError("range 必须包含 YYYY-MM-DD 格式的 from 与 to")
    start_day = date.fromisoformat(raw_range["from"])
    end_day = date.fromisoformat(raw_range["to"])
    if end_day < start_day or (end_day - start_day).days + 1 > MAX_GROUP_DAYS:
        raise ValueError(f"群体约时日期跨度必须在 1 到 {MAX_GROUP_DAYS} 天之间")
    return start_day, end_day


def _slot_grid(start_day: date, end_day: date, slot_minutes: int) -> list[tuple[datetime, datetime]]:
    zone = get_zone()
    cursor = datetime.combine(start_day, time(0), tzinfo=zone)
    finish = datetime.combine(end_day + timedelta(days=1), time(0), tzinfo=zone)
    step = timedelta(minutes=slot_minutes)
    grid: list[tuple[datetime, datetime]] = []
    while cursor < finish:
        grid.append((cursor, cursor + step))
        cursor += step
    return grid


def export_availability_mask(payload: object) -> dict[str, Any]:
    """POST /api/v3/group/export-mask: desensitize a v3 snapshot into a pure 0/1 bitmask."""
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise ValueError("请求必须包含 v3 snapshot 对象")
    snapshot = payload["snapshot"]
    validate_snapshot_v3(snapshot)
    start_day, end_day = _parse_range(payload.get("range"))
    slot_minutes = payload.get("slot_minutes", 30)
    if slot_minutes not in ALLOWED_SLOT_MINUTES:
        raise ValueError("slot_minutes 只能是 15、30 或 60")
    include_work_as_busy = bool(payload.get("include_work_as_busy", True))

    date_range = {"from": start_day.isoformat(), "to": end_day.isoformat()}
    cal = calendar_snapshot({"snapshot": snapshot, "range": date_range})
    free_spans = [(_instant(f["start"], "s"), _instant(f["end"], "e")) for f in cal["layers"]["free"]]
    work_spans = [
        (_instant(w["start"], "s"), _instant(w["end"], "e"))
        for w in cal["layers"]["work"]
        if include_work_as_busy or w.get("kind") == "appointment"
    ]

    grid = _slot_grid(start_day, end_day, slot_minutes)
    bits: list[str] = []
    for s_start, s_end in grid:
        inside_free = any(fs <= s_start and s_end <= fe for fs, fe in free_spans)
        hits_work = any(ws < s_end and we > s_start for ws, we in work_spans)
        bits.append("1" if (inside_free and not hits_work) else "0")

    mask = "".join(bits)
    return {
        "schema_version": 1,
        "participant_token": f"p-{uuid.uuid4().hex[:8]}",
        "range": date_range,
        "slot_minutes": slot_minutes,
        "slots_total": len(bits),
        "available_slots": mask.count("1"),
        "free_mask": mask,
        "privacy_guarantee": "仅包含脱敏后的 0/1 空闲位图，不含任何课程名、事件标题、地点或任务内容。",
    }


def coordinate_group_slots(payload: object) -> dict[str, Any]:
    """POST /api/v3/group/coordinate: aggregate anonymous bitmasks to find minimum-conflict slots."""
    if not isinstance(payload, dict):
        raise ValueError("请求JSON必须是对象")
    start_day, end_day = _parse_range(payload.get("range"))
    slot_minutes = payload.get("slot_minutes", 30)
    if slot_minutes not in ALLOWED_SLOT_MINUTES:
        raise ValueError("slot_minutes 只能是 15、30 或 60")
    duration_minutes = payload.get("duration_minutes")
    if not isinstance(duration_minutes, int) or isinstance(duration_minutes, bool) or duration_minutes < slot_minutes or duration_minutes % slot_minutes != 0:
        raise ValueError(f"duration_minutes 必须是 {slot_minutes} 分钟的正整数倍")
    top_k = payload.get("top_k", 5)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not (1 <= top_k <= 20):
        raise ValueError("top_k 必须是 1 到 20 之间的整数")

    participants = payload.get("participants")
    if not isinstance(participants, list) or not (1 <= len(participants) <= MAX_PARTICIPANTS):
        raise ValueError(f"participants 必须是包含 1 到 {MAX_PARTICIPANTS} 位成员的数组")

    grid = _slot_grid(start_day, end_day, slot_minutes)
    expected_len = len(grid)
    parsed: list[dict[str, Any]] = []
    for idx, member in enumerate(participants):
        if not isinstance(member, dict):
            raise ValueError(f"participants[{idx}] 必须是对象")
        mask = member.get("free_mask")
        if not isinstance(mask, str) or len(mask) != expected_len or any(ch not in "01" for ch in mask):
            raise ValueError(f"participants[{idx}].free_mask 必须是长度为 {expected_len} 的 0/1 位图")
        weight = member.get("weight", 1)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not (0 < weight <= 10):
            raise ValueError(f"participants[{idx}].weight 必须在 (0, 10] 之间")
        label = str(member.get("label") or member.get("participant_token") or f"成员{idx + 1}")[:40]
        parsed.append({"label": label, "weight": float(weight), "mask": mask})

    window_slots = duration_minutes // slot_minutes
    total_members = len(parsed)
    total_weight = sum(m["weight"] for m in parsed)

    # Per-slot heatmap (count of available participants)
    heatmap = [
        sum(1 for m in parsed if m["mask"][slot_idx] == "1")
        for slot_idx in range(expected_len)
    ]

    # Evaluate every contiguous candidate window (must stay within same calendar day)
    candidates: list[dict[str, Any]] = []
    for start_idx in range(expected_len - window_slots + 1):
        w_start = grid[start_idx][0]
        w_end = grid[start_idx + window_slots - 1][1]
        if w_start.date() != (w_end - timedelta(seconds=1)).date():
            continue  # Do not span midnight
        avail_count = 0
        conflict_weight = 0.0
        conflict_labels: list[str] = []
        for m in parsed:
            if all(m["mask"][k] == "1" for k in range(start_idx, start_idx + window_slots)):
                avail_count += 1
            else:
                conflict_weight += m["weight"]
                conflict_labels.append(m["label"])
        if avail_count == 0:
            continue
        # Mild daylight preference (08:00–22:00) as tie-breaker only
        hour = w_start.hour
        daytime_penalty = 0 if 8 <= hour <= 21 else 1
        candidates.append({
            "start": w_start.isoformat(),
            "end": w_end.isoformat(),
            "available_count": avail_count,
            "conflict_count": total_members - avail_count,
            "weighted_conflict": round(conflict_weight, 2),
            "attendance_rate": round(avail_count / total_members, 3),
            "conflict_labels": conflict_labels,
            "_sort": (round(conflict_weight, 4), total_members - avail_count, daytime_penalty, start_idx),
        })

    candidates.sort(key=lambda item: item["_sort"])
    # Pick top-K non-identical start windows (prefer distinct non-overlapping slots first)
    picked: list[dict[str, Any]] = []
    for cand in candidates:
        cand_start, cand_end = _instant(cand["start"], "s"), _instant(cand["end"], "e")
        if any(_instant(p["start"], "s") < cand_end and _instant(p["end"], "e") > cand_start for p in picked):
            continue
        item = {k: v for k, v in cand.items() if k != "_sort"}
        picked.append(item)
        if len(picked) >= top_k:
            break

    return {
        "schema_version": 1,
        "range": {"from": start_day.isoformat(), "to": end_day.isoformat()},
        "slot_minutes": slot_minutes,
        "duration_minutes": duration_minutes,
        "participant_count": total_members,
        "total_weight": round(total_weight, 2),
        "recommended_slots": picked,
        "heatmap": heatmap,
        "privacy_mode": "bitmask_only_no_schedule_details",
    }
