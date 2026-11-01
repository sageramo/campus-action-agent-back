from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any


@dataclass
class TimePoint:
    time_type: str
    value: str
    evidence: str
    confidence: float = 0.7
    end_value: str | None = None
    precision: str = "datetime"
    kind: str = "point"
    timezone: str = "Asia/Shanghai"
    warnings: list[str] = field(default_factory=list)
    source_start: int = 0
    source_end: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionTask:
    task_name: str
    category: str
    time_points: list[TimePoint] = field(default_factory=list)
    location: str | None = None
    department: str | None = None
    materials: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    source: str | None = None
    confidence: float = 0.75
    locations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parsing_status: str = "needs_confirmation"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["time_points"] = [tp.to_dict() for tp in self.time_points]
        return data


@dataclass
class FixedTime:
    start: str
    end: str


@dataclass
class ConflictTask:
    task_id: str
    name: str
    category: str
    priority: int = 50
    estimated_hours: float | None = None
    must_do: bool = False
    deadline: str | None = None
    fixed_time: FixedTime | None = None
    status: str = "pending"
    remaining_minutes: float | None = None
    _remaining_supplied: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConflictTask":
        if not isinstance(data, dict):
            raise ValueError("每个任务必须是对象")
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            raise ValueError("任务 name 必须是非空字符串")
        for key in ("task_id", "category"):
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"任务 {key} 必须是字符串")
        priority = data.get("priority", 50)
        if type(priority) is not int or not 0 <= priority <= 100:
            raise ValueError("priority 必须是0至100的整数")
        must_do = data.get("must_do", False)
        if type(must_do) is not bool:
            raise ValueError("must_do 必须是布尔值，不能传字符串false或true")
        for key in ("estimated_hours", "remaining_minutes"):
            value = data.get(key)
            if value is not None and (type(value) not in (int, float) or not 0 <= value <= 10_000_000 or not math.isfinite(value)):
                raise ValueError(f"{key} 必须是有限的非负数或null")
        status = data.get("status", "pending")
        if not isinstance(status, str) or status not in {"pending", "draft", "confirmed", "scheduled", "in_progress", "completed", "cancelled"}:
            raise ValueError("不支持的任务 status")
        if status == "completed" and data.get("remaining_minutes") not in (None, 0):
            raise ValueError("已完成任务的 remaining_minutes 必须为0或省略")
        if data.get("deadline") is not None and not isinstance(data["deadline"], str):
            raise ValueError("deadline 必须是日期时间字符串或null")
        fixed = data.get("fixed_time")
        if fixed is not None and (not isinstance(fixed, dict) or set(fixed) != {"start", "end"}):
            raise ValueError("fixed_time 必须包含且仅包含start和end")
        return cls(
            task_id=data.get("task_id", ""),
            name=data["name"].strip(),
            category=data.get("category", "其他"),
            priority=priority,
            estimated_hours=data.get("estimated_hours"),
            must_do=must_do,
            deadline=data.get("deadline"),
            fixed_time=FixedTime(**fixed) if isinstance(fixed, dict) else None,
            status=status,
            remaining_minutes=data.get("remaining_minutes"),
            _remaining_supplied="remaining_minutes" in data,
        )

    @property
    def work_minutes(self) -> float | None:
        if self.status in {"completed", "cancelled"} or self.fixed_time:
            return 0.0
        if self._remaining_supplied or self.remaining_minutes is not None:
            return self.remaining_minutes
        return self.estimated_hours * 60 if self.estimated_hours is not None else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_remaining_supplied")
        # Canonical output must survive a round trip through the task editor.
        data["remaining_minutes"] = self.work_minutes
        if self.status in {"completed", "cancelled"} or self.fixed_time:
            data["workload_basis"] = "none"
        elif self.work_minutes is None:
            data["workload_basis"] = "unknown"
        else:
            data["workload_basis"] = "remaining_minutes" if self._remaining_supplied or self.remaining_minutes is not None else "estimated_hours_assumed_remaining"
        return data
