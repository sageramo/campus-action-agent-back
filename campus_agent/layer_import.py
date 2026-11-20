"""Turn rule or model extraction into v3 events and tasks that can be used at once.

Complete items are adopted without a confirmation step; their evidence keeps
the source quote so the user can see where each time came from and edit it.
Anything missing stays null ("undefined") rather than being guessed.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from .candidates import validate_extraction_input
from .extractor import parse_notice_text
from .time_parser import CLOCK_RE, DATE_RE
from .time_utils import parse_dt

EVENT_LABELS = {"活动/比赛时间", "考试时间", "面试时间", "抽签/签到时间"}
WINDOW_LABELS = {"办理时间", "选课时间"}
SENTENCE_END = re.compile(r"[。；;！？!?\n]")
CLAUSE_END = re.compile(r"[，,。；;！？!?\n]")
MOVED_TO = re.compile(r"^(?:改为|改至|改到|调整为|调整至|更改为|变更为|移至|迁至|换到|换至)\s*")
LEADING = re.compile(r"^(?:请(?:于|在)?|于|在|(?:之|以)?前)+")
EVENT_VERBS = {"举行", "进行", "召开", "开始", "举办"}
LEADING_EVENT_VERB = re.compile(r"^(?:举行|举办|召开|开展)")
LOCATION_CLAUSE_PREFIX = re.compile(r"^(?:地点|地址|教室|会议室|办公室)[：:\s为在]")
PAREN_LOCATION_RE = re.compile(r"[（(]\s*(?:(?:地点|地址)[：:\s]*)?([^（）()，,。；;\n！？!?]{2,24})\s*[）)]\s*$")
VENUE_HINT_RE = re.compile(
    r"(?:楼|室|厅|馆|堂|场|操场|广场|学活|教学楼|实验楼|办公楼|宿舍|公寓|食堂|"
    r"会议|中心|校区|礼堂|剧场|阶梯|教室|机房|实验室|腾讯会议|钉钉|飞书|线上|"
    r"[一二三四五六七八九十\d]+教)"
)
NON_LOCATION_PAREN_RE = re.compile(r"待定|未定|暂定|参加|必到|自愿|选填|截止|提交|电子版|纸质版|PDF|含|约|周一|周二|周三|周四|周五|周六|周日|星期")
FIELD_MAP = {"title": "title", "deadline_at": "deadline_at", "fixed_start_at": "start_at", "fixed_end_at": "end_at",
             "location": "location", "materials": "materials"}


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _source(text: str) -> dict:
    return {"id": _id("s"), "kind": "pasted_notice", "text": text, "published_at": None}


def _event(source: dict, title: str, start: str | None, end: str | None, location: str | None, evidence: list, notes: list) -> dict:
    return {"id": _id("e"), "title": title[:200], "start_at": start, "end_at": end, "location": location, "status": "active",
            "source_id": source["id"], "evidence": evidence, "notes": list(dict.fromkeys(notes))[:100]}


def _task(source: dict, title: str, deadline: str | None, evidence: list, notes: list, *, earliest: str | None = None,
          location: str | None = None, materials: list | None = None) -> dict:
    return {"id": _id("t"), "title": title[:200], "deadline_at": deadline, "estimate_minutes": None, "splittable": True,
            "min_block_minutes": None, "earliest_start_at": earliest, "priority": None, "status": "active",
            "location": location, "materials": list(dict.fromkeys(materials or []))[:30], "source_id": source["id"],
            "evidence": evidence, "notes": list(dict.fromkeys(notes))[:100]}


def _iso(value: str | None) -> str | None:
    parsed = parse_dt(value) if value else None
    return parsed.isoformat() if parsed else None


def _sentence(text: str, start: int, end: int) -> str:
    left = max((match.end() for match in SENTENCE_END.finditer(text, 0, start)), default=0)
    right = SENTENCE_END.search(text, end)
    return text[left:right.start() if right else len(text)].strip()


def _clean_event_title(text: str, event: bool) -> str:
    if event:
        stripped = LEADING_EVENT_VERB.sub("", text).strip("：: ")
        if len(stripped) >= 2:
            return stripped
    return text


def _extract_paren_location(title: str) -> tuple[str, str | None]:
    match = PAREN_LOCATION_RE.search(title)
    if not match:
        return title, None
    candidate = match.group(1).strip()
    explicit = bool(re.match(r"^[（(]\s*(?:地点|地址)[：:\s]", match.group(0)))
    if not explicit and (NON_LOCATION_PAREN_RE.search(candidate) or not VENUE_HINT_RE.search(candidate)):
        return title, None
    cleaned = title[:match.start()].strip("：: ")
    if len(cleaned) < 2:
        return title, None
    return cleaned, candidate


def _adjacent_clause_title(text: str, start: int, end: int, event: bool) -> str | None:
    sent_left = max((m.end() for m in SENTENCE_END.finditer(text, 0, start)), default=0)
    sent_right_match = SENTENCE_END.search(text, end)
    sent_right = sent_right_match.start() if sent_right_match else len(text)
    sentence = text[sent_left:sent_right]
    rel_start, rel_end = start - sent_left, end - sent_left
    clauses = list(re.finditer(r"[^，,]+", sentence))
    idx = next((i for i, m in enumerate(clauses) if m.start() <= rel_start and rel_end <= m.end()), None)
    if idx is None:
        return None
    for step in (1, -1):
        cursor = idx + step
        while 0 <= cursor < len(clauses):
            piece = clauses[cursor].group().strip("：: ")
            if DATE_RE.search(piece) or CLOCK_RE.search(piece):
                break
            if piece and not LOCATION_CLAUSE_PREFIX.match(piece):
                candidate = _clean_event_title(LEADING.sub("", piece).strip("：: "), event)
                if len(candidate) >= 2:
                    return candidate
            cursor += step
    return None


def _title(text: str, start: int, end: int, event: bool) -> str:
    """'请于{时间}前提交课程报告' -> '提交课程报告'; '讲座于{时间}举行' -> '参加讲座'."""
    left = max((match.end() for match in CLAUSE_END.finditer(text, 0, start)), default=0)
    right_match = CLAUSE_END.search(text, end)
    before = re.sub(r"(?:于|在)$", "", LEADING.sub("", text[left:start].strip())).rstrip("：: ")
    after = LEADING.sub("", text[end:right_match.start() if right_match else len(text)].strip()).lstrip("：: ")
    if event and after in EVENT_VERBS and before:
        return f"参加{before}"
    if event and not before:
        after = _clean_event_title(after, event)
    title = f"{before}{after}".strip("：: ")
    if len(title) >= 2:
        return title
    return _adjacent_clause_title(text, start, end, event) or _sentence(text, start, end)


def items_from_rules(text: str, reference_date: str | None) -> dict[str, Any]:
    text, reference_date, timezone = validate_extraction_input({"text": text, "reference_date": reference_date})
    parsed = parse_notice_text(text, source="user input", reference_date=reference_date, timezone=timezone).to_dict()
    source = _source(text)
    events, tasks = [], []
    for point in parsed["time_points"]:
        sentence = _sentence(text, point["source_start"], point["source_end"]) or point["evidence"]
        evidence = [{"field": "title", "origin": "source_text", "source_id": source["id"], "quote": sentence}]
        notes = list(point["warnings"])
        start, end = _iso(point["value"]) if point["precision"] == "datetime" else None, _iso(point["end_value"])
        label = point["time_type"]
        title = _title(text, point["source_start"], point["source_end"], label in EVENT_LABELS)
        title, paren_location = _extract_paren_location(title)
        location = next((place for place in parsed["locations"] if place in sentence), None) or paren_location
        # A change notice says "地点改为学活305"; the place is what follows the verb (still a literal quote).
        if location:
            location = MOVED_TO.sub("", location) or location
        if start is None:
            notes.append(f"原文时间「{point['evidence']}」缺少具体日期或时刻，请补填。")
            tasks.append(_task(source, title, None, evidence, notes))
            continue
        quote = {"origin": "source_text", "source_id": source["id"], "quote": point["evidence"]}
        if label in EVENT_LABELS:
            fields = [{"field": "start_at", **quote}] + ([{"field": "end_at", **quote}] if end else [])
            if location:
                fields.append({"field": "location", "origin": "source_text", "source_id": source["id"], "quote": location})
            events.append(_event(source, title, start, end if point["kind"] == "interval" else None, location, evidence + fields, notes))
        elif label in WINDOW_LABELS and point["kind"] == "interval" and end:
            notes.append("这是办理窗口，可在开始与结束之间任选时间完成。")
            tasks.append(_task(source, title, end, evidence + [{"field": "earliest_start_at", **quote}, {"field": "deadline_at", **quote}],
                               notes, earliest=start))
        else:
            tasks.append(_task(source, title, end or start, evidence + [{"field": "deadline_at", **quote}], notes))
    if not events and not tasks:
        tasks.append(_task(source, (parsed["task_name"] or text.strip())[:200], None,
                           [], ["没有找到可计算的时间，截止未定义；有截止时请补填。"]))
    return {"source": source, "events": events, "tasks": tasks, "warnings": parsed["warnings"], "mode": "rules"}


def items_from_candidates(result: dict, text: str, reference_date: str | None = None) -> dict[str, Any]:
    source = _source(text)
    events, tasks = [], []
    for candidate in result["candidates"]:
        evidence = []
        for item in candidate["evidence"]:
            fields = ["earliest_start_at", "deadline_at"] if item["field"] == "visit_windows" else [FIELD_MAP.get(item["field"])]
            evidence.extend({"field": field, "origin": "source_text", "source_id": source["id"], "quote": item["quote"]}
                            for field in fields if field)
        notes = list(candidate["uncertainties"])
        if candidate["kind"] == "fixed":
            if candidate["materials"]:
                notes.append("需携带：" + "、".join(candidate["materials"]))
            events.append(_event(source, candidate["title"], candidate["fixed_start_at"], candidate["fixed_end_at"],
                                 candidate["location"], evidence, notes))
        elif candidate["kind"] == "visit_window" and candidate["visit_windows"]:
            window = candidate["visit_windows"][0]
            notes.append("这是办理窗口，可在开始与结束之间任选时间完成。")
            tasks.append(_task(source, candidate["title"], window["end"], evidence, notes, earliest=window["start"],
                               location=candidate["location"], materials=candidate["materials"]))
        else:
            tasks.append(_task(source, candidate["title"], candidate["deadline_at"], evidence, notes,
                               location=candidate["location"], materials=candidate["materials"]))
    return {"source": source, "events": events, "tasks": tasks, "warnings": list(result["warnings"]), "mode": "model"}
