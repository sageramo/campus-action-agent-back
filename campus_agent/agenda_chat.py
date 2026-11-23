"""Read-only chat with deterministic schedule-capacity & confirmable action-proposal tools
(agenda_chat_v1 + agenda_chat_tools_v1 + agenda_chat_proposals_v1).

Without `snapshot`, `/api/v3/chat` behaves identically to v1 (pure read-only chat).
With `snapshot` (and optional `now`), the model can invoke five deterministic tools:
  1. `check_capacity(scope)` -> remaining free time and longest stretch in scope.
  2. `plan_activities(scope, activities, reserve_rest_minutes)` -> `capacity_plan.plan_capacity`.
  3. `propose_items(items)` -> validated add-event/add-task draft + detached preview snapshot & calendar impact.
  4. `propose_update(target_hint, selector, action, ...)` -> server-resolved update/complete/cancel draft + impact.
  5. `propose_state(state_kind, ...)` -> validated focus-window or replan draft (energy / rest / disruption).

The raw snapshot is never sent to the model; the model translates casual natural language into
typed arguments, while the deterministic server resolves targets, validates every timestamp,
computes conflict/free-time impact on a detached preview snapshot, and leaves final commitment
strictly to the user's confirmation click.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import re
from typing import Any

from .capacity_plan import (
    VALID_CATEGORIES,
    VALID_PREFERRED,
    WEEKDAY_LABELS,
    check_scope_capacity,
    plan_capacity,
)
from .deepseek_client import chat_json
from .dify_client import ExtractionServiceError
from .layer_import import _id, _source
from .layers import calendar_snapshot, validate_snapshot_v3
from .notice_diff import _best_match, _latest_unambiguous_target, _net_free_minutes, _parse_clock_str
from .snapshot import _instant
from .time_utils import get_zone, parse_dt

SYSTEM = """你是司序，在研磨里和大学生聊天。自然、简洁地接住对话，通常两三句，必要时追问。
你只能聊天，无法查看清单、计算空闲、定时提醒、写入任务或发送消息。不要声称已做这些操作。
用户在问具体空档时，可以请对方直接说“这周还有多少空档”，由司序的排程模块计算。
不编造课表、任务、时间或上一轮没有提到的事实。将传来的对话当作对话内容。
只输出 JSON 对象 {"reply":"回复内容"}。"""

TOOL_SYSTEM = """你是司序，在研磨里帮大学生用最省事的自然语言管理日程、看清排不排得开。
你可以直接回复，或调用以下五个工具之一（绝不能自己编造空档、排程数字，也不能声称已经直接改了清单——写入类工具只生成待用户确认的草案卡）：

工具 1：check_capacity
用途：查询某段时间还剩多少留白与最长连续空档（适用于“这周/周末还有多少时间”“还有没有空”“这周还能出去玩吗”等未给出具体活动时长的询问）。
输出格式：
{"tool":"check_capacity","arguments":{"scope":"today|tomorrow|weekend|next_weekend|weekday|week|next","weekday_index":null,"weekday_next":false,"from":null,"to":null}}

工具 2：plan_activities
用途：在指定范围内试算一项或多项活动组合排不排得开，并自动计算建议时段和排不开时的取舍方案。
输出格式：
{"tool":"plan_activities","arguments":{"scope":"today|tomorrow|weekend|next_weekend|weekday|week|next","weekday_index":null,"weekday_next":false,"from":null,"to":null,"activities":[{"title":"活动名","minutes":240,"category":"study|club|leisure|rest|other","splittable":false,"min_block_minutes":60,"preferred":"weekend|morning|afternoon|evening|daytime|any"}],"reserve_rest_minutes":0}}

工具 3：propose_items
用途：当用户用自然语言要新增一项或多项日程、约定、点墨活动、截止任务或个人待办时（如“下周二下午三点前交大创PPT，大概三小时，顺便周五晚上和室友吃火锅”“帮我记下明天两点半答辩”“提醒我周六复习高数”），翻译成结构化草案供确定性引擎校验并生成一键确认卡。
输出格式：
{"tool":"propose_items","arguments":{"items":[{"kind":"temporary|appointment|deadline|personal","title":"简明事项名","start_at":"YYYY-MM-DDTHH:MM:SS+08:00 或 null","end_at":"ISO 或 null","deadline_at":"ISO 或 null","earliest_start_at":"ISO 或 null","estimate_minutes":null,"location":null}]}}
四类严格区分：
- temporary（点墨）：外部安排、到点必须到场的校园活动/讲座/考试/答辩/面试/路演/集体会议（填 start_at，没说结束就留 null，绝不瞎编结束时间）；
- appointment（约定）：用户自己跟自己或朋友定下来的固定时段（如吃火锅、看电影、取快递、约自习，填 start_at 与可选 end_at）；
- deadline（截止之事）：有外部或明确交付节点的任务（如交作业、交大创PPT、填报名表，填 deadline_at）；
- personal（个人任务）：用户给自己立的弹性待办（如复习高数、背单词、洗衣服、买日用品，填 deadline_at，若指定某天做则同时填当天 00:00:00+08:00 到 23:59:00+08:00 或对应上午/下午/晚上时段）。
所有相对日期严格以输入里的 now 和 now_weekday 为基准换算。

工具 4：propose_update
用途：当用户要修改、改期、延后、估时、标记完成或取消清单里已有的事项时（如“把最急的那个往后挪一天”“刚才那场讲座改到下周六晚上七点”“实验报告我写完了”“摄影社说明会取消了”）。
输出格式：
{"tool":"propose_update","arguments":{"target_hint":"事项关键词或null","selector":"by_title|latest|most_urgent","action":"update|complete|cancel","start_at":null,"end_at":null,"deadline_at":null,"new_date":"YYYY-MM-DD或null","new_start_clock":"HH:MM或null","new_end_clock":"HH:MM或null","shift_days":null,"shift_minutes":null,"estimate_minutes":null,"location":null,"title":null}}

工具 5：propose_state
用途：当用户表达精力状态、想休息、突发临时打断、或要设置「常用专注时段」偏好时（如“刚路演完脑子彻底空了，今晚学不动”“待会四点到五点半被叫去搬东西”“把周末下午设为我的常用专注时段”）。
输出格式：
{"tool":"propose_state","arguments":{"state_kind":"focus_window|low_energy|high_energy|rest|disruption","weekday":{"start":"HH:MM","end":"HH:MM"},"weekend":{"start":"HH:MM","end":"HH:MM"},"start_at":"ISO或null","end_at":"ISO或null","title":"原因或标签"}}
注意：focus_window 只填用户提到的 weekday 和/或 weekend（未提的填 null）；low_energy/high_energy 未给时间段时 start_at/end_at 可填 null（后端默认从此刻起 2 小时）；rest/disruption 必须给出起止 ISO 时间。

调用规则：
1. 只要用户问“排不排得开 / 还有没有时间 / 能不能出去玩 / 空档多少”，调用 check_capacity 或 plan_activities。
2. 只要用户明确想记一件事、改一件事、标完成、取消、设常用专注时段、报精力低或突发打断，优先调用对应的 propose_items / propose_update / propose_state，由后端校验生成确认卡；不要把「设置常用专注时段」错调成 plan_activities。
3. 口语时长换算：“半天/一上午/一下午”按 240 分钟，“一晚上/半晚”按 180 分钟。weekday_index 用 0=周一 到 6=周日。
4. 纯闲聊、倾诉或信息完全不足无法构成任何操作时，直接输出 {"reply":"回复内容"}（追问只问一句）。
5. 严格只输出一个 JSON 对象，不要输出额外解释。"""

ANSWER_SYSTEM = """你是司序，在研磨里用后端工具返回的真实校验结果回答大学生。
要求：
1. 严格依据 tool_result 里的数字、时段和校验状态作答，绝对不编造任何未出现的时间、课程或任务。
2. 若为 plan_activities：先明确说“排得开”或“排不开”，给出具体建议时段（或放不下的项及缺口）、排完后剩余留白与最长连续空档；排不开时简明列出 2–3 个 tool_result.tradeoffs 里的取舍方案及后果。
3. 若为 check_capacity：给出该范围的剩余留白、最长连续空档和 1–2 个示例空档；如果用户原本想安排某项活动但没说时长（或 missing_duration_note 非空），在末尾只追问一句。
4. 若为 propose_items / propose_update / propose_state：若 proposal_error 非空，礼貌指出缺了什么或没匹配到哪项并只追问一句；若校验通过，简明复述草案核心内容（标题、时间、地点或冲突/留白变化），提示用户点下方卡片按钮确认即可落进日程（强调尚未擅自改动）。
5. 语气自然、简洁、有同理心，控制在 2–5 句，不用 Markdown 列表堆砌。
只输出 JSON 对象 {"reply":"回复内容"}。"""

ALLOWED_CHAT_KEYS = {"mode", "messages", "snapshot", "now"}


def _validate_messages(messages: object) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not 1 <= len(messages) <= 9:
        raise ValueError("聊天需要1至9条消息")
    for index, message in enumerate(messages):
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "text"}
            or message["role"] != ("user" if index % 2 == 0 else "assistant")
            or not isinstance(message["text"], str)
            or not 1 <= len(message["text"].strip()) <= 2000
        ):
            raise ValueError("聊天消息应交替出现，每条最多2000字")
    if messages[-1]["role"] != "user":
        raise ValueError("最后一条应为用户消息")
    return messages


CHINESE_SCOPE_MAP = {
    "今天": "today",
    "今日": "today",
    "今晚": "today",
    "明天": "tomorrow",
    "明日": "tomorrow",
    "周末": "weekend",
    "本周末": "weekend",
    "这周末": "weekend",
    "下周末": "next_weekend",
    "下个周末": "next_weekend",
    "本周": "week",
    "这周": "week",
    "下周": "next",
    "下个周": "next",
}
CHINESE_WEEKDAY_CHARS = "一二三四五六日天"
CHINESE_CATEGORY_MAP = {
    "学习": "study",
    "作业": "study",
    "复习": "study",
    "社团": "club",
    "学生组织": "club",
    "娱乐": "leisure",
    "放松": "leisure",
    "玩": "leisure",
    "休息": "rest",
    "其他": "other",
}
CHINESE_PREFERRED_MAP = {
    "周末": "weekend",
    "晚上": "evening",
    "晚间": "evening",
    "夜晚": "evening",
    "白天": "daytime",
    "上午": "morning",
    "早上": "morning",
    "下午": "afternoon",
    "任意": "any",
    "不限": "any",
}


def _normalize_model_scope(raw_scope: object) -> tuple[object, int | None, bool | None]:
    """Normalize Chinese scope strings an LLM might emit into canonical agenda.js scopes."""
    if not isinstance(raw_scope, str):
        return raw_scope, None, None
    cleaned = raw_scope.strip()
    if cleaned in CHINESE_SCOPE_MAP:
        return CHINESE_SCOPE_MAP[cleaned], None, None
    # Match 周一..周日 / 本周一..本周日 / 这周五 / 下周三 / 星期六 / 礼拜天
    for prefix, is_next in (("下个", True), ("下", True), ("本", False), ("这", False), ("", False)):
        if prefix and not cleaned.startswith(prefix):
            continue
        rest = cleaned[len(prefix):]
        for marker in ("周", "星期", "礼拜"):
            if rest.startswith(marker) and len(rest) == len(marker) + 1:
                ch = rest[-1]
                pos = CHINESE_WEEKDAY_CHARS.find(ch)
                if pos >= 0:
                    idx = 6 if pos == 7 else pos
                    return "weekday", idx, is_next
    return cleaned, None, None


def _extract_scope_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.get("from") is not None and args.get("to") is not None:
        out["from"] = args["from"]
        out["to"] = args["to"]
    elif args.get("range") is not None:
        out["range"] = args["range"]
    else:
        raw_scope = args.get("scope") or "week"
        norm_scope, inferred_idx, inferred_next = _normalize_model_scope(raw_scope)
        out["scope"] = norm_scope
        if args.get("weekday_index") is not None:
            out["weekday_index"] = _coerce_int(args["weekday_index"])
        elif args.get("weekdayIndex") is not None:
            out["weekday_index"] = _coerce_int(args["weekdayIndex"])
        elif inferred_idx is not None:
            out["weekday_index"] = inferred_idx
        if args.get("weekday_next") is not None:
            out["weekday_next"] = bool(args["weekday_next"])
        elif args.get("weekdayNext") is not None:
            out["weekday_next"] = bool(args["weekdayNext"])
        elif inferred_next is not None:
            out["weekday_next"] = inferred_next
    return out


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            num = float(raw)
        except ValueError:
            return None
        if num.is_integer():
            return int(num)
    return None


def _sanitize_model_activities(raw: object) -> tuple[list[dict[str, Any]], list[str]]:
    """Coerce benign LLM field quirks while flagging activities genuinely missing durations."""
    if not isinstance(raw, list) or not raw:
        return [], ["未提供待试算活动"]
    cleaned: list[dict[str, Any]] = []
    missing: list[str] = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:120]
        if not title:
            continue
        mins = _coerce_int(item.get("minutes"))
        if mins is None or mins <= 0:
            missing.append(title)
            continue
        cat_raw = str(item.get("category") or "other").strip()
        cat = CHINESE_CATEGORY_MAP.get(cat_raw, cat_raw.lower())
        if cat not in VALID_CATEGORIES:
            cat = "other"
        splittable = bool(item.get("splittable", False))
        min_blk = _coerce_int(item.get("min_block_minutes"))
        if min_blk is not None and (min_blk < 1 or min_blk > mins):
            min_blk = min(30, mins) if splittable else mins
        pref_raw = str(item.get("preferred") or "any").strip()
        pref = CHINESE_PREFERRED_MAP.get(pref_raw, pref_raw.lower())
        if pref not in VALID_PREFERRED:
            pref = "any"
        entry: dict[str, Any] = {
            "title": title,
            "minutes": mins,
            "category": cat,
            "splittable": splittable,
            "preferred": pref,
        }
        if min_blk is not None:
            entry["min_block_minutes"] = min_blk
        cleaned.append(entry)
    return cleaned, missing


HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SUPPORTED_TOOLS = {"check_capacity", "plan_activities", "propose_items", "propose_update", "propose_state"}


def _parse_local_iso(raw: object, zone) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = parse_dt(raw.strip())
    except (ValueError, TypeError):
        return None
    if parsed is None:
        return None
    return parsed.astimezone(zone).isoformat()


def _validate_hhmm_span(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    start = str(raw.get("start") or "").strip()
    end = str(raw.get("end") or "").strip()
    m1, m2 = HHMM_RE.match(start), HHMM_RE.match(end)
    if not m1 or not m2:
        return None
    s_min = int(m1.group(1)) * 60 + int(m1.group(2))
    e_min = int(m2.group(1)) * 60 + int(m2.group(2))
    if e_min <= s_min:
        return None
    return {"start": start, "end": end}


def _calendar_impact(snapshot: dict, preview: dict, anchor_day: date) -> dict[str, int] | None:
    if not snapshot.get("daily_window"):
        return None
    monday = anchor_day - timedelta(days=anchor_day.weekday())
    rng = {"from": monday.isoformat(), "to": (monday + timedelta(days=6)).isoformat()}
    try:
        cal_before = calendar_snapshot({"snapshot": snapshot, "range": rng})
        cal_after = calendar_snapshot({"snapshot": preview, "range": rng})
    except Exception:
        return None
    return {
        "conflicts_before": len(cal_before["conflicts"]),
        "conflicts_after": len(cal_after["conflicts"]),
        "free_delta_minutes": _net_free_minutes(cal_after) - _net_free_minutes(cal_before),
    }


def _build_add_items_proposal(
    snapshot: dict, raw_args: dict[str, Any], user_text: str, now_local: datetime, zone
) -> tuple[dict[str, Any] | None, str | None]:
    raw_items = raw_args.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return None, "没有识别出具体的待记事项，请说明要记录的事情和时间。"
    source = _source(user_text)
    new_events: list[dict[str, Any]] = []
    new_tasks: list[dict[str, Any]] = []
    summary_items: list[dict[str, Any]] = []
    anchor_day = now_local.date()

    for entry in raw_items[:10]:
        if not isinstance(entry, dict):
            continue
        raw_kind = str(entry.get("kind") or "").strip().lower()
        kind_map = {
            "temporary": ("event", "temporary"),
            "点墨": ("event", "temporary"),
            "appointment": ("event", "appointment"),
            "event": ("event", "appointment"),
            "约定": ("event", "appointment"),
            "deadline": ("task", "deadline"),
            "task": ("task", "deadline"),
            "截止": ("task", "deadline"),
            "截止之事": ("task", "deadline"),
            "personal": ("task", "personal"),
            "个人任务": ("task", "personal"),
        }
        mapped = kind_map.get(raw_kind)
        title = str(entry.get("title") or "").strip()[:200]
        if not title or mapped is None:
            continue
        bucket, layer_kind = mapped
        location = str(entry.get("location") or "").strip()[:100] or None
        evidence = [{"field": "title", "origin": "source_text", "source_id": source["id"], "quote": user_text[:200]}]
        notes = (["个人任务"] if layer_kind == "personal" else []) + ["由司序语义理解生成草案，经用户确认记入"]

        if bucket == "event":
            start_at = _parse_local_iso(entry.get("start_at"), zone)
            end_at = _parse_local_iso(entry.get("end_at"), zone)
            if start_at and end_at and _instant(end_at, "end_at") <= _instant(start_at, "start_at"):
                end_at = None
            if start_at:
                anchor_day = _instant(start_at, "start_at").astimezone(zone).date()
                evidence.append({"field": "start_at", "origin": "source_text", "source_id": source["id"], "quote": user_text[:200]})
            if end_at:
                evidence.append({"field": "end_at", "origin": "source_text", "source_id": source["id"], "quote": user_text[:200]})
            ev = {
                "id": _id("e"),
                "kind": layer_kind,
                "title": title,
                "start_at": start_at,
                "end_at": end_at,
                "location": location,
                "status": "active",
                "source_id": source["id"],
                "evidence": evidence,
                "notes": notes,
            }
            new_events.append(ev)
            summary_items.append({
                "kind": "event",
                "layer": layer_kind,
                "title": title,
                "start_at": start_at,
                "end_at": end_at,
                "location": location,
            })
        else:
            deadline_at = _parse_local_iso(entry.get("deadline_at"), zone)
            earliest = _parse_local_iso(entry.get("earliest_start_at"), zone)
            if earliest and deadline_at and _instant(earliest, "earliest_start_at") >= _instant(deadline_at, "deadline_at"):
                earliest = None
            est = _coerce_int(entry.get("estimate_minutes"))
            if est is not None and not (1 <= est <= 1440):
                est = None
            if deadline_at:
                anchor_day = _instant(deadline_at, "deadline_at").astimezone(zone).date()
                evidence.append({"field": "deadline_at", "origin": "source_text", "source_id": source["id"], "quote": user_text[:200]})
            tk = {
                "id": _id("t"),
                "title": title,
                "deadline_at": deadline_at,
                "estimate_minutes": est,
                "splittable": True,
                "min_block_minutes": None,
                "earliest_start_at": earliest,
                "priority": None,
                "status": "active",
                "location": location,
                "materials": [],
                "source_id": source["id"],
                "evidence": evidence,
                "notes": notes,
            }
            new_tasks.append(tk)
            summary_items.append({
                "kind": "task",
                "layer": layer_kind,
                "title": title,
                "deadline_at": deadline_at,
                "earliest_start_at": earliest,
                "estimate_minutes": est,
                "location": location,
            })

    if not summary_items:
        return None, "未能提取到有效的事项或合法时间，请补充具体事项名称和时间。"

    preview = deepcopy(snapshot)
    preview["revision"] = int(preview.get("revision", 0)) + 1
    preview["sources"].append(source)
    preview["events"].extend(new_events)
    preview["tasks"].extend(new_tasks)
    validate_snapshot_v3(preview)
    impact = _calendar_impact(snapshot, preview, anchor_day)
    return {
        "tool": "propose_items",
        "kind": "add_items",
        "items": summary_items,
        "preview_snapshot": preview,
        "impact": impact,
        "week_anchor": anchor_day.isoformat(),
    }, None


def _resolve_update_target(snapshot: dict, target_hint: str, selector: str) -> tuple[str, dict] | None:
    if target_hint:
        ev_match, ev_score = _best_match(target_hint, snapshot.get("events", []), set())
        tk_match, tk_score = _best_match(target_hint, snapshot.get("tasks", []), set())
        if ev_match and ev_score >= tk_score:
            return "event", ev_match
        if tk_match:
            return "task", tk_match
    if selector == "most_urgent":
        active_tasks = [
            tk for tk in snapshot.get("tasks", [])
            if tk.get("status") == "active" and tk.get("deadline_at")
        ]
        if active_tasks:
            active_tasks.sort(key=lambda item: item["deadline_at"])
            return "task", active_tasks[0]
        active_events = [
            ev for ev in snapshot.get("events", [])
            if ev.get("status") == "active" and ev.get("start_at")
        ]
        if active_events:
            active_events.sort(key=lambda item: item["start_at"])
            return "event", active_events[0]
    if selector == "latest" or not target_hint:
        return _latest_unambiguous_target(snapshot, set())
    return None


def _build_update_proposal(
    snapshot: dict, raw_args: dict[str, Any], user_text: str, now_local: datetime, zone
) -> tuple[dict[str, Any] | None, str | None]:
    target_hint = str(raw_args.get("target_hint") or "").strip()[:120]
    selector = str(raw_args.get("selector") or ("by_title" if target_hint else "latest")).strip().lower()
    action = str(raw_args.get("action") or "update").strip().lower()
    if action not in {"update", "complete", "cancel"}:
        action = "update"

    picked = _resolve_update_target(snapshot, target_hint, selector)
    if picked is None:
        return None, "在当前清单里没有定位到唯一的活跃事项，请说一下具体要改哪件事的名称。"
    kind, orig = picked
    preview = deepcopy(snapshot)
    preview["revision"] = int(preview.get("revision", 0)) + 1
    source = _source(user_text)
    preview["sources"].append(source)
    pool = preview["events"] if kind == "event" else preview["tasks"]
    target = next(item for item in pool if item["id"] == orig["id"])
    changes: dict[str, dict[str, Any]] = {}

    if action == "cancel":
        target["status"] = "cancelled"
        changes["status"] = {"from": "active", "to": "cancelled"}
    elif action == "complete":
        next_status = "completed" if kind == "task" else "cancelled"
        target["status"] = next_status
        changes["status"] = {"from": "active", "to": next_status}
    else:
        shift_days = _coerce_int(raw_args.get("shift_days")) or 0
        shift_minutes = _coerce_int(raw_args.get("shift_minutes")) or 0
        shift_delta = timedelta(days=shift_days, minutes=shift_minutes)
        new_date_raw = str(raw_args.get("new_date") or "").strip()
        new_day: date | None = None
        if new_date_raw:
            try:
                new_day = date.fromisoformat(new_date_raw[:10])
            except ValueError:
                new_day = None
        start_clk = str(raw_args.get("new_start_clock") or "").strip()
        end_clk = str(raw_args.get("new_end_clock") or "").strip()

        if kind == "event":
            new_start = _parse_local_iso(raw_args.get("start_at"), zone)
            new_end = _parse_local_iso(raw_args.get("end_at"), zone)
            old_start_dt = _instant(target["start_at"], "start_at").astimezone(zone) if target.get("start_at") else None
            old_end_dt = _instant(target["end_at"], "end_at").astimezone(zone) if target.get("end_at") else None
            if not new_start and old_start_dt is not None:
                cand = old_start_dt
                if new_day is not None:
                    cand = cand.replace(year=new_day.year, month=new_day.month, day=new_day.day)
                if HHMM_RE.match(start_clk):
                    h, m, s = _parse_clock_str(start_clk)
                    cand = cand.replace(hour=h, minute=m, second=s, microsecond=0)
                if shift_delta:
                    cand = cand + shift_delta
                if cand.isoformat() != target["start_at"]:
                    new_start = cand.isoformat()
            if not new_end and old_end_dt is not None:
                if HHMM_RE.match(end_clk):
                    base_dt = _instant(new_start, "start_at").astimezone(zone) if new_start else old_start_dt
                    if base_dt is not None:
                        eh, em, es = _parse_clock_str(end_clk)
                        cand_end = base_dt.replace(hour=eh, minute=em, second=es, microsecond=0)
                        if cand_end <= base_dt:
                            cand_end += timedelta(days=1)
                        new_end = cand_end.isoformat()
                elif new_start and old_start_dt is not None:
                    new_end = (_instant(new_start, "start_at").astimezone(zone) + (old_end_dt - old_start_dt)).isoformat()
            if new_start and new_start != target.get("start_at"):
                changes["start_at"] = {"from": target.get("start_at"), "to": new_start}
                target["start_at"] = new_start
            if new_end and new_end != target.get("end_at"):
                if not target.get("start_at") or _instant(new_end, "end_at") > _instant(target["start_at"], "start_at"):
                    changes["end_at"] = {"from": target.get("end_at"), "to": new_end}
                    target["end_at"] = new_end
        else:
            new_dl = _parse_local_iso(raw_args.get("deadline_at"), zone)
            old_dl_dt = _instant(target["deadline_at"], "deadline_at").astimezone(zone) if target.get("deadline_at") else None
            if not new_dl and old_dl_dt is not None:
                cand = old_dl_dt
                if new_day is not None:
                    cand = cand.replace(year=new_day.year, month=new_day.month, day=new_day.day)
                if HHMM_RE.match(end_clk or start_clk):
                    h, m, s = _parse_clock_str(end_clk or start_clk)
                    cand = cand.replace(hour=h, minute=m, second=s, microsecond=0)
                if shift_delta:
                    cand = cand + shift_delta
                if cand.isoformat() != target["deadline_at"]:
                    new_dl = cand.isoformat()
            if new_dl and new_dl != target.get("deadline_at"):
                changes["deadline_at"] = {"from": target.get("deadline_at"), "to": new_dl}
                target["deadline_at"] = new_dl
                if target.get("earliest_start_at") and old_dl_dt is not None:
                    old_ea_dt = _instant(target["earliest_start_at"], "earliest_start_at").astimezone(zone)
                    shifted_ea = old_ea_dt + (_instant(new_dl, "deadline_at").astimezone(zone) - old_dl_dt)
                    if shifted_ea < _instant(new_dl, "deadline_at").astimezone(zone):
                        changes["earliest_start_at"] = {"from": target["earliest_start_at"], "to": shifted_ea.isoformat()}
                        target["earliest_start_at"] = shifted_ea.isoformat()
            est = _coerce_int(raw_args.get("estimate_minutes"))
            if est is not None and 1 <= est <= 1440 and est != target.get("estimate_minutes"):
                changes["estimate_minutes"] = {"from": target.get("estimate_minutes"), "to": est}
                target["estimate_minutes"] = est

        new_loc = str(raw_args.get("location") or "").strip()[:100]
        if new_loc and new_loc != target.get("location"):
            changes["location"] = {"from": target.get("location"), "to": new_loc}
            target["location"] = new_loc
        new_title = str(raw_args.get("title") or "").strip()[:200]
        if new_title and new_title != target.get("title"):
            changes["title"] = {"from": target.get("title"), "to": new_title}
            target["title"] = new_title

    if not changes:
        return None, "没有识别出需要改动的新时间、地点或状态，请具体说明想怎么改。"

    for field in changes:
        if field in {"start_at", "end_at", "deadline_at", "earliest_start_at", "location", "title"}:
            target["evidence"].append({"field": field, "origin": "source_text", "source_id": source["id"], "quote": user_text[:200]})
    validate_snapshot_v3(preview)
    anchor_iso = target.get("start_at") or target.get("deadline_at")
    anchor_day = _instant(anchor_iso, "anchor").astimezone(zone).date() if anchor_iso else now_local.date()
    impact = _calendar_impact(snapshot, preview, anchor_day)
    return {
        "tool": "propose_update",
        "kind": "update_item",
        "item_kind": kind,
        "target_id": target["id"],
        "target_title": orig["title"],
        "action": action,
        "changes": changes,
        "preview_snapshot": preview,
        "impact": impact,
        "week_anchor": anchor_day.isoformat(),
    }, None


def _build_state_proposal(
    raw_args: dict[str, Any], user_text: str, now_local: datetime, zone
) -> tuple[dict[str, Any] | None, str | None]:
    state_kind = str(raw_args.get("state_kind") or "").strip().lower()
    if state_kind == "focus_window":
        wd = _validate_hhmm_span(raw_args.get("weekday"))
        we = _validate_hhmm_span(raw_args.get("weekend"))
        if not wd and not we:
            return None, "请说明想设成常用专注时段的具体钟点，例如工作日 19:00–21:00 或周末 14:00–18:00。"
        return {
            "tool": "propose_state",
            "kind": "focus_window",
            "weekday": wd,
            "weekend": we,
        }, None

    if state_kind not in {"low_energy", "high_energy", "rest", "disruption"}:
        return None, "没能判断你想记录精力状态、休息还是突发打断，请再说具体一点。"

    start_at = _parse_local_iso(raw_args.get("start_at"), zone)
    end_at = _parse_local_iso(raw_args.get("end_at"), zone)
    if state_kind in {"low_energy", "high_energy"} and (not start_at or not end_at):
        rem = (5 - now_local.minute % 5) % 5
        rounded = (now_local + timedelta(minutes=rem)).replace(second=0, microsecond=0)
        start_at = start_at or rounded.isoformat()
        end_at = end_at or (_instant(start_at, "start_at").astimezone(zone) + timedelta(hours=2)).isoformat()
    if not start_at or not end_at or _instant(end_at, "end_at") <= _instant(start_at, "start_at"):
        return None, "请补充具体起止时间，例如「下午 4 点到 5 点半」。"

    title = str(raw_args.get("title") or "").strip()[:80]
    if state_kind in {"low_energy", "high_energy"}:
        level = "low" if state_kind == "low_energy" else "high"
        label = "精力偏低" if level == "low" else "状态不错"
        draft = {
            "kind": "state",
            "state": {
                "id": _id("st"),
                "level": level,
                "start_at": start_at,
                "end_at": end_at,
                "text": (title or user_text)[:120],
            },
        }
    elif state_kind == "rest":
        label = title or "休息"
        draft = {"kind": "rest", "rest": {"start": start_at, "end": end_at, "label": label}}
    else:
        label = title or "临时安排"
        draft = {"kind": "disruption", "disruption": {"title": label, "start_at": start_at, "end_at": end_at}}

    return {
        "tool": "propose_state",
        "kind": "replan_draft",
        "state_kind": state_kind,
        "label": label,
        "start_at": start_at,
        "end_at": end_at,
        "draft": draft,
    }, None


def _require_reply(result: object) -> str:
    if not isinstance(result, dict):
        raise ExtractionServiceError("聊天模型没有返回可用的回复。", 502, "model_invalid_chat")
    reply = result.get("reply")
    if not isinstance(reply, str) or not 1 <= len(reply.strip()) <= 1200:
        raise ExtractionServiceError("聊天模型没有返回可用的回复。", 502, "model_invalid_chat")
    return reply.strip()


def agenda_chat(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("聊天需要 JSON 对象")
    unknown = set(payload) - ALLOWED_CHAT_KEYS
    if unknown or payload.get("mode") != "model" or "messages" not in payload:
        raise ValueError("聊天需要 model 模式及 messages")
    messages = _validate_messages(payload["messages"])

    snapshot = payload.get("snapshot")
    has_snapshot = snapshot is not None
    if "now" in payload and payload["now"] is None:
        raise ValueError("now 不能显式传 null；请提供当前时刻或省略")
    now_raw = payload.get("now")
    if now_raw is not None:
        _instant(now_raw, "now")

    # Backwards-compatible pure read-only chat (agenda_chat_v1) when snapshot is not provided.
    if not has_snapshot:
        result = chat_json(SYSTEM, json.dumps(messages, ensure_ascii=False), max_tokens=550)
        return {"reply": _require_reply(result)}

    validate_snapshot_v3(snapshot)
    now_dt = _instant(now_raw, "now") if now_raw is not None else _instant(snapshot["reference_time"], "reference_time")
    now_local = now_dt.astimezone(get_zone())
    now_iso = now_local.isoformat()

    step1_input = {
        "now": now_iso,
        "now_weekday": WEEKDAY_LABELS[now_local.isoweekday() - 1],
        "messages": messages,
    }
    first = chat_json(TOOL_SYSTEM, json.dumps(step1_input, ensure_ascii=False), max_tokens=650)
    if not isinstance(first, dict):
        raise ExtractionServiceError("聊天模型没有返回可用的回复。", 502, "model_invalid_chat")

    tool_name = first.get("tool")
    if not tool_name:
        return {"reply": _require_reply(first)}

    if not isinstance(tool_name, str) or tool_name not in SUPPORTED_TOOLS:
        raise ExtractionServiceError("聊天模型请求了不支持的工具。", 502, "model_invalid_chat")

    raw_args = first.get("arguments")
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ExtractionServiceError("聊天模型工具参数格式无效。", 502, "model_invalid_chat")

    if tool_name in {"propose_items", "propose_update", "propose_state"}:
        zone = get_zone()
        user_text = messages[-1]["text"].strip()
        if tool_name == "propose_items":
            proposal, proposal_error = _build_add_items_proposal(snapshot, raw_args, user_text, now_local, zone)
        elif tool_name == "propose_update":
            proposal, proposal_error = _build_update_proposal(snapshot, raw_args, user_text, now_local, zone)
        else:
            proposal, proposal_error = _build_state_proposal(raw_args, user_text, now_local, zone)
        step2_payload: dict[str, Any] = {
            "now": now_iso,
            "messages": messages,
            "tool": tool_name,
            "tool_result": (
                {k: v for k, v in proposal.items() if k != "preview_snapshot"}
                if proposal is not None
                else {}
            ),
        }
        if proposal_error:
            step2_payload["proposal_error"] = proposal_error
        second = chat_json(ANSWER_SYSTEM, json.dumps(step2_payload, ensure_ascii=False), max_tokens=650)
        out: dict[str, Any] = {"reply": _require_reply(second)}
        if proposal is not None:
            out["proposal"] = proposal
        return out

    scope_args = _extract_scope_args(raw_args)
    missing_note: str | None = None

    if tool_name == "check_capacity":
        try:
            tool_result = check_scope_capacity({"snapshot": snapshot, "now": now_iso, **scope_args})
        except ValueError:
            # Safe fallback to default week scope if model emitted a malformed scope
            tool_result = check_scope_capacity({"snapshot": snapshot, "now": now_iso, "scope": "week"})
    else:
        activities, missing = _sanitize_model_activities(raw_args.get("activities"))
        reserve_rest = _coerce_int(raw_args.get("reserve_rest_minutes"))
        if reserve_rest is None or reserve_rest < 0:
            reserve_rest = 0
        if missing or not activities:
            # Activity duration(s) not yet specified: still return exact bare capacity and let step 2 ask 1 question.
            try:
                tool_result = check_scope_capacity({"snapshot": snapshot, "now": now_iso, **scope_args})
            except ValueError:
                tool_result = check_scope_capacity({"snapshot": snapshot, "now": now_iso, "scope": "week"})
            missing_note = f"以下活动尚未说明时长：{'、'.join(missing)}。请先报出真实剩余留白，再只追问一句大概需要几个小时。"
        else:
            try:
                tool_result = plan_capacity(
                    {
                        "snapshot": snapshot,
                        "now": now_iso,
                        **scope_args,
                        "activities": activities,
                        "reserve_rest_minutes": reserve_rest,
                    }
                )
            except ValueError:
                # Fallback to default week scope if scope parameters were malformed
                tool_result = plan_capacity(
                    {
                        "snapshot": snapshot,
                        "now": now_iso,
                        "scope": "week",
                        "activities": activities,
                        "reserve_rest_minutes": reserve_rest,
                    }
                )

    step2_input: dict[str, Any] = {
        "now": now_iso,
        "messages": messages,
        "tool": tool_result.get("tool", tool_name),
        "tool_result": tool_result,
    }
    if missing_note:
        step2_input["missing_duration_note"] = missing_note

    second = chat_json(ANSWER_SYSTEM, json.dumps(step2_input, ensure_ascii=False), max_tokens=650)
    return {"reply": _require_reply(second), "capacity": tool_result}
