"""Personal reminders typed into 研墨: "提醒我今天要完成实验报告", "明天下午3点去取快递".

研墨 is the agent's single entry. Besides notices, people write one-line things for themselves.
Those sentences are recognised here, turned into 个人任务 (tasks marked with the note "个人任务")
or, when a clock time is given without "前", a self-set 约定 starting then; the rest of the text
still goes to the notice extractor. Relative days are read against the reference date (or today,
Beijing time). Anything not stated stays null, as everywhere else.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .time_utils import get_zone

PERSONAL_NOTE = "个人任务"
SPLIT = re.compile(r"[\n。；;！!？?]+")
MARKER = re.compile(r"提醒我|帮我记(?:住|一下)?|记得|别忘了|我(?:今天|明天|后天|今晚|明早|明晚|这周|周末)?(?:要|得|需要|想要?)|(?:今天|明天|后天|今晚|明早|明晚)(?:要|得|需要)|待办[:：]?")
RELATIVE = {"今天": 0, "今日": 0, "今晚": 0, "今早": 0, "明天": 1, "明早": 1, "明晚": 1, "后天": 2, "大后天": 3}
WEEKDAYS = "一二三四五六日天"
DATE = re.compile(r"(?P<rel>大后天|后天|明天|明早|明晚|今天|今日|今晚|今早)"
                  r"|(?P<next>下个?)?(?:周|星期|礼拜)(?P<wd>[一二三四五六日天])"
                  r"|(?:(?P<m>\d{1,2})月(?P<d>\d{1,2})[日号])"
                  r"|(?P<dd>\d{1,2})[号日](?!\d)")
CLOCK = re.compile(r"(?P<part>凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|明晚)?\s*(?P<h>\d{1,2})(?:[:：](?P<mm>\d{2})|点(?:(?P<half>半)|(?P<pm>\d{1,2})分?)?|时)")
BEFORE = re.compile(r"(?:之|以)?前|截止")
DURATION = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*(?P<u>分钟|小时|个小时|h)")
GOING = re.compile(r"去|参加|开会|见|上课|面试|看|取|接|约")
NOISE = re.compile(r"提醒我|帮我记(?:住|一下)?|记得|别忘了|待办[:：]?|^(?:我|要|得|需要|想要?|请)+|(?:之|以)?前|截止|一下$|(?:大概|大约|约|预计)$|[，,](?:大概|大约|约|预计)?$")
# A notice reads "11月20日19:00 学术讲座"; a note to oneself reads "明天下午3点去取快递" or "周五前交报告".
NOTICEY = re.compile(r"【|】|通知|同学们|各位|各班|请于|请各|请在|报名方式|联系人")
COLLOQUIAL = re.compile(r"大后天|后天|明天|明早|明晚|今天|今日|今晚|今早|(?:下个?)?(?:周|星期|礼拜)[一二三四五六日天]")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _today(reference_date: str | None) -> date:
    if reference_date:
        return date.fromisoformat(reference_date)
    return datetime.now(get_zone()).date()


def _day(sentence: str, today: date) -> tuple[date | None, tuple[int, int] | None]:
    match = DATE.search(sentence)
    if not match:
        return None, None
    if match["rel"]:
        return today + timedelta(days=RELATIVE[match["rel"]]), match.span()
    if match["wd"]:
        target = WEEKDAYS.index(match["wd"]) if match["wd"] != "天" else 6
        delta = (target - today.weekday()) % 7
        if match["next"]:
            delta = 7 - today.weekday() + target
        return today + timedelta(days=delta), match.span()
    if match["m"]:
        month, day = int(match["m"]), int(match["d"])
        try:
            found = date(today.year, month, day)
        except ValueError:
            return None, None
        if found < today - timedelta(days=60):
            try:
                found = date(today.year + 1, month, day)
            except ValueError:
                return None, None
        return found, match.span()
    try:
        found = date(today.year, today.month, int(match["dd"]))
    except ValueError:
        return None, None
    return found, match.span()


def _clock(sentence: str) -> tuple[int, int, tuple[int, int]] | None:
    match = CLOCK.search(sentence)
    if not match:
        return None
    hour = int(match["h"])
    minute = int(match["mm"] or match["pm"] or 0) + (30 if match["half"] else 0)
    part = match["part"] or ""
    if part in {"下午", "傍晚", "晚上", "今晚", "明晚"} and hour < 12:
        hour += 12
    elif part == "中午" and hour < 6:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute, match.span()


def _at(day: date, hour: int, minute: int) -> str:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=get_zone()).isoformat()


def _title(sentence: str, spans: list[tuple[int, int]]) -> str:
    text = sentence
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    text = DURATION.sub("", text)
    text = re.sub(r"[，,]?\s*(?:大概|大约|约|预计)\s*$", "", text.strip())
    for _ in range(3):
        text = NOISE.sub("", text.strip(" ，,、：:"))
    text = re.sub(r"^(?:今天|明天|后天|今晚|这周|周末|上午|下午|晚上|在|把)+", "", text).strip(" ，,、：:")
    return text[:200] or sentence.strip()[:200]


def personal_item(sentence: str, today: date, source_id: str, lone: bool = False) -> tuple[str, dict[str, Any]] | None:
    """('task' | 'event', item) for one personal sentence, or None if it is not one.

    lone: the whole input is this one short sentence, so a colloquial day ("明天", "周五") is enough.
    """
    explicit = MARKER.match(sentence)
    if not explicit and (NOTICEY.search(sentence) or not lone or not COLLOQUIAL.match(sentence)):
        return None
    if not explicit and re.search(r"吗$|呢$|怎么办|能不能|要不要|有空|好累|开心|难过", sentence):
        return None
    day, day_span = _day(sentence, today)
    clock = _clock(sentence)
    before = bool(BEFORE.search(sentence))
    spans = [span for span in (day_span, clock[2] if clock else None) if span]
    if len(spans) == 2 and spans[0][0] < spans[1][1] and spans[1][0] < spans[0][1]:
        spans = [(min(spans[0][0], spans[1][0]), max(spans[0][1], spans[1][1]))]
    title = _title(sentence, spans)
    duration = DURATION.search(sentence)
    minutes = None
    if duration:
        value = float(duration["n"]) * (60 if duration["u"] != "分钟" else 1)
        minutes = int(value) if 5 <= value <= 24 * 60 else None
    evidence = [{"field": "title", "origin": "source_text", "source_id": source_id, "quote": sentence}]
    notes = [PERSONAL_NOTE]
    if clock and not before and GOING.search(sentence):
        start_day = day or today
        event = {"id": _id("e"), "kind": "appointment", "title": title, "start_at": _at(start_day, clock[0], clock[1]),
                 "end_at": None, "location": None, "status": "active",
                 "source_id": source_id, "evidence": evidence, "notes": notes}
        return "event", event
    deadline = earliest = None
    if day or clock:
        target = day or today
        deadline = _at(target, clock[0], clock[1]) if clock else _at(target, 23, 59)
        if not before:
            earliest = _at(target, 0, 0)  # "今天要做" is pinned to that day, not just due by it
    else:
        notes.append("没说哪天，先记作待书；可以在笔墨里选一天。")
    task = {"id": _id("t"), "title": title, "deadline_at": deadline, "estimate_minutes": minutes, "splittable": True,
            "min_block_minutes": None, "earliest_start_at": earliest, "priority": None, "status": "active", "location": None,
            "materials": [], "source_id": source_id, "evidence": evidence, "notes": notes}
    return "task", task


def split_personal(text: str, reference_date: str | None, source_id: str) -> tuple[list[dict], list[dict], str]:
    """Personal events and tasks, plus the text left for the notice extractor."""
    today = _today(reference_date)
    events, tasks, rest = [], [], []
    sentences = [sentence.strip() for sentence in SPLIT.split(text) if sentence.strip()]
    lone = len(sentences) == 1 and len(sentences[0]) <= 50
    for sentence in sentences:
        found = personal_item(sentence, today, source_id, lone)
        if found is None:
            rest.append(sentence)
        elif found[0] == "event":
            events.append(found[1])
        else:
            tasks.append(found[1])
    return events, tasks, "。".join(rest).strip()


def with_personal(text: str, reference_date: str | None, extract) -> dict[str, Any]:
    """Run the notice extractor only on what is not personal, and add the personal items to its result."""
    if not isinstance(text, str):
        raise ValueError("text 需为文本")
    source = {"id": _id("s"), "kind": "pasted_notice", "text": text, "published_at": None}
    events, tasks, rest = split_personal(text, reference_date, source["id"])
    if not events and not tasks:
        return extract(text)
    result = extract(rest) if rest else {"events": [], "tasks": [], "warnings": [], "mode": "rules"}
    for item in [*result["events"], *result["tasks"]]:
        item["source_id"] = source["id"]
        for quote in item.get("evidence", []):
            quote["source_id"] = source["id"]
    result["source"] = source
    result["events"] = [*result["events"], *events]
    result["tasks"] = [*result["tasks"], *tasks]
    return result
