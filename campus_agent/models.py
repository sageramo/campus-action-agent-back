from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TimePoint:
    time_type: str
    value: str
    evidence: str
    confidence: float = 0.7

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
    estimated_hours: float = 0.0
    must_do: bool = False
    deadline: str | None = None
    fixed_time: FixedTime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConflictTask":
        fixed = data.get("fixed_time")
        return cls(
            task_id=str(data.get("task_id", "")),
            name=str(data.get("name", "")),
            category=str(data.get("category", "其他")),
            priority=int(data.get("priority", 50)),
            estimated_hours=float(data.get("estimated_hours") or 0),
            must_do=bool(data.get("must_do", False)),
            deadline=data.get("deadline"),
            fixed_time=FixedTime(**fixed) if isinstance(fixed, dict) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data
