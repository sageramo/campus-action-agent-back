"""Read-only chat with deterministic schedule-capacity tools (agenda_chat_v1 + agenda_chat_tools_v1).

Without `snapshot`, `/api/v3/chat` behaves identically to v1 (pure read-only chat).
With `snapshot` (and optional `now`), the model can invoke two deterministic tools:
  1. `check_capacity(scope)` -> remaining free time and longest stretch in scope.
  2. `plan_activities(scope, activities, reserve_rest_minutes)` -> `capacity_plan.plan_capacity`.

The raw snapshot is never sent to the model; only the server-computed tool result is
returned to the model for phrasing and returned to the frontend as `capacity`.
"""
from __future__ import annotations

import json
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
from .layers import validate_snapshot_v3
from .snapshot import _instant
from .time_utils import get_zone

SYSTEM = """你是司序，在研磨里和大学生聊天。自然、简洁地接住对话，通常两三句，必要时追问。
你只能聊天，无法查看清单、计算空闲、定时提醒、写入任务或发送消息。不要声称已做这些操作。
用户在问具体空档时，可以请对方直接说“这周还有多少空档”，由司序的排程模块计算。
不编造课表、任务、时间或上一轮没有提到的事实。将传来的对话当作对话内容。
只输出 JSON 对象 {"reply":"回复内容"}。"""

TOOL_SYSTEM = """你是司序，在研磨里帮大学生看清学习、社团、娱乐排不排得开。
你可以直接回复，或调用以下两个只读工具之一（绝不能自己编造空档或排程数字）：

工具 1：check_capacity
用途：查询某段时间还剩多少留白与最长连续空档（适用于“这周/周末还有多少时间”“还有没有空”“这周还能出去玩吗”等未给出具体活动时长的询问）。
输出格式：
{"tool":"check_capacity","arguments":{"scope":"today|tomorrow|weekend|next_weekend|weekday|week|next","weekday_index":null,"weekday_next":false,"from":null,"to":null}}

工具 2：plan_activities
用途：在指定范围内试算一项或多项活动组合排不排得开，并自动计算建议时段和排不开时的取舍方案。
输出格式：
{"tool":"plan_activities","arguments":{"scope":"today|tomorrow|weekend|next_weekend|weekday|week|next","weekday_index":null,"weekday_next":false,"from":null,"to":null,"activities":[{"title":"活动名","minutes":240,"category":"study|club|leisure|rest|other","splittable":false,"min_block_minutes":60,"preferred":"weekend|evening|daytime|any"}],"reserve_rest_minutes":0}}

调用规则：
1. 只要用户问“排不排得开 / 还有没有时间 / 能不能出去玩 / 空档多少 / 能不能再接社团或活动”，必须先调用工具，不得直接猜时间。
2. 口语时长换算：“半天 / 一上午 / 一下午”按 240 分钟（4 小时），“一晚上 / 半晚”按 180 分钟（3 小时）。学习/作业类活动未说明不能拆时可设 splittable=true（min_block_minutes=60），外出娱乐/社团活动默认 splittable=false。
3. weekday_index 使用 0=周一 到 6=周日；仅当 scope="weekday" 时填写，其余情况为 null。用户未说时间范围但问“这周/最近排得开吗”默认 scope="week"。
4. 如果用户提到了具体活动但完全没说时长（例如只问“能出去玩吗”没说几小时）：先调用 check_capacity 查对应范围的剩余留白，方便下一轮给出真实空档并只追问一句时长；如果连范围也没说清或只是日常闲聊/倾诉，直接输出 {"reply":"回复内容"}（追问只问一句）。
5. 严格只输出一个 JSON 对象，不要输出额外解释。"""

ANSWER_SYSTEM = """你是司序，在研磨里用工具返回的真实排程结果回答大学生。
要求：
1. 严格依据 tool_result 里的数字和时段作答，绝对不编造任何未出现的时间、课程或任务。
2. 若为 plan_activities：先明确说“排得开”或“排不开”，给出具体建议时段（或放不下的项及缺口）、排完后剩余留白与最长连续空档；排不开时简明列出 2–3 个 tool_result.tradeoffs 里的取舍方案及后果。
3. 若为 check_capacity：给出该范围的剩余留白、最长连续空档和 1–2 个示例空档；如果用户原本想安排某项活动但没说时长（或 missing_duration_note 非空），在末尾只追问一句，例如“出去玩大概几个小时？”。
4. 语气自然、简洁、有同理心，控制在 2–5 句，不用 Markdown 列表堆砌。
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


def _extract_scope_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.get("from") is not None and args.get("to") is not None:
        out["from"] = args["from"]
        out["to"] = args["to"]
    elif args.get("range") is not None:
        out["range"] = args["range"]
    else:
        scope = args.get("scope") or "week"
        out["scope"] = scope
        if args.get("weekday_index") is not None:
            out["weekday_index"] = args["weekday_index"]
        elif args.get("weekdayIndex") is not None:
            out["weekday_index"] = args["weekdayIndex"]
        if args.get("weekday_next") is not None:
            out["weekday_next"] = bool(args["weekday_next"])
        elif args.get("weekdayNext") is not None:
            out["weekday_next"] = bool(args["weekdayNext"])
    return out


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
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
        cat = str(item.get("category") or "other").strip().lower()
        if cat not in VALID_CATEGORIES:
            cat = "other"
        splittable = bool(item.get("splittable", False))
        min_blk = _coerce_int(item.get("min_block_minutes"))
        if min_blk is not None and (min_blk < 1 or min_blk > mins):
            min_blk = min(30, mins) if splittable else mins
        pref = str(item.get("preferred") or "any").strip().lower()
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

    if not isinstance(tool_name, str) or tool_name not in {"check_capacity", "plan_activities"}:
        raise ExtractionServiceError("聊天模型请求了不支持的工具。", 502, "model_invalid_chat")

    raw_args = first.get("arguments")
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ExtractionServiceError("聊天模型工具参数格式无效。", 502, "model_invalid_chat")

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
