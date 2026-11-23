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
COMMA_SPLIT = re.compile(r"[，,]+")
PERIOD_RE = re.compile(r"(清晨|早晨|早上|上午|中午|下午|傍晚|晚上|晚间|夜间)")
PERIOD_START_ANCHOR: dict[str, tuple[int, int, int]] = {
    "清晨": (6, 0, 0),
    "早晨": (7, 0, 0),
    "早上": (8, 0, 0),
    "上午": (8, 0, 0),
    "中午": (12, 0, 0),
    "下午": (13, 0, 0),
    "傍晚": (17, 0, 0),
    "晚上": (18, 0, 0),
    "晚间": (18, 0, 0),
    "夜间": (19, 0, 0),
}
PERIOD_END_ANCHOR: dict[str, tuple[int, int, int]] = {
    "清晨": (9, 0, 0),
    "早晨": (10, 0, 0),
    "早上": (10, 0, 0),
    "上午": (12, 0, 0),
    "中午": (14, 0, 0),
    "下午": (18, 0, 0),
    "傍晚": (20, 0, 0),
    "晚上": (22, 0, 0),
    "晚间": (22, 0, 0),
    "夜间": (23, 0, 0),
}
PERIOD_TO_CLOCK_RE = re.compile(r"(清晨|早晨|早上|上午|中午|下午|傍晚|晚上|晚间|夜间)\s*(?:到|至|-|—|–|~|～)\s*$")
CLOCK_TO_PERIOD_RE = re.compile(r"^\s*(?:到|至|-|—|–|~|～)\s*(清晨|早晨|早上|上午|中午|下午|傍晚|晚上|晚间|夜间)")
PERIOD_TO_PERIOD_RE = re.compile(r"(清晨|早晨|早上|上午|中午|下午|傍晚|晚上|晚间|夜间)\s*(?:到|至|-|—|–|~|～)\s*(清晨|早晨|早上|上午|中午|下午|傍晚|晚上|晚间|夜间)")
PERIOD_WINDOWS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], str]] = {
    "清晨": ((6, 0, 0), (9, 0, 0), "清晨"),
    "早晨": ((6, 0, 0), (10, 0, 0), "早上"),
    "早上": ((6, 0, 0), (10, 0, 0), "早上"),
    "今早": ((6, 0, 0), (10, 0, 0), "早上"),
    "明早": ((6, 0, 0), (10, 0, 0), "早上"),
    "上午": ((8, 0, 0), (12, 0, 0), "上午"),
    "中午": ((11, 30, 0), (14, 0, 0), "中午"),
    "下午": ((13, 0, 0), (18, 0, 0), "下午"),
    "傍晚": ((17, 0, 0), (20, 0, 0), "傍晚"),
    "晚上": ((18, 0, 0), (23, 0, 0), "晚上"),
    "晚间": ((18, 0, 0), (23, 0, 0), "晚上"),
    "夜间": ((18, 0, 0), (23, 0, 0), "晚上"),
    "今晚": ((18, 0, 0), (23, 0, 0), "晚上"),
    "明晚": ((18, 0, 0), (23, 0, 0), "晚上"),
}
CLAUSE_LEAD = re.compile(r"^(?:然后|再|还有|另外|并且|以及|接着|随后)?\s*")
MARKER = re.compile(r"提醒我|帮我记(?:住|一下)?|记得|别忘了|我(?:今天|明天|后天|今晚|明早|明晚|这周|本周|下周|(?:这|本|下个?)?周末)?(?:要|得|需要|想要?)|(?:今天|明天|后天|今晚|明早|明晚|(?:这|本|下个?)?周末)(?:要|得|需要)|待办[:：]?")
RELATIVE = {"今天": 0, "今日": 0, "今晚": 0, "今早": 0, "明天": 1, "明早": 1, "明晚": 1, "后天": 2, "大后天": 3}
WEEKDAYS = "一二三四五六日天"
DATE = re.compile(r"(?P<rel>大后天|后天|明天|明早|明晚|今天|今日|今晚|今早)"
                  r"|(?P<weekend>(?:下个?|本|这)?周末)"
                  r"|(?P<next>下个?|本|这)?(?:周|星期|礼拜)(?P<wd>[一二三四五六日天])"
                  r"|(?<!\d)(?P<iy>\d{4})-(?P<im>\d{1,2})-(?P<id>\d{1,2})(?!\d)"
                  r"|(?:(?:(?P<y>\d{4})年\s*)?(?P<m>\d{1,2})月(?P<d>\d{1,2})[日号])"
                  r"|(?<![\d年月])(?P<dd>\d{1,2})[号日](?![\d楼室馆门线房])")
BEFORE = re.compile(
    r"\s*(?:(?:之|以)?前(?!端|台|往|进|期|几|提(?![交供出醒报]))|"
    r"(?:之|以)?内(?!容|部|涵|地|外|存|核|嵌|置|向|衣|饰|科|脏)|截止|为止)"
)
NUMBER = rf"(?:\d+(?:\.\d+)?|[{CN}]{{1,3}})"
DURATION = re.compile(
    rf"(?<![\d.点:：-])(?:半\s*(?:个\s*)?小时"
    rf"|(?P<hours>{NUMBER})\s*(?:个\s*(?P<leading_half>半\s*)?)?(?:小时|h(?![a-z]))"
    rf"(?:\s*(?P<extra>{NUMBER})\s*分钟|\s*(?P<half>半))?"
    rf"|(?P<minutes>{NUMBER})\s*分钟)", re.I,
)
RECURRING = re.compile(r"每(?:天|日|周|晚|早|个?星期|个月)|工作日|隔[天周]")
GOING = re.compile(
    r"去|参加|开会|例会|组会|开.{0,12}会|见|上课|面试|答辩|路演|赴约|取快递|"
    r"彩排|排练|表演|演出|比赛|考试|测验|讲座|宣讲|汇报|培训|体检|值班|志愿|"
    r"看电影|观影|吃饭|聚餐|约饭|打球|健身|集合|签到"
)
NOISE = re.compile(r"^(?:提醒我|帮我记(?:住|一下)?|记得|别忘了|待办|我(?:要|得|需要|想要?)?|要|得|需要|请|截止)+|[，,]?\s*(?:大概|大约|约|预计|用时)$")
# A notice reads "11月20日19:00 学术讲座"; a note to oneself reads "明天下午3点去取快递" or "周五前交报告".
NOTICEY = re.compile(r"【|】|通知|同学们|各位|各班|请于|请各|请在|报名方式|联系人")
COLLOQUIAL = re.compile(
    r"(?:我\s*)?(?:(?:(?:下个?|本|这)?周末)|(?:(?:下个?|本|这)?(?:周|星期|礼拜)[一二三四五六日天])|"
    r"大后天|后天|明天|明早|明晚|今天|今日|今晚|今早)|"
    r"我\s*(?:\d{1,2}月\d{1,2}[日号]|\d{1,2}[号日])"
)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _today(reference_date: str | None) -> date:
    if reference_date:
        return date.fromisoformat(reference_date)
    return datetime.now(get_zone()).date()


def _weekend_range(label: str, today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    offset = 7 if "下" in label else 0
    sat = monday + timedelta(days=5 + offset)
    sun = sat + timedelta(days=1)
    start = sat if offset else max(today, sat)
    return start, sun


def _day(match: re.Match[str], today: date) -> date:
    if match["rel"]:
        return today + timedelta(days=RELATIVE[match["rel"]])
    if match["weekend"]:
        return _weekend_range(match["weekend"], today)[1]
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
    for _ in range(4):
        text = NOISE.sub("", text.strip(" ，,、：:"))
        text = re.sub(r"^(?:然后|接着|随后|今天|明天|后天|今晚|今早|明早|明晚|这周|本周|下周|(?:下个?|本|这)?周末|清晨|早晨|早上|上午|中午|下午|傍晚|晚上|晚间|夜间|在|把)+", "", text).strip(" ，,、：:")
    return text[:200]


def _starts_timed_clause(clause: str) -> bool:
    """True when a comma-separated clause introduces its own date, day-period, or clock."""
    trimmed = CLAUSE_LEAD.sub("", clause.strip())
    if not trimmed:
        return False
    if DATE.match(trimmed) or PERIOD_RE.match(trimmed) or MARKER.match(trimmed):
        return True
    # Avoid splitting on durations like "1小时30分钟"
    if DURATION.match(trimmed):
        return False
    return CLOCK_RE.match(trimmed) is not None


def _split_comma_clauses(sentence: str) -> list[str]:
    matches = list(COMMA_SPLIT.finditer(sentence))
    if not matches:
        return [sentence]
    # Slice exact character ranges so every clause remains a verbatim substring of `sentence` (for evidence checks).
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for m in matches:
        seg = sentence[cursor:m.start()].strip()
        if seg:
            s = sentence.index(seg, cursor)
            bounds.append((s, s + len(seg)))
        cursor = m.end()
    tail = sentence[cursor:].strip()
    if tail:
        s = sentence.index(tail, cursor)
        bounds.append((s, s + len(tail)))
    if len(bounds) <= 1:
        return [sentence]
    merged: list[tuple[int, int]] = [bounds[0]]
    for s, e in bounds[1:]:
        if _starts_timed_clause(sentence[s:e]):
            merged.append((s, e))
        else:
            prev_s, _ = merged[-1]
            merged[-1] = (prev_s, e)
    return [sentence[s:e] for s, e in merged]


def _item_day(item: dict[str, Any]) -> date | None:
    for key in ("start_at", "earliest_start_at", "deadline_at"):
        val = item.get(key)
        if isinstance(val, str) and len(val) >= 10:
            try:
                return date.fromisoformat(val[:10])
            except ValueError:
                pass
    return None


def personal_item(
    sentence: str,
    today: date,
    source_id: str,
    lone: bool = False,
    *,
    inherited_day: date | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """('task' | 'event', item) for one personal sentence, or None if it is not one.

    lone: the whole input is this one short sentence, so a colloquial day ("明天", "周五") is enough.
    inherited_day: date carried forward from an earlier comma-separated clause (e.g. "这周四上午要彩排，下午要表演").
    """
    explicit = MARKER.match(sentence)
    if not explicit and lone and RECURRING.match(sentence) and GOING.search(sentence):
        raise ValueError("固定课表或例会请在铺纸里添加。")
    if not explicit and inherited_day is None and (NOTICEY.search(sentence) or not lone or not COLLOQUIAL.match(sentence)):
        return None
    if not explicit and re.search(
        r"吗$|呢$|[？?]$|怎么办|能不能|要不要|有没有|可不可以|是否|有空|多少空|剩多少|"
        r"好累|开心|难过|下雨|下雪|(?:状态|精力|精神)(?:不|很差|差|一般|偏低|不足|不够|不错|很好|挺好|充沛)|不舒服|头[疼痛晕]",
        sentence,
    ):
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

    # Recognize period-anchored ranges ("中午到晚上五点", "下午3点到晚上", "上午到下午") and standalone day-periods.
    period_range_info: tuple[str, tuple[int, int, int] | None, tuple[int, int, int] | None, str] | None = None
    period_match = None
    period_key: str | None = None
    if len(clocks) == 1:
        p2c = PERIOD_TO_CLOCK_RE.search(clock_text[:clocks[0].start()])
        c2p = CLOCK_TO_PERIOD_RE.match(clock_text[clocks[0].end():]) if not p2c else None
        if p2c:
            p_word = p2c.group(1)
            spans.append((p2c.start(), clocks[0].start()))
            period_range_info = ("p2c", PERIOD_START_ANCHOR[p_word], None, p_word)
        elif c2p:
            p_word = c2p.group(1)
            spans.append((clocks[0].end(), clocks[0].end() + c2p.end()))
            period_range_info = ("c2p", None, PERIOD_END_ANCHOR[p_word], p_word)
    elif not clocks:
        p2p = PERIOD_TO_PERIOD_RE.search(clock_text)
        if p2p:
            p1, p2 = p2p.group(1), p2p.group(2)
            if PERIOD_START_ANCHOR[p1] < PERIOD_END_ANCHOR[p2]:
                spans.append(p2p.span())
                period_range_info = ("p2p", PERIOD_START_ANCHOR[p1], PERIOD_END_ANCHOR[p2], f"{p1}到{p2}")
        if not period_range_info:
            period_match = PERIOD_RE.search(clock_text)
            if period_match:
                period_key = period_match.group(1)
                spans.append(period_match.span())
            elif len(dates) == 1 and dates[0]["rel"] in PERIOD_WINDOWS:
                period_key = dates[0]["rel"]

    end_of_time = max(
        (match.end() for match in [*dates, *clocks, *([period_match] if period_match else [])]),
        default=0,
    )
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

    is_event = not before and (connector is not None or period_range_info is not None or (bool(clocks) and GOING.search(title)))
    start = end = None
    day: date | None = None
    try:
        if len(dates) > 1 and (len(dates) > 2 or not connector or dates[1].end() > clocks[1].end()):
            raise ValueError("出现多个日期")
        if len(clocks) > 1 and not connector:
            raise ValueError("出现多个时刻")
        day = _day(dates[0], today) if dates else inherited_day
        if period_range_info is not None:
            r_kind, p_start_c, p_end_c, p_label = period_range_info
            base_day = day or today
            relative = dates[0]["rel"] if dates else None
            rel_period = {"今晚": "晚上", "明晚": "晚上", "明早": "早上", "今早": "早上"}.get(relative)
            if r_kind == "p2c":
                assert p_start_c is not None
                start = _at(base_day, p_start_c)
                end = _at(base_day, clock_value(clocks[0], rel_period))
                if end <= start:
                    raise ValueError("结束须晚于开始，跨午夜请写明次日")
                notes.append(f"开始时刻按「{p_label}」默认 {p_start_c[0]:02d}:{p_start_c[1]:02d} 计，可在笔墨微调。")
            elif r_kind == "c2p":
                assert p_end_c is not None
                start = _at(base_day, clock_value(clocks[0], rel_period))
                end_c = (12, 0, 0) if p_label == "中午" and start < _at(base_day, (12, 0, 0)) else p_end_c
                end = _at(base_day, end_c)
                if end <= start:
                    raise ValueError("结束须晚于开始，跨午夜请写明次日")
                notes.append(f"结束时刻按「{p_label}」默认 {end_c[0]:02d}:{end_c[1]:02d} 计，可在笔墨微调。")
            else:
                assert p_start_c is not None and p_end_c is not None
                start = _at(base_day, p_start_c)
                end = _at(base_day, p_end_c)
                notes.append(
                    f"起止按「{p_label}」默认 {p_start_c[0]:02d}:{p_start_c[1]:02d}–{p_end_c[0]:02d}:{p_end_c[1]:02d} 计，可在笔墨微调。"
                )
        elif clocks:
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
    earliest = None
    if deadline is None and day:
        if period_key and period_key in PERIOD_WINDOWS:
            p_start, p_end, p_label = PERIOD_WINDOWS[period_key]
            deadline = _at(day, p_end)
            if not before:
                earliest = _at(day, p_start).isoformat()
            notes.append(
                f"按{p_label}时段（{p_start[0]:02d}:{p_start[1]:02d}–{p_end[0]:02d}:{p_end[1]:02d}）限定，可在笔墨修改具体时刻。"
            )
        else:
            deadline = _at(day, (23, 59, 0))
    if deadline and not before and earliest is None:
        if len(dates) == 1 and dates[0]["weekend"]:
            wk_start, _ = _weekend_range(dates[0]["weekend"], today)
            start_of_day = _at(wk_start, (0, 0, 0))
        else:
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
    events, tasks = [], []
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for match in SPLIT.finditer(text):
        segment = text[cursor:match.start()].strip()
        if segment:
            start = text.index(segment, cursor)
            spans.append((start, start + len(segment), segment))
        cursor = match.end()
    tail = text[cursor:].strip()
    if tail:
        start = text.index(tail, cursor)
        spans.append((start, start + len(tail), tail))
    lone = len(spans) == 1 and len(spans[0][2]) <= 80
    kept: list[tuple[int, int, str, int]] = []
    for index, (start, end, sentence) in enumerate(spans):
        sub_clauses = _split_comma_clauses(sentence)
        if len(sub_clauses) > 1 and not NOTICEY.search(sentence):
            first = personal_item(sub_clauses[0], today, source_id, lone)
            if first is not None:
                kind0, item0 = first
                (events if kind0 == "event" else tasks).append(item0)
                carried_day = _item_day(item0)
                for sub in sub_clauses[1:]:
                    sub_found = personal_item(sub, today, source_id, lone, inherited_day=carried_day)
                    if sub_found is not None:
                        sk, si = sub_found
                        (events if sk == "event" else tasks).append(si)
                        carried_day = _item_day(si) or carried_day
                    else:
                        kept.append((start, end, sub, index))
                continue
        found = personal_item(sentence, today, source_id, lone)
        if found is None:
            kept.append((start, end, sentence, index))
        elif found[0] == "event":
            events.append(found[1])
        else:
            tasks.append(found[1])
    parts: list[str] = []
    for pos, (start, end, sentence, index) in enumerate(kept):
        if pos > 0:
            _, prev_end, _, prev_index = kept[pos - 1]
            parts.append(text[prev_end:start] if index == prev_index + 1 else "\n")
        parts.append(sentence)
    return events, tasks, "".join(parts).strip()


def with_personal(text: str, reference_date: str | None, extract) -> dict[str, Any]:
    """Run the notice extractor only on what is not personal, and add the personal items to its result."""
    source = {"id": _id("s"), "kind": "pasted_notice", "text": text, "published_at": None}
    events, tasks, rest = split_personal(text, reference_date, source["id"])
    if not events and not tasks:
        return extract(text)
    result = extract(rest) if rest else {"events": [], "tasks": [], "warnings": [], "mode": "rules"}
    for item in [*result["events"], *result["tasks"]]:
        item["source_id"] = source["id"]
        valid_evidence = []
        for quote in item.get("evidence", []):
            quote["source_id"] = source["id"]
            if quote.get("origin") == "source_text" and quote.get("quote") and quote["quote"] not in source["text"]:
                continue
            valid_evidence.append(quote)
        item["evidence"] = valid_evidence
    result["warnings"] = list(result["warnings"]) + [
        f"「{item['title']}」{note}" for item in [*events, *tasks] for note in item["notes"]
        if note.startswith(("时间待书", "预计用时有歧义"))
    ]
    result["source"] = source
    result["events"] = [*result["events"], *events]
    result["tasks"] = [*result["tasks"], *tasks]
    return result
