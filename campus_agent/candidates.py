"""Validate model quotations and derive draft times without trusting model dates.

The model never supplies task identity, confirmation, effort, or user priorities.
Exact quotations establish provenance, not truth of the model's interpretation;
all returned candidates still require the user's review.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator

from .time_parser import DATE_RE, extract_time_candidates
from .time_utils import DEFAULT_TIMEZONE, parse_dt, reference_day


SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/notice-extraction.v1.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
MAX_NOTICE_CHARS = 20_000
RECURRING = re.compile(r"每天|每日|每周|工作日|节假日|次日起|隔日|隔天|逢[一二三四五六日天]|周末|周[一二三四五六日天]至周")
UNCERTAIN_TIME = re.compile(r"左右|大约|大概|约为|预计|暂定|待定|择日|另行通知|视情况|任选|或")
SOURCE_TIME_QUALIFIER = re.compile(r"左右|大约|大概|约为|预计|暂定|待定|视情况|约(?=[0-9一二三四五六七八九十本这下今明昨后])")
WINDOW_ACTION = re.compile(r"报名|办理|补办|领取|选课|补选|退课|申报|申请|提交|上传|撤回|退赛")
SENTENCE_BOUNDARY = re.compile(r"[。；;\n！？!?]")
CLAUSE_BOUNDARY = re.compile(r"[，,。；;\n！？!?]")
REVIEW_WARNING = "所有事项均为待核对候选；原文引文校验不能证明模型已正确理解事项边界、否定条件或地点与材料的归属。"
MATERIALS_WARNING = "材料为模型摘录的原文片段，请人工核对“无需、仅需、除外”等否定或条件，以及是否属于当前事项。"
AUDIENCE = re.compile(r"(?:仅(?:限)?|只限|只面向|面向|限于)[^，,。；;\n]{1,100}?(?:同学|学生|人员|者)")
REGISTERED_STATUS = re.compile(r"已(?:经)?报名|报名(?:成功|通过|完成|获批)|未报名(?:者|的(?:同学|学生|人员))|报名(?:的)?(?:同学|学生|人员)")
FIXED_EVENT = re.compile(r"参加|出席|开会|举行|举办")
DEADLINE_TIME = re.compile(r"截止|[0-9０-９点时分](?:之|以)?前")
ROLE_ACTION = re.compile(r"(?:请(?:各班|各位|各学院|各年级|全体|新生|毕业班)?|各班|各位|各学院|各年级|全体|新生|毕业班|辛苦(?:各班|各位|各学院|各年级|新生|毕业班)?)(?:团支书|班长|学习委员|生活委员|负责人|辅导员|班主任)(?:老师)?(?=将|收集|整理|统计|转发|提交|组织|负责|审核|核实|汇总|领取|发放|完成|于|在|先)")
PARTICIPATION_SCOPE = re.compile(r"(?:本次|本项|此次|本活动|本竞赛)[^，,。；;\n！？!?]{0,35}?(?:仅限|只限|限于)[^，,。；;\n！？!?]{1,100}(?:参加|参与|报名)|面向对象[：:]?[^\n。；;！？!?]{1,100}")
PERSONAL_STATUS_RULE = re.compile(r"[^。\n！？!?]{0,600}仅(?:需)?办理[“\"「]?待办理[”\"」]?[^。\n！？!?]{0,100}")
CONDITIONAL_ACTION = re.compile(r"(?:如需|若需|如果需要|如有需要|若有需要)[^。\n；;！？!?]{1,300}")
PREREQUISITE_ACTION = re.compile(r"(?:申请通过|审核通过|审批通过|报名成功|办理完|完成)[^。\n；;！？!?]{0,180}?后[^。\n；;！？!?]{0,180}")
# A model note that already says the time is missing makes the generic reminder
# a duplicate on the same card. '无需在规定时间' is not a missing-time statement.
MODEL_TIME_GAP = re.compile(r"(?:未|没有)(?:给出|提供|明确|说明|注明|写明|列出|规定)[^。；;\n]{0,30}?(?:时间|截止|日期|时刻)")
MAX_OVERLAP_TITLES = 6
MODEL_YEAR_GAP = re.compile(r"(?:未|没有|无法确定)[^。；;\n]{0,12}年份")
YEAR_ASSUMED_PREFIX = "原文未给年份，暂按 "
# Only named events; a pickup or make-up whose time is announced later is not
# turned into an attendance item.
PENDING_TIME_EVENT = re.compile(r"(?P<event>宣讲会|说明会|座谈会|交流会|分享会|报告会|讲座|会议|比赛|竞赛|考试|培训|典礼|仪式|班会)"
                                r"(?:具体)?(?:时间|日期)[：:]?\s*(?:待定|另行通知|另行安排|暂未确定|待通知)[^。；;\n]*")
MAX_CANDIDATES = 40
ATTENDANCE_REQUIREMENT = re.compile(r"签到|签退|入场|入座|就座|着装|穿[^，,。；;\n]{0,6}(?:上衣|服装|正装)")
OWN_ACTION = re.compile(r"报名|提交|办理|领取|缴|填写|申请|上传|预约")
PARSER_DATE_ONLY ="只有日期，尚不能确认具体截止时刻或全天占用。"
DATE_ONLY_NOTE = "仅有日期、日期跨度、无日期时刻或重复规则，不能自动补成精确截止时间或全天占用。"


def validate_extraction_input(payload: object) -> tuple[str, str | None, str]:
    """Validate the public request without inventing a publication/reference day."""
    if not isinstance(payload, dict):
        raise ValueError("候选提取请求必须是JSON对象")
    unknown = set(payload) - {"text", "reference_date", "timezone"}
    if unknown:
        raise ValueError("候选提取请求包含未知属性")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 必须是非空通知原文")
    if len(text) > MAX_NOTICE_CHARS:
        raise ValueError(f"通知原文不能超过 {MAX_NOTICE_CHARS} 个字符，请分批处理")
    timezone = payload.get("timezone", DEFAULT_TIMEZONE)
    if timezone != DEFAULT_TIMEZONE:
        raise ValueError("timezone 当前仅支持 Asia/Shanghai")
    reference_date = payload.get("reference_date")
    reference_day(reference_date, timezone)
    return text, reference_date, timezone


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)


def _nonblank(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"模型输出 {field} 不能仅含空白")


def _validate_raw(raw: object, text: str) -> dict[str, Any]:
    error = next(VALIDATOR.iter_errors(raw), None)
    if error is not None:
        # Do not include model values/large notices in a public validation error.
        path = ".".join(str(part) for part in error.absolute_path) or "output"
        raise ValueError(f"模型输出不符合候选契约（{path}，{error.validator}）")
    assert isinstance(raw, dict)
    for note in raw["warnings"]:
        _nonblank(note, "warnings")
    for index, candidate in enumerate(raw["candidates"]):
        _nonblank(candidate["title"], f"candidates[{index}].title")
        for note in candidate["uncertainties"]:
            _nonblank(note, f"candidates[{index}].uncertainties")
        quotations = [
            ("action_quote", candidate["action_quote"]),
            ("time_quote", candidate["time_quote"]),
            ("location_quote", candidate["location_quote"]),
            *(("materials_quotes", quote) for quote in candidate["materials_quotes"]),
        ]
        for field, quote in quotations:
            if quote is None:
                continue
            _nonblank(quote, f"candidates[{index}].{field}")
            if quote not in text:
                raise ValueError(f"模型输出 candidates[{index}].{field} 不是原文精确引文")
    return raw


def _contexts(text: str, quote: str, boundary_pattern: re.Pattern[str] = SENTENCE_BOUNDARY) -> list[str]:
    """Retain adjacent recurrence/qualification words a model might have omitted."""
    contexts = []
    boundaries = list(boundary_pattern.finditer(text))
    starts = [boundary.start() for boundary in boundaries]
    ends = [boundary.end() for boundary in boundaries]
    position = 0
    while True:
        found = text.find(quote, position)
        if found < 0:
            return contexts
        left = bisect_right(ends, found)
        right = bisect_right(starts, found + len(quote) - 1)
        ending = starts[right] if right < len(starts) else len(text)
        contexts.append(text[ends[left - 1] if left else 0:ending])
        # All repetitions in this same source context have identical semantics.
        position = max(found + len(quote), ending + 1)


def _bounded_notes(notes: list[str]) -> list[str]:
    """Bound generated diagnostics, with an explicit summary if details exceed UI limits."""
    bounded = []
    for note in notes:
        _append_unique(bounded, note if len(note) <= 1000 else note[:960] + "…（提示过长，已截短；请查看原文证据）")
    if len(bounded) > 100:
        bounded = bounded[:99] + ["时间解析诊断超过显示上限，已汇总其余提示；请拆分通知并人工核对。"]
    return bounded


def _has_window_action(context: str) -> bool:
    # Registration eligibility is not an instruction to spend the whole event
    # registering. Exempt only explicit audience/status phrases when an actual
    # fixed event remains; a real '报名/提交/办理' action must still be blocked.
    remainder = REGISTERED_STATUS.sub("", AUDIENCE.sub("", context))
    if WINDOW_ACTION.search(remainder):
        return True
    return bool(WINDOW_ACTION.search(context) and not FIXED_EVENT.search(remainder))


def _receipt_only_material(quote: str, text: str) -> bool:
    """Remove only nouns whose every occurrence is directly a receipt object.

    Mixed contexts remain for human review; this is intentionally not a general
    material/negation classifier. The original proposed quote is kept as evidence.
    """
    positions = [match.start() for match in re.finditer(re.escape(quote), text)]
    return bool(positions) and all(re.search(r"(?:领取|领回|发放)\s*$", text[max(0, position - 12):position]) for position in positions)


def _time_fields(candidate: dict[str, Any], text: str, reference_date: str | None,
                 timezone: str, uncertainties: list[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {"deadline_at": None, "fixed_start_at": None, "fixed_end_at": None, "visit_windows": []}
    quote = candidate["time_quote"]
    if quote is None:
        if not any(MODEL_TIME_GAP.search(note) for note in candidate["uncertainties"]):
            _append_unique(uncertainties, "未提取到可核对的完整时间引文，请手动补充日期和时刻。")
        return fields
    # A model may omit '每天' from time_quote; check surrounding source sentences.
    contexts = _contexts(text, quote)
    if any(RECURRING.search(context) for context in contexts):
        _append_unique(uncertainties, "原文含重复或例外时间规则，尚未展开日历；不能将整段跨度作为连续占用或单个办理窗口。")
        return fields
    clauses = _contexts(text, quote, CLAUSE_BOUNDARY)
    if UNCERTAIN_TIME.search(quote) or any(SOURCE_TIME_QUALIFIER.search(context) for context in clauses):
        _append_unique(uncertainties, "时间引文含约数、暂定或选择关系，不能确定唯一精确时刻，请人工确认。")
        return fields
    if candidate["kind"] == "fixed" and any(_has_window_action(context) for context in [candidate["action_quote"], *clauses]):
        _append_unique(uncertainties, "原文行动含报名、办理或提交等窗口含义，不能仅凭模型的 fixed 分类视为全程固定占用；请核对事项类型。")
        return fields
    if candidate["kind"] == "fixed" and any(DEADLINE_TIME.search(context) for context in [quote, *clauses]):
        _append_unique(uncertainties, "原文时间是截止或最晚完成界限，不能直接当作固定活动开始，请核对事项类型。")
        return fields
    dates = list(DATE_RE.finditer(quote))
    # Only a two-ended interval may share its explicitly written start year.
    # The parser below must still produce one complete interval spanning both
    # dates; two unrelated points, relative dates and reverse/cross-year ranges
    # are not made precise by this exception.
    shared_year = (len(dates) == 2 and bool(dates[0]["iy"] or dates[0]["year"])
                   and not (dates[1]["iy"] or dates[1]["year"] or dates[1]["relative"]
                            or dates[1]["week"] or dates[1]["year_word"]))
    if reference_date is None and not shared_year and any(not (match["iy"] or match["year"]) for match in dates):
        _append_unique(uncertainties, "原文时间缺少明确年份或使用相对日期；未提供 reference_date，不能按服务器当前年份或日期补全。")
        return fields
    notes: list[str] = []
    # A fixed fallback makes the parser independent of the wall clock even for
    # malformed quotes; unresolved year/date candidates are rejected above.
    parser_year = int(reference_date[:4]) if reference_date is not None else 2000
    points = extract_time_candidates(quote, default_year=parser_year, reference_date=reference_date,
                                     timezone=timezone, limit=100, warnings=notes)
    for note in notes:
        if not (shared_year and note.startswith("原文未给年份，暂按 ")):
            _append_unique(uncertainties, note)
    if len(points) != 1:
        _append_unique(uncertainties, "时间引文未得到唯一可解析时间，或包含多个时间点；请拆分事项并补全每项时间。")
        return fields
    point = points[0]
    if shared_year and (point.kind != "interval" or point.source_start > dates[0].start() or point.source_end < dates[1].end()):
        _append_unique(uncertainties, "省略年份的终点没有与原文明确年份组成唯一连续区间，请人工核对。")
        return fields
    if any("未采纳" in note or "尚不支持" in note or "精度不一致" in note for note in notes):
        _append_unique(uncertainties, "时间引文存在未被解析的歧义或无效片段，本次保留空值。")
        return fields
    if point.precision != "datetime" or point.kind not in {"point", "interval"}:
        _append_unique(uncertainties, DATE_ONLY_NOTE)
        return fields
    start = parse_dt(point.value, timezone)
    end = parse_dt(point.end_value, timezone) if point.end_value else None
    if start is None or (point.kind == "interval" and (end is None or end <= start)):
        _append_unique(uncertainties, "解析出的时间或区间无效，请人工补充。")
        return fields
    if shared_year:
        _append_unique(uncertainties, "区间终点沿用原文起点的明确年份；请核对两端属于同一活动或办理窗口。")
    kind = candidate["kind"]
    if kind == "fixed" and point.kind == "interval":
        fields.update(fixed_start_at=start.isoformat(), fixed_end_at=end.isoformat())
    elif kind == "fixed" and point.kind == "point":
        fields["fixed_start_at"] = start.isoformat()
        _append_unique(uncertainties, "原文只明确活动开始时间，结束时间仍未知；需填写计划离场或核实结束后才能确认固定占用。")
    elif kind in {"flexible", "milestone"} and point.kind == "point":
        fields["deadline_at"] = start.isoformat()
    elif kind == "visit_window" and point.kind == "interval":
        fields["visit_windows"] = [{"start": start.isoformat(), "end": end.isoformat()}]
        _append_unique(uncertainties, "办理窗口表示可选择的到场时段，不代表全程占用；本阶段暂不支持窗口内排程。")
    else:
        _append_unique(uncertainties, "事项类型与时间形态不一致；固定占用需要明确起止，截止事项需要单一时刻，办理窗口不能变成连续占用。")
    return fields


def _year_only(note: str) -> bool:
    """A note that only says the year is missing, possibly restating the date."""
    clauses = [clause for clause in re.split(r"[，,]", note.strip().rstrip("。")) if clause]
    return bool(MODEL_YEAR_GAP.search(note)) and all("年份" in clause or clause.startswith(("仅有", "只有")) for clause in clauses)


def _fold_date_notes(uncertainties: list[str], model_notes: list[str], time_used: bool) -> list[str]:
    """Keep one reminder per date fact; the time fields themselves are unchanged."""
    notes = list(uncertainties)
    if DATE_ONLY_NOTE in notes:
        notes = [note for note in notes if note != PARSER_DATE_ONLY]
    if not time_used:
        # An assumed year that produced no computable time only adds noise.
        return [note for note in notes if not note.startswith(YEAR_ASSUMED_PREFIX)]
    if any(note.startswith(YEAR_ASSUMED_PREFIX) for note in notes):
        # The assumed-year note names the year actually used, so it supersedes
        # a model note that only says the year is missing.
        notes = [note for note in notes if not (note in model_notes and _year_only(note))]
    return notes


def _fold_attendance_requirements(candidates: list[dict[str, Any]], originals: list[dict[str, Any]]) -> tuple[list, list]:
    """Turn untimed sign-in/entry items into reminders on the one timed event.

    Such items would otherwise sit as fixed tasks without times and block
    planning. With zero or several timed events the host is ambiguous, so
    nothing is folded; the original quote stays visible on the host.
    """
    hosts = [index for index, task in enumerate(candidates) if task["kind"] == "fixed" and task["fixed_start_at"]]
    if len(hosts) != 1:
        return candidates, originals
    folded = {index for index, (task, original) in enumerate(zip(candidates, originals))
              if index != hosts[0] and task["kind"] == "fixed" and original["time_quote"] is None
              and original["location_quote"] is None and not task["materials"]
              and ATTENDANCE_REQUIREMENT.search(original["action_quote"]) and not OWN_ACTION.search(original["action_quote"])}
    host = candidates[hosts[0]]
    for index in sorted(folded):
        host["uncertainties"] = _bounded_notes([*host["uncertainties"],
            f"到场要求原文：「{originals[index]['action_quote'].rstrip('。')}」。模型曾单列为事项「{candidates[index]['title']}」，"
            "已并入本项提醒，不单独排程；如确需单独安排，请手动添加。"])
    keep = [index for index in range(len(candidates)) if index not in folded]
    return [candidates[index] for index in keep], [originals[index] for index in keep]


def _supplement_pending_events(candidates: list[dict[str, Any]], originals: list[dict[str, Any]],
                               text: str, warnings: list[str]) -> None:
    """Keep an announced event whose time is still pending as an untimed draft.

    A model may leave such an event only in warnings, so the user could miss it.
    The supplement carries no time and is labelled as program-derived.
    """
    quotes = [quote for original in originals for quote in (original["action_quote"], original["time_quote"]) if quote]
    for match in PENDING_TIME_EVENT.finditer(text):
        sentence = match.group()
        if any(quote in context or context in quote for quote in quotes for context in _contexts(text, sentence)):
            continue
        if len(candidates) >= MAX_CANDIDATES:
            _append_unique(warnings, f"原文另有时间待定的活动「{sentence}」，因候选已达{MAX_CANDIDATES}项未补出；请拆分通知后核对。")
            continue
        quotes.append(sentence)
        candidates.append({"title": f"参加{match['event']}（时间待定）", "kind": "fixed",
                           "deadline_at": None, "fixed_start_at": None, "fixed_end_at": None, "visit_windows": [],
                           "location": None, "materials": [],
                           "evidence": [{"field": field, "origin": "source_text", "quote": sentence} for field in ("title", "kind")],
                           "uncertainties": [f"该事项由程序依据原文补出：「{sentence}」。模型没有把这项时间待定的活动列为候选，"
                                             "名称与类型由规则生成，请核对是否需要参加。",
                                             "活动时间尚未确定，没有生成任何时间；时间公布后请补填开始与结束，确认前不会参与排程。"]})


def _note_same_sentence_overlap(candidates: list[dict[str, Any]], originals: list[dict[str, Any]], text: str) -> None:
    """Flag umbrella/enumerated candidates so effort is not counted twice.

    Sharing a source sentence is only a review prompt: a sentence may also hold
    two genuinely separate actions, so nothing is merged or removed here.
    """
    # A quote ending in '。' would otherwise reach into the next sentence's context.
    sentences = [set(_contexts(text, original["action_quote"].rstrip("。；;！？!?\n") or original["action_quote"]))
                 for original in originals]
    for index, task in enumerate(candidates):
        siblings = list(dict.fromkeys(candidates[other]["title"] for other in range(len(candidates))
                                      if other != index and sentences[index] & sentences[other]
                                      and candidates[other]["title"] != task["title"]))
        if not siblings:
            continue
        named = "".join(f"「{title}」" for title in siblings[:MAX_OVERLAP_TITLES])
        more = f"等{len(siblings)}项" if len(siblings) > MAX_OVERLAP_TITLES else ""
        task["uncertainties"] = _bounded_notes([*task["uncertainties"],
            f"本项与{named}{more}出自同一句原文，可能是总述与分项或同一列举；确认时请核对是否重复，避免重复计入工时。没有自动合并或删除。"])


def normalize_extraction(raw: object, text: str, reference_date: str | None = None,
                         timezone: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
    """Return provenance-preserving candidates; never return confirmed task data."""
    text, reference_date, timezone = validate_extraction_input({"text": text, "reference_date": reference_date, "timezone": timezone})
    data = _validate_raw(raw, text)
    candidates = []
    warnings = list(dict.fromkeys(data["warnings"]))
    _append_unique(warnings, REVIEW_WARNING)
    scopes = list(dict.fromkeys(scope.group() for scope in PARTICIPATION_SCOPE.finditer(text)))
    for scope in scopes[:10]:
        _append_unique(warnings, f"参加范围原文：{scope}。涉及个人参加事项时请核对适用资格，不自动认定用户满足条件。")
    if len(scopes) > 10:
        _append_unique(warnings, "原文包含超过10条不同参加范围，未全部展开提醒；请拆分通知并逐项核对适用资格。")
    roles = list(dict.fromkeys(role.group() for role in ROLE_ACTION.finditer(text)))
    if roles:
        _append_unique(warnings, f"原文另有指定执行对象：{'、'.join(roles[:6])}。请区分个人参加事项与指定角色的组织职责，不能将全部事项默认分配给同一用户。")
    status_rules = list(dict.fromkeys(match.group() for match in PERSONAL_STATUS_RULE.finditer(text)))
    for rule in status_rules[:5]:
        _append_unique(warnings, f"个人办理状态原文：{rule}。本系统没有读取你的表单状态，请核对哪些事项确为待办理，不把列举事项全部认定为必做。")
    if len(status_rules) > 5:
        _append_unique(warnings, "原文包含超过5条个人办理状态规则，未全部展开提醒；请拆分通知并核对个人待办理范围。")
    for original in data["candidates"]:
        uncertainties = list(dict.fromkeys(original["uncertainties"]))
        for condition in AUDIENCE.finditer(original["action_quote"]):
            _append_unique(uncertainties, f"适用条件原文：{condition.group()}。请核对你是否满足该条件，不自动认定为个人必做。")
        for role in ROLE_ACTION.finditer(original["action_quote"]):
            _append_unique(uncertainties, f"执行对象原文：{role.group()}。请确认该职责是否由你承担，不将他人职责自动认定为个人必做。")
        for rule in status_rules:
            if original["action_quote"] in rule or rule in original["action_quote"]:
                _append_unique(uncertainties, f"个人办理状态原文：{rule}。须核对个人表单为待办理后再认领，不自动认定你需要完成。")
        for condition in CONDITIONAL_ACTION.finditer(original["action_quote"]):
            _append_unique(uncertainties, f"条件行动原文：{condition.group()}。请确认该条件适用于你；条件申请不是所有人的必做事项。")
        for condition in PREREQUISITE_ACTION.finditer(original["action_quote"]):
            _append_unique(uncertainties, f"前置条件原文：{condition.group()}。请核对前置是否完成；没有自动创建或确认任务依赖。")
        timings = _time_fields(original, text, reference_date, timezone, uncertainties)
        uncertainties = _fold_date_notes(uncertainties, original["uncertainties"],
                                         any(value is not None and value != [] for value in timings.values()))
        evidence = [{"field": field, "origin": "source_text", "quote": original["action_quote"]} for field in ("title", "kind")]
        for field, value in timings.items():
            if value is not None and value != []:
                evidence.append({"field": field, "origin": "source_text", "quote": original["time_quote"]})
        if original["time_quote"] is not None and all(value is None or value == [] for value in timings.values()):
            proposed_fields = {"fixed": ("fixed_start_at", "fixed_end_at"), "visit_window": ("visit_windows",)}.get(original["kind"], ("deadline_at",))
            for field in proposed_fields:
                evidence.append({"field": field, "origin": "source_text", "quote": original["time_quote"]})
            _append_unique(uncertainties, "时间原文已保留在字段证据中，本次未采用为可计算时间，请人工核对。")
        if original["location_quote"] is not None:
            evidence.append({"field": "location", "origin": "source_text", "quote": original["location_quote"]})
        proposed_materials = list(dict.fromkeys(original["materials_quotes"]))
        materials = []
        for quote in proposed_materials:
            evidence.append({"field": "materials", "origin": "source_text", "quote": quote})
            if _receipt_only_material(quote, text):
                _append_unique(uncertainties, f"材料候选「{quote}」在原文中仅作为领取或发放对象出现，暂不列为需准备材料；原文依据保留供核对。")
            else:
                materials.append(quote)
        if materials:
            _append_unique(uncertainties, MATERIALS_WARNING)
        candidates.append({"title": original["title"].strip(), "kind": original["kind"], **timings,
                           "location": original["location_quote"], "materials": materials,
                           "evidence": evidence, "uncertainties": _bounded_notes(uncertainties)})
    candidates, originals = _fold_attendance_requirements(candidates, data["candidates"])
    _note_same_sentence_overlap(candidates, originals, text)
    # Folded quotes still count as covered, so pending-event detection sees all.
    _supplement_pending_events(candidates, data["candidates"], text, warnings)
    if not candidates:
        _append_unique(warnings, "未得到候选事项；请核对原文是否包含个人行动，必要时手动添加。")
    return {"schema_version": 1, "provider": "dify", "parsing_status": "needs_confirmation",
            "candidates": candidates, "warnings": warnings}
