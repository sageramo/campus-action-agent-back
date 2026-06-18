from __future__ import annotations

import re
from dataclasses import dataclass


CURRENT_YEAR = 2026


@dataclass
class CandidateTime:
    time_type: str
    value: str
    evidence: str
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "time_type": self.time_type,
            "value": self.value,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


TIME_PATTERNS = [
    re.compile(
        r"(?P<year>20\d{2})年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?"
        r"(?:[^\n，。；;]{0,14}?(?P<hour>\d{1,2})[:：点](?P<minute>\d{2})?)?"
    ),
    re.compile(
        r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?"
        r"(?:[^\n，。；;]{0,14}?(?P<hour>\d{1,2})[:：点](?P<minute>\d{2})?)?"
    ),
    re.compile(
        r"(?P<month>\d{1,2})[./](?P<day>\d{1,2})"
        r"(?:[^\n，。；;]{0,14}?(?P<hour>\d{1,2})[:：点](?P<minute>\d{2})?)?"
    ),
    re.compile(r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?:\s*[-至到]\s*(?P<end_hour>\d{1,2})[:：](?P<end_minute>\d{2}))?"),
]


TYPE_HINTS = [
    ("抽签/签到时间", ["抽签", "签到", "入场", "到场"]),
    ("报名截止", ["报名", "截止", "逾期", "前完成报名"]),
    ("材料提交", ["提交", "材料", "纸质", "作品", "上传", "交到", "邮箱"]),
    ("活动/比赛时间", ["活动时间", "比赛", "举行", "开始", "典礼", "班会", "会议"]),
    ("考试时间", ["考试", "入场", "听力", "试音", "考生"]),
    ("选课时间", ["选课", "退课", "补选"]),
    ("面试时间", ["面试", "问答", "答辩", "汇报"]),
    ("办理时间", ["办理", "补办", "申请", "领取"]),
    ("结果/公示时间", ["公示", "结果", "名单", "公布"]),
]


def infer_time_type(context: str) -> tuple[str, float]:
    best_label = "未分类时间"
    best_hit = 0
    for label, hints in TYPE_HINTS:
        hit = sum(hint in context for hint in hints)
        if hit > best_hit:
            best_label = label
            best_hit = hit
    if best_hit:
        return best_label, min(0.98, 0.72 + best_hit * 0.08)
    if "前" in context or "截止" in context:
        return "截止时间", 0.78
    return "未分类时间", 0.62


def normalize_match(match: re.Match[str], default_year: int = CURRENT_YEAR) -> str:
    groups = match.groupdict()
    year = int(groups.get("year") or default_year)
    month = groups.get("month")
    day = groups.get("day")
    hour = groups.get("hour")
    minute = groups.get("minute")
    end_hour = groups.get("end_hour")
    end_minute = groups.get("end_minute")
    if month and day:
        base = f"{year:04d}-{int(month):02d}-{int(day):02d}"
        if hour:
            base += f" {int(hour):02d}:{int(minute or 0):02d}"
        return base
    if hour and end_hour:
        return f"{int(hour):02d}:{int(minute):02d}/{int(end_hour):02d}:{int(end_minute):02d}"
    if hour:
        return f"{int(hour):02d}:{int(minute):02d}"
    return match.group(0)


def infer_by_proximity(prefix: str, suffix: str) -> tuple[str, float] | None:
    nearby = prefix[-22:] + suffix[:16]
    if any(key in prefix[-22:] for key in ["抽签", "签到", "入场", "到场"]):
        return "抽签/签到时间", 0.9
    if any(key in prefix[-22:] for key in ["比赛时间", "活动时间", "会议时间", "开始"]):
        return "活动/比赛时间", 0.9
    if any(key in prefix[-22:] for key in ["提交", "材料", "作品", "上传"]):
        return "材料提交", 0.88
    if "报名" in prefix[-22:] and any(key in nearby for key in ["前", "之前", "截止", "逾期"]):
        return "报名截止", 0.9
    if "退赛" in nearby or "撤回" in nearby:
        return "退赛/撤回截止", 0.86
    return None


def spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def extract_time_candidates(text: str, default_year: int = CURRENT_YEAR, limit: int = 30) -> list[CandidateTime]:
    compact = re.sub(r"\s+", " ", text)
    candidates: list[CandidateTime] = []
    seen: set[tuple[str, str]] = set()
    used_spans: list[tuple[int, int]] = []
    for pattern in TIME_PATTERNS:
        for match in pattern.finditer(compact):
            start, end = match.span()
            if any(spans_overlap((start, end), span) for span in used_spans):
                continue
            evidence = compact[max(0, start - 42) : min(len(compact), end + 42)].strip()
            local_context = compact[max(0, start - 18) : min(len(compact), end + 26)].strip()
            proximity = infer_by_proximity(compact[max(0, start - 28) : start], compact[end : min(len(compact), end + 28)])
            label, confidence = proximity or infer_time_type(local_context)
            value = normalize_match(match, default_year=default_year)
            key = (value, label)
            if key in seen:
                continue
            seen.add(key)
            used_spans.append((start, end))
            candidates.append(CandidateTime(label, value, evidence, confidence))
            if len(candidates) >= limit:
                return candidates
    return candidates
