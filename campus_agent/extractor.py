from __future__ import annotations

import re
from pathlib import Path

from .models import ActionTask, TimePoint
from .text_io import read_text_file
from .time_parser import extract_time_candidates
from .time_utils import DEFAULT_TIMEZONE


MATERIAL_HINTS = ["申请表", "报名表", "成绩单", "准考证", "身份证", "学生证", "照片", "PPT", "简历", "承诺书", "证明", "材料", "附件"]
LOCATION_RE = re.compile(r"(?:地点|地址|教室|会议室|办公室)[：:\s为在]*([^\n。；;，,]{2,40})")
DEPARTMENT_RE = re.compile(r"(学生工作部|本科生院|国际处|港澳台办|创新创业教育中心|体育部|团委|学院|教务处|校医院)")


def infer_category(text: str, source_category: str = "") -> str:
    if source_category in {"活动策划", "冲突案例", "多时间点通知"}:
        return source_category
    rules = [
        ("考试/学习", ["考试", "四六级", "选课", "课程", "复习"]),
        ("竞赛/活动", ["比赛", "竞赛", "大广赛", "羽毛球", "晚会", "夜跑", "摆台"]),
        ("评奖/项目申报", ["资助", "奖学金", "大创", "申报", "项目"]),
        ("办事流程", ["补办", "申请", "报销", "提交", "办理"]),
        ("入学/转专业", ["新生", "入学", "转专业", "面试"]),
    ]
    for category, keys in rules:
        if any(key in text for key in keys):
            return category
    return source_category or "校园任务"


def guess_title(text: str, filename: str = "") -> str:
    for line in text.splitlines():
        line = line.strip()
        if 6 <= len(line) <= 46 and not line.startswith("{"):
            return line
    return Path(filename).stem or "未命名通知"


def extract_materials(text: str) -> list[str]:
    materials: list[str] = []
    for hint in MATERIAL_HINTS:
        for match in re.finditer(re.escape(hint), text):
            prefix = re.split(r"[，,。；;\n]", text[:match.start()])[-1]
            # Bound a negation by an explicit contrast/positive instruction.
            prefix = re.split(r"但|而|只需|仍需|必须", prefix)[-1]
            if not re.search(r"无需|无须|不必|不用|不需要|免交|免于|不得|禁止", prefix):
                materials.append(hint)
                break
    return materials


def extract_steps(text: str, category: str, times: list[TimePoint]) -> list[str]:
    steps: list[str] = []
    if times:
        steps.append("确认通知中的关键时间点，优先处理截止时间最早的任务。")
    if "报名" in text:
        steps.append("在报名截止前完成报名信息填写或队伍名单确认。")
    if "提交" in text or "材料" in text or "作品" in text:
        steps.append("整理所需材料，并按通知要求提交到指定系统、邮箱或线下地点。")
    if "地点" in text or "教室" in text or "会议室" in text:
        steps.append("提前确认地点和到达路线，固定活动建议提前 10-15 分钟到场。")
    if category == "活动策划":
        steps.extend(["明确活动对象、时间、地点、预算和人员分工。", "检查审批、物资、宣传和风险预案是否齐全。"])
    if not steps:
        steps.append("补充任务目标、截止时间、地点和材料要求后再生成完整行动清单。")
    return steps[:6]


def missing_fields(task: ActionTask) -> list[str]:
    missing: list[str] = []
    if not task.time_points:
        missing.append("关键时间")
    if not task.locations:
        missing.append("地点")
    elif len(task.locations) > 1:
        missing.append("地点与事项对应关系")
    if not task.materials and task.category in {"办事流程", "评奖/项目申报", "入学/转专业"}:
        missing.append("所需材料")
    if not task.department:
        missing.append("负责部门")
    return missing


def parse_notice_text(text: str, source: str = "", source_category: str = "", *, reference_date: str | None = None, timezone: str = DEFAULT_TIMEZONE) -> ActionTask:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 必须是非空字符串")
    if len(text) > 100_000:
        raise ValueError("通知文本不得超过100000字，请分批处理")
    category = infer_category(text, source_category)
    warnings: list[str] = []
    time_points = [TimePoint(**item.to_dict()) for item in extract_time_candidates(text, reference_date=reference_date, timezone=timezone, warnings=warnings)]
    locations = list(dict.fromkeys(match.group(1).strip() for match in LOCATION_RE.finditer(text)))
    if len(locations) > 1:
        warnings.append("已保留多个地点，尚未确认各地点对应的具体事项，不能统一套用第一个地点。")
    department_match = DEPARTMENT_RE.search(text)
    task = ActionTask(
        task_name=guess_title(text, source),
        category=category,
        time_points=time_points,
        location=locations[0] if len(locations) == 1 else None,
        department=department_match.group(1) if department_match else None,
        materials=extract_materials(text),
        source=source,
        confidence=0.82 if time_points else 0.62,
        locations=locations,
        warnings=warnings,
    )
    task.steps = extract_steps(text, category, time_points)
    task.missing_fields = missing_fields(task)
    return task


def parse_notice_file(path: Path, source_category: str = "") -> ActionTask:
    text = read_text_file(path)
    return parse_notice_text(text, source=str(path), source_category=source_category or path.parent.name)
