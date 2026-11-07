"""Conservative time candidates, retaining precision, evidence and uncertainty."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from .time_utils import DEFAULT_TIMEZONE, get_zone, reference_day


@dataclass
class CandidateTime:
    time_type: str
    value: str
    evidence: str
    confidence: float
    end_value: str | None = None
    precision: str = "datetime"
    kind: str = "point"
    timezone: str = DEFAULT_TIMEZONE
    warnings: list[str] = field(default_factory=list)
    source_start: int = 0
    source_end: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


DATE_RE = re.compile(
    r"(?<!\d)(?:(?P<iy>\d{4})(?P<sep>[./-])(?P<im>\d{1,2})(?P=sep)(?P<id>\d{1,2})(?!\d)"
    r"|(?:(?P<year>\d{4})年\s*|(?P<year_word>今年|明年|去年))?(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?"
    r"|(?P<sm>\d{1,2})[./](?P<sd>\d{1,2})(?![\d./])"
    r"|(?P<relative>大后天|后天|明天|今天|今日|昨天)"
    r"|(?<!下)(?P<week>本周|这周|下周)(?P<weekday>[一二三四五六日天])"
    r"|(?<![\d年月./-])(?P<bare_day>0?[1-9]|[12]\d|3[01])\s*[日号](?!楼|室|馆|门|线|房|公寓|宿舍|\d+(?:室|楼|号|报告厅|教室|房|幢)))"
)
WEEKDAY_ANNOTATION_RE = re.compile(r"\s*[（(]\s*(?:周|星期)(?P<weekday>[一二三四五六日天])\s*[）)]")
CN = "零〇一二两三四五六七八九十"
CLOCK_RE = re.compile(
    rf"(?<![\d:：])(?P<period>凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|晚间|夜间|(?<!最)早|(?<!最)晚)?\s*"
    rf"(?P<hour>\d{{1,2}}|[{CN}]{{1,3}})"
    rf"(?:(?:[:：](?P<minute>\d{{2}})(?::(?P<second>\d{{2}}))?)"
    rf"|(?:[点时](?P<cn_minute>半|一刻|三刻|\d{{1,2}}分?|[{CN}]{{1,3}}分)?))(?!\d)"
)
CLAUSE_RE = re.compile(r"[^，,。；;\n！？!?]+")
RANGE_RE = re.compile(r"\s*(?:(?P<next>次日|翌日)\s*)?(?:[-—–~～至到])\s*(?:(?P<next_after>次日|翌日)\s*)?$")
TYPE_HINTS = [
    ("抽签/签到时间", ["抽签", "签到", "入场", "到场"]),
    ("报名截止", ["报名"]),
    ("材料提交", ["提交", "上传", "交到", "交申请表", "交材料"]),
    ("活动/比赛时间", ["活动", "比赛", "讲座", "举行", "典礼", "班会", "会议", "开幕"]),
    ("考试时间", ["考试", "听力", "试音"]),
    ("选课时间", ["选课", "退课", "补选"]),
    ("面试时间", ["面试", "答辩", "汇报"]),
    ("办理时间", ["办理", "补办", "申请", "领取"]),
    ("结果/公示时间", ["公示", "结果", "名单", "公布"]),
    ("退赛/撤回截止", ["退赛", "撤回"]),
]


DEADLINE_AFTER = re.compile(r"\s*(?:(?:之|以)?前|截止|为止)")
# Labels that can name a deadline; event labels cannot.
DEADLINE_LABELS = {"报名截止", "材料提交", "选课时间", "办理时间", "退赛/撤回截止"}


def infer_time_type(context: str, start: int = 0, end: int = 0) -> tuple[str, float]:
    # "15:00前完成讲座报名": the 前 decides the time's role even if 讲座 is the nearest hint.
    deadline = end > start and bool(DEADLINE_AFTER.match(context, end))
    hits = []
    for order, (label, hints) in enumerate(TYPE_HINTS):
        if deadline and label not in DEADLINE_LABELS:
            continue
        for hint in hints:
            for match in re.finditer(re.escape(hint), context):
                distance = max(start - match.end(), match.start() - end, 0)
                hits.append((distance, -len(hint), order, label))
    if hits and min(hits)[0] <= 24:
        return min(hits)[3], 0.85
    if "截止" in context or "前" in context:
        return "截止时间", 0.7
    return "未分类时间", 0.6


def cn_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    if "十" in value:
        left, right = value.split("十", 1)
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    return digits[value]


def clock_value(match: re.Match[str], inherited_period: str | None = None) -> tuple[int, int, int]:
    hour = cn_number(match["hour"])
    minute_text = match["cn_minute"]
    if match["minute"] is not None:
        minute = int(match["minute"])
    elif minute_text in {"半", "一刻", "三刻"}:
        minute = {"半": 30, "一刻": 15, "三刻": 45}[minute_text]
    else:
        minute = cn_number(minute_text.rstrip("分")) if minute_text else 0
    second = int(match["second"] or 0)
    period = match["period"] or inherited_period
    if not (0 <= hour <= 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError("时分秒超出合法范围")
    if hour == 24:
        if minute or second:
            raise ValueError("24点只能表示24:00")
    elif period in {"下午", "傍晚", "晚上", "晚间", "夜间", "晚", "中午"} and hour == 0:
        raise ValueError("时段与0点矛盾，请明确日期和时刻")
    elif period in {"下午", "傍晚", "晚上", "晚间", "夜间", "晚"}:
        if hour == 12 and period in {"晚上", "晚间", "夜间", "晚"}:
            raise ValueError("晚上12点有日期歧义，请明确为当日24:00或次日00:00")
        if 1 <= hour < 12:
            hour += 12
    elif period == "中午" and 1 <= hour <= 2:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    if period in {"上午", "早上", "清晨", "凌晨", "早"} and hour > 12:
        raise ValueError("时段与小时矛盾")
    return hour, minute, second


def dated_value(day: date | None, clock: tuple[int, int, int]) -> str:
    hour, minute, second = clock
    if day is None:
        return f"{hour:02d}:{minute:02d}" + (f":{second:02d}" if second else "")
    dt = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=minute, seconds=second)
    return dt.isoformat(sep=" ", timespec="seconds" if second else "minutes")


def extract_time_candidates(
    text: str, default_year: int | None = None, limit: int = 30, *,
    reference_date: str | None = None, timezone: str = DEFAULT_TIMEZONE,
    warnings: list[str] | None = None,
) -> list[CandidateTime]:
    zone = get_zone(timezone)
    ref = reference_day(reference_date, timezone)
    current_year = default_year if default_year is not None else (ref or datetime.now(zone).date()).year
    warnings = warnings if warnings is not None else []
    candidates: list[CandidateTime] = []
    recurring = bool(re.search(r"每天|每日|每周|工作日|次日起|节假日", text))
    recurring_note = "发现重复或例外时间规则，尚未展开日历，请人工确认，不能把整个跨度当作连续占用。"
    if recurring:
        warnings.append(recurring_note)
    annotations = [annotation.span() for day_match in DATE_RE.finditer(text)
                   if (annotation := WEEKDAY_ANNOTATION_RE.match(text, day_match.end()))]
    unsupported = re.finditer(r"(?<![本这下])周[一二三四五六日天]|下下周|下个月|月底|近日|稍后", text)
    if any(not any(start <= match.start() < end for start, end in annotations) for match in unsupported):
        warnings.append("存在尚不支持或含歧义的相对时间表达，请改为明确日期。")

    for clause_match in CLAUSE_RE.finditer(text):
        clause = clause_match.group()
        base = clause_match.start()
        dates = list(DATE_RE.finditer(clause))
        used_clocks: set[tuple[int, int]] = set()
        clause_candidates: list[CandidateTime] = []
        for index, match in enumerate(dates):
            note: list[str] = []
            try:
                if match["relative"] or match["week"]:
                    if ref is None:
                        raise ValueError("相对日期缺少 reference_date；需指定原通知发布日或用户确认的参照日")
                    if match["relative"]:
                        day = ref + timedelta(days={"今天": 0, "今日": 0, "明天": 1, "后天": 2, "大后天": 3, "昨天": -1}[match["relative"]])
                    else:
                        offset = "一二三四五六日".index(match["weekday"].replace("天", "日"))
                        day = ref - timedelta(days=ref.weekday()) + timedelta(days=offset + (7 if match["week"] == "下周" else 0))
                    note.append(f"相对日期基于用户指定参照日 {ref.isoformat()}，请核对是否为原通知语境。")
                elif match["bare_day"]:
                    if ref is None:
                        raise ValueError("只写日号需要用户指定原通知 reference_date，不能使用机器年月")
                    if index > 0:
                        raise ValueError("同一分句含完整日期及省略月份的日号，归属尚不支持；请补全日期")
                    day = date(ref.year, ref.month, int(match["bare_day"]))
                    if day < ref:
                        raise ValueError("日号早于通知参照日，不能自动猜测跨月，请补充月份")
                    note.append(f"日号按用户指定通知参照日 {ref.isoformat()} 的年月解释为 {day.isoformat()}，请核对语境。")
                else:
                    year_text = match["iy"] or match["year"]
                    if match["year_word"]:
                        if ref is None:
                            raise ValueError("相对年份需要 reference_date")
                        current_year = ref.year + {"今年": 0, "明年": 1, "去年": -1}[match["year_word"]]
                        note.append(f"相对年份基于用户指定参照日 {ref.isoformat()}，需核对。")
                    elif year_text:
                        current_year = int(year_text)
                    else:
                        note.append(f"原文未给年份，暂按 {current_year} 年解释，需确认。")
                    day = date(current_year, int(match["im"] or match["month"] or match["sm"]), int(match["id"] or match["day"] or match["sd"]))
                annotation = WEEKDAY_ANNOTATION_RE.match(clause, match.end())
                if annotation:
                    stated = "一二三四五六日".index(annotation['weekday'].replace('天', '日'))
                    if day.weekday() != stated:
                        raise ValueError("日期与括号中的星期不一致，请核对原通知")
            except (ValueError, OverflowError) as exc:
                warnings.append(f"未采纳时间「{match.group()}」：{exc}")
                boundary = dates[index + 1].start() if index + 1 < len(dates) else len(clause)
                used_clocks.update(m.span() for m in CLOCK_RE.finditer(clause, match.end(), boundary))
                continue

            boundary = dates[index + 1].start() if index + 1 < len(dates) else len(clause)
            clocks = list(CLOCK_RE.finditer(clause, match.end(), boundary))
            if not clocks:
                label, confidence = infer_time_type(clause, match.start(), match.end())
                clause_candidates.append(CandidateTime(label, day.isoformat(), match.group(), confidence, precision="date", timezone=timezone, warnings=note + ["只有日期，尚不能确认具体截止时刻或全天占用。"], source_start=base + match.start(), source_end=base + match.end()))
            else:
                cursor = 0
                while cursor < len(clocks):
                    clock = clocks[cursor]
                    used_clocks.add(clock.span())
                    end_clock = clocks[cursor + 1] if cursor + 1 < len(clocks) else None
                    connector = RANGE_RE.fullmatch(clause[clock.end():end_clock.start()]) if end_clock else None
                    if connector:
                        used_clocks.add(end_clock.span())
                    try:
                        value = dated_value(day, clock_value(clock))
                        end_value = None
                        end_pos = clock.end()
                        if connector:
                            end_day = day + timedelta(days=1 if connector["next"] or connector["next_after"] else 0)
                            end_value = dated_value(end_day, clock_value(end_clock, clock["period"]))
                            end_pos = end_clock.end()
                            if end_value <= value:
                                raise ValueError("区间结束不晚于开始；跨午夜请写明次日或结束日期")
                        label, confidence = infer_time_type(clause, match.start(), end_pos)
                        clause_candidates.append(CandidateTime(label, value, clause[match.start():end_pos], confidence, end_value=end_value, kind="interval" if end_value else "point", timezone=timezone, warnings=note.copy(), source_start=base + match.start(), source_end=base + end_pos))
                    except (ValueError, KeyError, OverflowError) as exc:
                        warnings.append(f"未采纳时间「{clause[match.start():end_clock.end() if connector else clock.end()]}」：{exc}")
                    cursor += 2 if connector else 1

        combined: list[CandidateTime] = []
        for item in clause_candidates:
            previous = combined[-1] if combined else None
            bridge = text[previous.source_end:item.source_start] if previous else ""
            if previous and RANGE_RE.fullmatch(bridge) and previous.end_value is None:
                endpoint = item.end_value or item.value
                if endpoint <= previous.value:
                    warnings.append(f"未采纳反向日期区间「{text[previous.source_start:item.source_end]}」")
                    combined.pop()
                    continue
                if previous.precision != item.precision:
                    warnings.append("区间两端时间精度不一致，请补全具体开始和结束时刻。")
                else:
                    previous.end_value = endpoint
                    previous.kind = "date_range" if previous.precision == "date" else "interval"
                    previous.source_end = item.source_end
                    previous.evidence = text[previous.source_start:item.source_end]
                    previous.warnings.extend(n for n in item.warnings if n not in previous.warnings)
                    continue
            combined.append(item)
        candidates.extend(combined)

        unanchored = [match for match in CLOCK_RE.finditer(clause) if match.span() not in used_clocks]
        cursor = 0
        while cursor < len(unanchored):
            match = unanchored[cursor]
            end_match = unanchored[cursor + 1] if cursor + 1 < len(unanchored) else None
            connector = RANGE_RE.fullmatch(clause[match.end():end_match.start()]) if end_match else None
            end_pos = end_match.end() if connector else match.end()
            try:
                value = dated_value(None, clock_value(match))
                end_value = dated_value(None, clock_value(end_match, match["period"])) if connector else None
                if end_value and end_value <= value:
                    raise ValueError("无日期区间跨日关系未明确，请补全日期")
            except (ValueError, KeyError) as exc:
                warnings.append(f"未采纳时间「{match.group()}」：{exc}")
                cursor += 2 if connector else 1
                continue
            label, confidence = infer_time_type(clause, match.start(), end_pos)
            candidates.append(CandidateTime(label, value, clause[match.start():end_pos], confidence, end_value=end_value, kind="interval" if end_value else "point", precision="time", timezone=timezone, warnings=["缺少日期，不能直接排入日历。"], source_start=base + match.start(), source_end=base + end_pos))
            cursor += 2 if connector else 1

    candidates.sort(key=lambda item: item.source_start)
    if len(candidates) > limit:
        warnings.append(f"时间候选超过 {limit} 个，已截断，请分批处理通知。")
    for item in candidates[:limit]:
        if recurring:
            item.kind = "recurring_unexpanded"
            item.warnings.append(recurring_note)
        warnings.extend(note for note in item.warnings if note not in warnings)
    return candidates[:limit]
