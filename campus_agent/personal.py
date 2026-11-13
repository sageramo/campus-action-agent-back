"""Personal reminders typed into 研墨: "提醒我今天要完成实验报告", "明天下午3点去取快递".

研墨 is the agent's single entry. Besides notices, people write one-line things for themselves.
Those sentences are recognised here, turned into 个人任务 (tasks marked with the note "个人任务")
or, when a clock time is given without "前", a self-set 约定 starting then; the rest of the text
still goes to the notice extractor. Relative days are read against the reference date (or today,
Beijing time). Anything not stated stays null, as everywhere else.
"""
from __future__ import annotations

import re
from math import ceil
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .time_parser import CLOCK_RE, CN, RANGE_RE, clock_value, cn_number
from .time_utils import get_zone

PERSONAL_NOTE = "个人任务"
SPLIT = re.compile(r"[\n。；;！!？?]+")
MARKER = re.compile(r"提醒我|帮我记(?:住|一下)?|记得|别忘了|我(?:今天|明天|后天|今晚|明早|明晚|这周|周末)?(?:要|得|需要|想要?)|(?:今天|明天|后天|今晚|明早|明晚)(?:要|得|需要)|待办[:：]?")
RELATIVE = {"今天": 0, "今日": 0, "今晚": 0, "今早": 0, "明天": 1, "明早": 1, "明晚": 1, "后天": 2, "大后天": 3}
WEEKDAYS = "一二三四五六日天"
DATE = re.compile(r"(?P<rel>大后天|后天|明天|明早|明晚|今天|今日|今晚|今早)"
                  r"|(?P<next>下个?|本|这)?(?:周|星期|礼拜)(?P<wd>[一二三四五六日天])"
                  r"|(?<!\d)(?P<iy>\d{4})-(?P<im>\d{1,2})-(?P<id>\d{1,2})(?!\d)"
                  r"|(?:(?:(?P<y>\d{4})年\s*)?(?P<m>\d{1,2})月(?P<d>\d{1,2})[日号])"
                  r"|(?<![\d年月])(?P<dd>\d{1,2})[号日](?![\d楼室馆门线房])")
BEFORE = re.compile(r"\s*(?:(?:之|以)?前(?!端|台|往|进|期|提|几)|截止|为止)")
NUMBER = rf"(?:\d+(?:\.\d+)?|[{CN}]{{1,3}})"
DURATION = re.compile(
    rf"(?<![\d.点:：-])(?:半\s*(?:个\s*)?小时"
    rf"|(?P<hours>{NUMBER})\s*(?:个\s*(?P<leading_half>半\s*)?)?(?:小时|h(?![a-z]))"
    rf"(?:\s*(?P<extra>{NUMBER})\s*分钟|\s*(?P<half>半))?"
    rf"|(?P<minutes>{NUMBER})\s*分钟)", re.I,
)
RECURRING = re.compile(r"每(?:天|日|周|晚|早|个?星期|个月)|工作日|隔[天周]")
GOING = re.compile(r"去|参加|开会|例会|组会|开.{0,12}会|见|上课|面试|赴约|取快递")
NOISE = re.compile(r"^(?:提醒我|帮我记(?:住|一下)?|记得|别忘了|待办|我(?:要|得|需要|想要?)?|要|得|需要|请|截止)+|[，,]?\s*(?:大概|大约|约|预计|用时)$")
# A notice reads "11月20日19:00 学术讲座"; a note to oneself reads "明天下午3点去取快递" or "周五前交报告".
NOTICEY = re.compile(r"【|】|通知|同学们|各位|各班|请于|请各|请在|报名方式|联系人")
COLLOQUIAL = re.compile(r"(?:(?:本|这)?周[一二三四五六日天])|大后天|后天|明天|明早|明晚|今天|今日|今晚|今早|(?:下个?)?(?:周|星期|礼拜)[一二三四五六日天]")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _today(reference_date: str | None) -> date:
    if reference_date:
        return date.fromisoformat(reference_date)
    return datetime.now(get_zone()).date()


def _day(match: re.Match[str], today: date) -> date:
    if match["rel"]:
        return today + timedelta(days=RELATIVE[match["rel"]])
    if match["wd"]:
        target = WEEKDAYS.index(match["wd"]) if match["wd"] != "天" else 6
        delta = target - today.weekday()
        if match["next"] in {"本", "这"}:
            return today + timedelta(days=delta)
        return today + timedelta(days=delta + 7 if match["next"] else delta % 7)
    if match["iy"]:
        return date(int(match["iy"]), int(match["im"]), int(match["id"]))
    if match["m"]:
        month, day = int(match["m"]), int(match["d"])
        found = date(int(match["y"] or today.year), month, day)
        if not match["y"] and found < today - timedelta(days=60):
            found = date(today.year + 1, month, day)
        return found
    return date(today.year, today.month, int(match["dd"]))


def _at(day: date, clock: tuple[int, int, int]) -> datetime:
    hour, minute, second = clock
    return datetime.combine(day, datetime.min.time(), get_zone()) + timedelta(hours=hour, minutes=minute, seconds=second)


def _minutes(match: re.Match[str]) -> int | None:
    def number(value: str) -> float:
        return float(value) if value[0].isdigit() else cn_number(value)

    if match["hours"]:
        extra = number(match["extra"]) if match["extra"] else 30 if match["half"] or match["leading_half"] else 0
        value = number(match["hours"]) * 60 + extra
    else:
        value = number(match["minutes"]) if match["minutes"] else 30
    return ceil(value) if 0 < value <= 20160 else None


def _title(sentence: str, spans: list[tuple[int, int]]) -> str:
    pieces = []
    cursor = 0
    for start, end in sorted(spans):
        if start > cursor:
            pieces.append(sentence[cursor:start])
        cursor = max(cursor, end)
    text = "".join([*pieces, sentence[cursor:]])
    for _ in range(3):
        text = NOISE.sub("", text.strip(" ，,、：:"))
    text = re.sub(r"^(?:今天|明天|后天|今晚|这周|周末|上午|下午|晚上|在|把)+", "", text).strip(" ，,、：:")
    return text[:200]


def personal_item(sentence: str, today: date, source_id: str, lone: bool = False) -> tuple[str, dict[str, Any]] | None:
    """('task' | 'event', item) for one personal sentence, or None if it is not one.

    lone: the whole input is this one short sentence, so a colloquial day ("明天", "周五") is enough.
    """
    explicit = MARKER.match(sentence)
    if not explicit and lone and RECURRING.match(sentence) and GOING.search(sentence):
        raise ValueError("固定课表或例会请在铺纸里添加。")
    if not explicit and (NOTICEY.search(sentence) or not lone or not COLLOQUIAL.match(sentence)):
        return None
    if not explicit and re.search(r"吗$|呢$|怎么办|能不能|要不要|有空|好累|开心|难过|下雨|下雪", sentence):
        return None
    dates = list(DATE.finditer(sentence))
    durations = list(DURATION.finditer(sentence))
    # Blank dates and durations before matching clocks, retaining the original offsets.
    spans = [match.span() for match in [*dates, *durations]]
    clock_text = sentence
    for start, end in sorted(spans, reverse=True):
        clock_text = clock_text[:start] + " " * (end - start) + clock_text[end:]
    clocks = list(CLOCK_RE.finditer(clock_text))
    spans.extend((match.start() + len(match[0]) - len(match[0].lstrip()), match.end()) for match in clocks)
    connector = RANGE_RE.fullmatch(clock_text[clocks[0].end():clocks[1].start()]) if len(clocks) == 2 else None
    if connector:
        spans.append((clocks[0].end(), clocks[1].start()))
    end_of_time = max((match.end() for match in [*dates, *clocks]), default=0)
    deadline_word = BEFORE.match(sentence, end_of_time) if end_of_time else None
    before = bool(deadline_word or "截止" in sentence)
    if deadline_word:
        spans.append(deadline_word.span())
    title = _title(sentence, spans)
    if not title:
        raise ValueError("请补上要记录的事项名称。")
    if RECURRING.search(sentence):
        raise ValueError("这句话包含重复安排。固定课表或例会请在铺纸里添加。")
    minutes = None
    if durations:
        try:
            minutes = _minutes(durations[0]) if len(durations) == 1 else None
        except (ValueError, KeyError):
            pass
    evidence = [{"field": "title", "origin": "source_text", "source_id": source_id, "quote": sentence[:3000]}]
    notes = [PERSONAL_NOTE]
    if durations and minutes is None:
        notes.append("预计用时有歧义或超出范围，请核对原文后补填。")

    is_event = not before and (connector is not None or (bool(clocks) and GOING.search(title)))
    start = end = None
    try:
        if len(dates) > 1 and (len(dates) > 2 or not connector or dates[1].end() > clocks[1].end()):
            raise ValueError("出现多个日期")
        if len(clocks) > 1 and not connector:
            raise ValueError("出现多个时刻")
        day = _day(dates[0], today) if dates else None
        if clocks:
            relative = dates[0]["rel"] if dates else None
            period = {"今晚": "晚上", "明晚": "晚上", "明早": "早上", "今早": "早上"}.get(relative)
            start = _at(day or today, clock_value(clocks[0], period))
            if connector:
                next_day = bool(connector["next"] or connector["next_after"])
                end_day = _day(dates[1], today) if len(dates) == 2 else (day or today) + timedelta(days=next_day)
                end = _at(end_day, clock_value(clocks[1], clocks[0]["period"] or period))
                if end <= start:
                    raise ValueError("结束须晚于开始，跨午夜请写明次日")
            elif is_event and minutes:
                end = start + timedelta(minutes=minutes)
    except (ValueError, KeyError, OverflowError) as error:
        day = start = end = None
        notes.append(f"时间待书，请核对原文。{error}")

    if is_event:
        event = {"id": _id("e"), "kind": "appointment", "title": title,
                 "start_at": start.isoformat() if start else None, "end_at": end.isoformat() if end else None,
                 "location": None, "status": "active", "source_id": source_id, "evidence": evidence, "notes": notes}
        return "event", event
    deadline = end or start
    if deadline is None and day:
        deadline = _at(day, (23, 59, 0))
    earliest = None
    if deadline and not before:
        start_of_day = _at(day or today, (0, 0, 0))
        if start_of_day < deadline:
            earliest = start_of_day.isoformat()
    if deadline is None and len(notes) == 1:
        notes.append("没说哪天，先记作待书；可以在笔墨里选一天。")
    task = {"id": _id("t"), "title": title, "deadline_at": deadline.isoformat() if deadline else None, "estimate_minutes": minutes, "splittable": True,
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
    source = {"id": _id("s"), "kind": "pasted_notice", "text": text, "published_at": None}
    events, tasks, rest = split_personal(text, reference_date, source["id"])
    if not events and not tasks:
        return extract(text)
    result = extract(rest) if rest else {"events": [], "tasks": [], "warnings": [], "mode": "rules"}
    for item in [*result["events"], *result["tasks"]]:
        item["source_id"] = source["id"]
        for quote in item.get("evidence", []):
            quote["source_id"] = source["id"]
    result["warnings"] = list(result["warnings"]) + [
        f"「{item['title']}」{note}" for item in [*events, *tasks] for note in item["notes"]
        if note.startswith(("时间待书", "预计用时有歧义"))
    ]
    result["source"] = source
    result["events"] = [*result["events"], *events]
    result["tasks"] = [*result["tasks"], *tasks]
    return result
