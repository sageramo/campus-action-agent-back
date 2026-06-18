from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from .models import ConflictTask


DT_FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace("24:00", "23:59")
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def working_capacity(start: datetime, end: datetime, weekday_hours: float = 4, weekend_hours: float = 8) -> float:
    if end <= start:
        return 0
    days = (end.date() - start.date()).days + 1
    total = 0.0
    for i in range(days):
        day = start.date().toordinal() + i
        weekday = datetime.fromordinal(day).weekday()
        total += weekend_hours if weekday >= 5 else weekday_hours
    return total


def detect_conflicts(tasks_data: list[dict[str, Any]], now: str | None = None) -> dict[str, Any]:
    tasks = [ConflictTask.from_dict(item) for item in tasks_data]
    fixed = []
    for task in tasks:
        if task.fixed_time:
            start = parse_dt(task.fixed_time.start)
            end = parse_dt(task.fixed_time.end)
            if start and end:
                fixed.append((task, start, end))

    conflicts: list[dict[str, Any]] = []
    for i, (a, a_start, a_end) in enumerate(fixed):
        for b, b_start, b_end in fixed[i + 1 :]:
            if overlap(a_start, a_end, b_start, b_end):
                conflicts.append(
                    {
                        "type": "hard_time_overlap",
                        "severity": "high" if a.must_do and b.must_do else "medium",
                        "tasks": [a.name, b.name],
                        "reason": f"{a.name}（{a_start:%Y-%m-%d %H:%M}-{a_end:%H:%M}）与 {b.name}（{b_start:%Y-%m-%d %H:%M}-{b_end:%H:%M}）时间重叠。",
                    }
                )

    ref_now = parse_dt(now) or datetime.now().replace(second=0, microsecond=0)
    by_deadline_week: dict[str, list[ConflictTask]] = defaultdict(list)
    for task in tasks:
        deadline = parse_dt(task.deadline)
        if deadline:
            by_deadline_week[f"{deadline.year}-W{deadline.isocalendar().week:02d}"].append(task)
            if deadline > ref_now:
                need = sum(t.estimated_hours for t in tasks if parse_dt(t.deadline) and parse_dt(t.deadline) <= deadline)
                capacity = working_capacity(ref_now, deadline)
                if need > capacity * 0.9:
                    conflicts.append(
                        {
                            "type": "capacity_shortage",
                            "severity": "high" if need > capacity else "medium",
                            "tasks": [task.name],
                            "reason": f"截至 {deadline:%Y-%m-%d %H:%M} 前累计预计需要 {need:.1f} 小时，可用容量约 {capacity:.1f} 小时，缓冲不足。",
                        }
                    )

    exam_tasks = [t for t in tasks if "复习" in t.name or t.category == "学习"]
    exam_hours = sum(t.estimated_hours for t in exam_tasks)
    if len(exam_tasks) >= 4 and exam_hours >= 45:
        conflicts.append(
            {
                "type": "exam_review_pressure",
                "severity": "high",
                "tasks": [t.name for t in exam_tasks[:8]],
                "reason": f"检测到 {len(exam_tasks)} 项学习/复习任务，合计约 {exam_hours:.1f} 小时，属于期末高压复习窗口。",
            }
        )

    for task in tasks:
        if any(key in task.name for key in ["打印", "材料", "PPT准备", "报名确认"]):
            deadline = parse_dt(task.deadline)
            if deadline:
                conflicts.append(
                    {
                        "type": "sequence_risk",
                        "severity": "medium",
                        "tasks": [task.name],
                        "reason": f"{task.name} 是前置准备任务，必须在 {deadline:%Y-%m-%d %H:%M} 前完成，不能拖到现场处理。",
                    }
                )

    return {
        "tasks": [task.to_dict() for task in tasks],
        "conflicts": conflicts,
        "recommendations": recommend(tasks, conflicts),
    }


def recommend(tasks: list[ConflictTask], conflicts: list[dict[str, Any]]) -> dict[str, list[str]]:
    keep = []
    adjust = []
    drop_or_delegate = []
    for task in sorted(tasks, key=lambda item: (not item.must_do, -item.priority, item.estimated_hours)):
        if task.must_do or task.priority >= 80:
            keep.append(task.name)
        elif task.priority >= 55:
            adjust.append(f"{task.name}：压缩范围、调整时间或寻找协作。")
        else:
            drop_or_delegate.append(f"{task.name}：建议延期、委托他人或放弃低收益目标。")
    if any(item["type"] == "hard_time_overlap" for item in conflicts):
        adjust.append("固定时间冲突优先通过请假、换班、代办或改期解决。")
    if any(item["type"] == "capacity_shortage" for item in conflicts):
        adjust.append("截止前容量不足时，先保留考试、强制提交和核心目标，压缩非核心学生工作。")
    return {
        "keep": keep[:8],
        "adjust": adjust[:8],
        "drop_or_delegate": drop_or_delegate[:8],
    }
