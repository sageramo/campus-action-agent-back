"""Validate one v2 snapshot and calculate necessary bounds without persisting it.

This endpoint deliberately does not schedule tasks.  The v1 calculator supplies
only fixed-overlap and cumulative-volume checks; all relaxed or excluded
constraints remain visible in the response.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .conflict import detect_conflicts
from .time_utils import get_zone

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/task-snapshot.v2.schema.json").read_text(encoding="utf-8"))
FORMAT_CHECKER = FormatChecker()
if "date-time" not in FORMAT_CHECKER.checkers:
    raise RuntimeError("必须安装rfc3339-validator，不能跳过v2日期时间校验")
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FORMAT_CHECKER)
INACTIVE = {"completed", "cancelled"}
CONFIRMABLE_FIELDS = set(SCHEMA["$defs"]["task"]["properties"]) - {"id", "source_id", "confirmed_fields", "evidence", "uncertainties"}
COMMON_CRITICAL_FIELDS = {"title", "kind", "must_do", "priority"}


# Plain-language names for constraints the capacity check does not model.
CONSTRAINT_NAMES = {"earliest_start_at": "最早开始时刻", "deadline_at": "固定活动的截止时间", "depends_on": "前置事项",
                    "splittable": "需要连续完成", "min_block_minutes": "最小连续时长"}

def critical_fields(task: dict[str, Any]) -> set[str]:
    """Fields whose values and user confirmation gate supported calculations."""
    required = set(COMMON_CRITICAL_FIELDS)
    if task["kind"] == "fixed":
        required.update({"fixed_start_at", "fixed_end_at"})
    elif task["kind"] == "milestone":
        required.add("deadline_at")
    else:
        required.update({"deadline_at", "remaining_minutes", "splittable"})
        if task["splittable"] is True:
            required.add("min_block_minutes")
        if task["kind"] == "visit_window":
            required.add("visit_windows")
    return required


def _instant(value: str, field: str) -> datetime:
    try:
        # RFC3339 permits lower-case t/z; normalise without dropping the offset.
        normalized = value.replace("t", "T")
        if normalized.endswith("z"):
            normalized = normalized[:-1] + "Z"
        result = datetime.fromisoformat(normalized)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(get_zone())
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field} 必须是可计算且带时区偏移的有效日期时间") from exc


def _interval(item: dict[str, str], field: str) -> None:
    if _instant(item["end"], field + ".end") <= _instant(item["start"], field + ".start"):
        raise ValueError(f"{field} 结束必须晚于开始")


def validate_snapshot(snapshot: object) -> None:
    """Raise ValueError for malformed/contradictory data, not for incompleteness."""
    error = next(VALIDATOR.iter_errors(snapshot), None)
    if error is not None:
        path = ".".join(str(part) for part in error.absolute_path) or "snapshot"
        raise ValueError(f"v2数据结构不符合契约（{path}）：{error.message[:400]}")
    assert isinstance(snapshot, dict)
    _instant(snapshot["reference_time"], "reference_time")
    source_ids = [source["id"] for source in snapshot["sources"]]
    task_ids = [task["id"] for task in snapshot["tasks"]]
    for field, values in (("sources", source_ids), ("tasks", task_ids)):
        if any(not value.strip() for value in values):
            raise ValueError(f"{field} 的id不能仅含空白")
        if len(values) != len(set(values)):
            raise ValueError(f"{field} 的id不可重复")
    sources = {source["id"]: source for source in snapshot["sources"]}
    tasks = {task["id"]: task for task in snapshot["tasks"]}
    for source in snapshot["sources"]:
        if not source["text"].strip():
            raise ValueError(f"来源 {source['id']} 的原文不能仅含空白")
        if source["published_at"] is not None:
            _instant(source["published_at"], f"来源 {source['id']}.published_at")
    for index, interval in enumerate(snapshot["availability"]):
        _interval(interval, f"availability[{index}]")
    for task in snapshot["tasks"]:
        path = f"任务 {task['id']}"
        if task["status"] != "draft" and not task["title"].strip():
            raise ValueError(f"{path} 的title不能仅含空白")
        if task["source_id"] is not None and task["source_id"] not in sources:
            raise ValueError(f"{path} 的source_id不存在")
        unknown = set(task["confirmed_fields"]) - CONFIRMABLE_FIELDS
        if unknown:
            raise ValueError(f"{path} 的confirmed_fields含未知字段：{', '.join(sorted(unknown))}")
        for field in ("earliest_start_at", "deadline_at", "fixed_start_at", "fixed_end_at"):
            if task[field] is not None:
                _instant(task[field], f"{path}.{field}")
        if task["earliest_start_at"] is not None and task["deadline_at"] is not None:
            if _instant(task["earliest_start_at"], path) > _instant(task["deadline_at"], path):
                raise ValueError(f"{path} 的最早开始不能晚于截止时间")
        if task["fixed_start_at"] is not None and task["fixed_end_at"] is not None:
            _interval({"start": task["fixed_start_at"], "end": task["fixed_end_at"]}, path + ".fixed_time")
        if task["kind"] != "fixed" and (task["fixed_start_at"] is not None or task["fixed_end_at"] is not None):
            raise ValueError(f"{path} 的非fixed任务不能同时携带固定占用；准备工作请另建任务")
        if task["kind"] == "fixed" and task["remaining_minutes"] not in (None, 0):
            raise ValueError(f"{path} 的固定占用不能重复累计工作分钟；准备工作请另建flexible任务")
        if task["kind"] != "visit_window" and task["visit_windows"]:
            raise ValueError(f"{path} 的非visit_window任务不能同时携带办理窗口")
        for index, interval in enumerate(task["visit_windows"]):
            _interval(interval, f"{path}.visit_windows[{index}]")
        for evidence in task["evidence"]:
            source_id = evidence["source_id"]
            if source_id is not None and source_id not in sources:
                raise ValueError(f"{path} 的证据source_id不存在")
            if evidence["origin"] == "source_text" and evidence["quote"] not in sources[source_id]["text"]:
                raise ValueError(f"{path} 的原文证据不在对应来源中，不能用编造引文支撑字段")
        for dependency in task["depends_on"]:
            if dependency not in tasks:
                raise ValueError(f"{path} 的依赖 {dependency} 不存在")
            if dependency == task["id"]:
                raise ValueError(f"{path} 不能依赖自身")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("任务依赖存在循环，必须先修正依赖关系")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def calculate_snapshot(snapshot: object) -> dict[str, Any]:
    """Return read-only, revision-labelled necessary checks for confirmed inputs."""
    validate_snapshot(snapshot)
    assert isinstance(snapshot, dict)
    ref_now = _instant(snapshot["reference_time"], "reference_time")
    missing: list[dict[str, Any]] = []
    warnings: list[str] = []
    converted: list[dict[str, Any]] = []
    excluded: list[str] = []
    inactive: list[str] = []
    milestones: list[dict[str, Any]] = []
    tasks_by_id = {task["id"]: task for task in snapshot["tasks"]}

    def need(task: dict[str, Any], fields: list[str], reason: str) -> None:
        missing.append({"task_id": task["id"], "name": task["title"], "fields": fields, "reason": reason})

    for task in snapshot["tasks"]:
        if task["status"] in INACTIVE:
            inactive.append(task["id"])
            continue
        required = critical_fields(task)
        absent = sorted(field for field in required if task[field] is None
                        or (field == "title" and not task[field].strip())
                        or (field == "visit_windows" and not task[field]))
        unconfirmed = sorted(required - set(task["confirmed_fields"]))
        blocking = False
        if task["status"] == "draft":
            need(task, ["status"], "草稿任务尚未确认；已排除，不能把其未知工作量视为0。")
            blocking = True
        if absent:
            need(task, absent, "缺少必要字段值；未纳入本次量算。")
            blocking = True
        if unconfirmed:
            need(task, unconfirmed, "关键字段尚未经用户确认；未纳入本次量算。")
            blocking = True
        if task["kind"] == "flexible" and task["remaining_minutes"] == 0:
            need(task, ["remaining_minutes", "kind"], "未完成的工作任务不能按0分钟静默计算；请补充剩余耗时、标记完成或改为milestone。")
            blocking = True
        if task["kind"] == "visit_window":
            need(task, ["visit_windows"], "本阶段尚未验证办理窗口的选择及连续占用；已排除该任务，其工作量未按0计算。")
            blocking = True
        if blocking:
            excluded.append(task["id"])
            continue

        if task["uncertainties"]:
            need(task, ["uncertainties"], "任务仍有待核实信息：" + "；".join(task["uncertainties"]))
        constraints = []
        if task["earliest_start_at"] is not None:
            constraints.append("earliest_start_at")
        if task["kind"] == "fixed" and task["deadline_at"] is not None:
            constraints.append("deadline_at")
        if task["depends_on"]:
            unresolved = [task_id for task_id in task["depends_on"] if tasks_by_id[task_id]["status"] != "completed"]
            if unresolved:
                constraints.append("depends_on")
            else:
                warnings.append(f"{task['title']}：其全部依赖已标记完成；本阶段不生成含依赖的排程。")
        if task["kind"] == "flexible":
            if task["splittable"] is False:
                constraints.append("splittable")
            if task["min_block_minutes"] is not None and task["min_block_minutes"] > 1:
                constraints.append("min_block_minutes")
        if constraints:
            named = "、".join(CONSTRAINT_NAMES[item] for item in constraints)
            need(task, constraints, f"容量检查只比较总时长，没有检查“{named}”；时长够也不代表一定排得下，请以“生成具体安排”的结果为准。")
            warnings.append(f"{task['title']}：容量检查没有考虑“{named}”；时长不够时一定排不下，时长够时仍需生成具体安排确认。")
        if task["kind"] == "milestone":
            milestones.append(task)
            continue
        legacy = {
            "task_id": task["id"], "name": task["title"], "category": task["kind"],
            "status": task["status"], "must_do": task["must_do"], "priority": task["priority"],
            "remaining_minutes": task["remaining_minutes"],
            "deadline": _instant(task["deadline_at"], task["id"]).isoformat() if task["deadline_at"] else None,
            "fixed_time": None,
        }
        if task["kind"] == "fixed":
            legacy["fixed_time"] = {
                "start": _instant(task["fixed_start_at"], task["id"]).isoformat(),
                "end": _instant(task["fixed_end_at"], task["id"]).isoformat(),
            }
        converted.append(legacy)

    availability_known = snapshot.get("availability_confirmed", bool(snapshot["availability"]))
    availability = None
    if availability_known:
        availability = [{"start": _instant(item["start"], "availability.start").isoformat(), "end": _instant(item["end"], "availability.end").isoformat()} for item in snapshot["availability"]]
    else:
        missing.append({"task_id": None, "name": "个人空闲时段", "fields": ["availability", "availability_confirmed"],
                        "reason": "尚未明确确认个人空闲时段；空数组不代表0分钟。请填写并确认时段，或明确选择目前没有可用时间；本次仅使用扣除固定占用后的物理时间上限。"})
    result = detect_conflicts(converted, now=ref_now.isoformat(), availability=availability, timezone=snapshot["timezone"])
    # The canonical browser snapshot must never be overwritten by the v1
    # conversion.  Decisions also belong to the user, not to legacy priorities.
    result.pop("tasks")
    result.pop("recommendations")
    for task in milestones:
        if _instant(task["deadline_at"], task["id"]) <= ref_now:
            result["conflicts"].append({
                "type": "deadline_passed", "severity": "high", "tasks": [task["title"]], "task_ids": [task["id"]],
                "reason": f"{task['title']} 的截止节点已到且未标记完成；里程碑不包含准备工作量，但仍须确认节点状态。",
            })
    analyzed = [task["task_id"] for task in converted] + [task["id"] for task in milestones]
    if excluded:
        warnings.append("本次仅检查已确认且受支持的任务，排除项仍保留在快照中；下面的容量数字不是完整清单的总需求。")
    result.update({
        "schema_version": 2, "revision": snapshot["revision"],
        "status": "conflicts_detected" if result["conflicts"] else "information_needed" if missing else "no_conflict_detected",
        "missing_information": missing,
        "warnings": list(dict.fromkeys(result["warnings"] + warnings)),
        "analyzed_task_ids": analyzed, "excluded_task_ids": excluded, "inactive_task_ids": inactive,
    })
    result["limitations"].append("v2接口只读取当前快照并回传revision，不保存任务，不生成排程；no_conflict_detected只表示已检查的必要条件未发现冲突。")
    return result
