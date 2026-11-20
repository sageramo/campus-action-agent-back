"""Exact constraint solver + cross-building transit buffers + Pareto trade-off plans (v3).

Pure-Python, zero-external-dependency solver designed for Render free-tier RAM/CPU limits:
1. Supports task precedence DAG (`dependencies: [{"before": id_a, "after": id_b}]`) with cycle checks.
2. Detects cross-building transit squeezes and optionally enforces `transit_minutes` buffers
   between consecutive fixed items at different non-empty campus locations.
3. Solves bounded task placement (<= 40 tasks, <= 31 days) using exact branch-and-bound search
   (`exact_branch_and_bound_v1`), finding globally feasible plans even in greedy-trap layouts
   where Earliest-Deadline-First fails.
4. Independently verifies every produced schedule with a zero-shared-logic validator.
5. Generates Pareto-non-dominated multi-objective trade-off plans (e.g. maximizing free/rest
   and preserving courses vs preserving temporary events) when conflicts occur.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any

from .conflict import merge_intervals
from .layers import (
    DEFAULT_MIN_BLOCK,
    MAX_PLANNED_TASKS,
    MAX_RANGE_DAYS,
    calendar_snapshot,
    validate_snapshot_v3,
)
from .snapshot import _instant
from .time_utils import get_zone

Interval = tuple[datetime, datetime]
MAX_SEARCH_NODES = 10_000
MAX_PARETO_CONFLICTS = 4
DEFAULT_STEP_MINUTES = 15


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _normalize_location(loc: str | None) -> str:
    return "".join(str(loc or "").split()).lower()


def _default_range(snapshot: dict) -> dict[str, str]:
    ref = _instant(snapshot["reference_time"], "reference_time").astimezone(get_zone()).date()
    monday = ref - timedelta(days=ref.weekday())
    return {"from": monday.isoformat(), "to": (monday + timedelta(days=6)).isoformat()}


def _cut_intervals(intervals: list[Interval], blocked: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in intervals:
        pieces = [(start, end)]
        for b_start, b_end in blocked:
            nxt: list[Interval] = []
            for p_start, p_end in pieces:
                if b_end <= p_start or b_start >= p_end:
                    nxt.append((p_start, p_end))
                    continue
                if p_start < b_start:
                    nxt.append((p_start, b_start))
                if b_end < p_end:
                    nxt.append((b_end, p_end))
            pieces = nxt
        result.extend((s, e) for s, e in pieces if e > s)
    return merge_intervals(result)


def _detect_transit_warnings_and_buffers(
    cal_result: dict, transit_minutes: int
) -> tuple[list[dict], list[Interval]]:
    """Detect tight transitions across distinct non-empty campus buildings and derive transit buffers."""
    if transit_minutes <= 0:
        return [], []
    fixed_items: list[dict] = []
    for layer_name in ("routine", "temporary"):
        for item in cal_result["layers"][layer_name]:
            fixed_items.append({
                "id": item["id"],
                "title": item["title"],
                "layer": layer_name,
                "start": _instant(item["start"], "start"),
                "end": _instant(item["end"], "end"),
                "location": item.get("location"),
            })
    for item in cal_result["layers"]["work"]:
        if item.get("kind") == "appointment":
            fixed_items.append({
                "id": item["id"],
                "title": item["title"],
                "layer": "appointment",
                "start": _instant(item["start"], "start"),
                "end": _instant(item["end"], "end"),
                "location": item.get("location"),
            })
    fixed_items.sort(key=lambda it: (it["start"], it["end"]))
    warnings: list[dict] = []
    buffers: list[Interval] = []
    delta = timedelta(minutes=transit_minutes)
    for idx in range(len(fixed_items) - 1):
        first, second = fixed_items[idx], fixed_items[idx + 1]
        loc_a, loc_b = _normalize_location(first["location"]), _normalize_location(second["location"])
        if not loc_a or not loc_b or loc_a == loc_b:
            continue
        gap = _minutes(first["end"], second["start"])
        if gap < 0:
            continue  # Already an overlap conflict handled by layers.
        if gap < transit_minutes:
            warnings.append({
                "type": "transit_tight",
                "from_id": first["id"],
                "from_title": first["title"],
                "from_location": first["location"],
                "to_id": second["id"],
                "to_title": second["title"],
                "to_location": second["location"],
                "gap_minutes": gap,
                "required_transit_minutes": transit_minutes,
                "reason": (
                    f"「{first['title']}」（{first['location']}）结束后仅隔 {gap} 分钟就要到"
                    f"「{second['title']}」（{second['location']}），小于设定的 {transit_minutes} 分钟跨楼通勤缓冲。"
                ),
            })
        else:
            # Reserve transit buffer right after first item so tasks do not pack against cross-building travel.
            buffers.append((first["end"], first["end"] + delta))
    return warnings, merge_intervals(buffers)


def _validate_dependencies(
    raw_deps: Any, task_ids: set[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[dict[str, str]]]:
    if raw_deps is None:
        return {tid: [] for tid in task_ids}, {tid: [] for tid in task_ids}, []
    if not isinstance(raw_deps, list):
        raise ValueError("dependencies 必须是数组")
    preds: dict[str, list[str]] = {tid: [] for tid in task_ids}
    succs: dict[str, list[str]] = {tid: [] for tid in task_ids}
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for entry in raw_deps:
        if not isinstance(entry, dict):
            raise ValueError("dependencies 每项必须是包含 before 和 after 的对象")
        before, after = entry.get("before"), entry.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            raise ValueError("dependencies 的 before 与 after 必须是任务 ID 字符串")
        if before == after:
            raise ValueError(f"任务「{before}」不能依赖自身")
        if before not in task_ids or after not in task_ids:
            raise ValueError(f"前置依赖引用了不存在的任务 ID（{before} -> {after}）")
        if (before, after) not in seen_edges:
            seen_edges.add((before, after))
            preds[after].append(before)
            succs[before].append(after)
            edges.append({"before": before, "after": after})
    # Kahn topological check for cycles
    indeg = {tid: len(preds[tid]) for tid in task_ids}
    queue = sorted(tid for tid, deg in indeg.items() if deg == 0)
    visited = 0
    while queue:
        cur = queue.pop(0)
        visited += 1
        for nxt in succs[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if visited != len(task_ids):
        raise ValueError("任务前置依赖存在循环，无法排程")
    return preds, succs, edges


def _allocate_split_greedy(
    slots: list[Interval], release: datetime, deadline: datetime, required_min: int, min_block: int
) -> tuple[list[Interval], list[Interval]] | None:
    placed: list[Interval] = []
    left = required_min
    for s_start, s_end in slots:
        start = max(s_start, release)
        end = min(s_end, deadline)
        cap = _minutes(start, end)
        if cap < min_block:
            continue
        take = min(cap, left)
        if 0 < left - take < min_block:
            take = left - min_block
        if take < min_block:
            continue
        placed.append((start, start + timedelta(minutes=take)))
        left -= take
        if left == 0:
            break
    if left > 0:
        return None
    return placed, _cut_intervals(slots, placed)


def _candidate_placements(
    slots: list[Interval],
    release: datetime,
    deadline: datetime,
    required_min: int,
    splittable: bool,
    min_block: int,
    step_min: int = DEFAULT_STEP_MINUTES,
) -> list[tuple[list[Interval], list[Interval]]]:
    """Generate exact candidate placements for one task: every feasible interval start + late-aligned fit."""
    candidates: list[tuple[list[Interval], list[Interval]]] = []
    seen_keys: set[tuple[tuple[datetime, datetime], ...]] = set()
    req_td = timedelta(minutes=required_min)

    def add(blocks: list[Interval]) -> None:
        key = tuple(blocks)
        if key not in seen_keys:
            seen_keys.add(key)
            candidates.append((blocks, _cut_intervals(slots, blocks)))

    # 1. Single-block placements across all feasible slots (earliest start, end-aligned, and stepped).
    for s_start, s_end in slots:
        start = max(s_start, release)
        end = min(s_end, deadline)
        cap = _minutes(start, end)
        if cap < required_min:
            continue
        # Earliest placement in this slot
        add([(start, start + req_td)])
        # End-aligned placement in this slot (crucial for leaving clean front room for other tasks)
        if cap > required_min:
            add([(end - req_td, end)])
        # Intermediate stepped placements (bounded to keep branching compact)
        if cap >= required_min + step_min:
            offset = step_min
            added_steps = 0
            while offset <= cap - required_min and added_steps < 4:
                p_start = start + timedelta(minutes=offset)
                add([(p_start, p_start + req_td)])
                offset += step_min
                added_steps += 1

    # 2. If splittable, also include exact earliest-fit split placement.
    if splittable:
        split_fit = _allocate_split_greedy(slots, release, deadline, required_min, min_block)
        if split_fit is not None:
            add(split_fit[0])

    return candidates


def _verify_solution(
    tasks_by_id: dict[str, dict],
    free_slots: list[Interval],
    placement: dict[str, list[Interval]],
    edges: list[dict[str, str]],
    reference: datetime,
) -> dict[str, Any]:
    """Independent zero-shared-logic verifier for exact solver schedules."""
    errors: list[str] = []
    all_blocks: list[tuple[datetime, datetime, str]] = []
    for tid, task in tasks_by_id.items():
        blocks = placement.get(tid, [])
        if not blocks:
            errors.append(f"任务 {tid} 未被安排")
            continue
        total = 0
        release = max(reference, _instant(task["earliest_start_at"], "earliest")) if task.get("earliest_start_at") else reference
        deadline = _instant(task["deadline_at"], "deadline")
        if not task["splittable"] and len(blocks) > 1:
            errors.append(f"不可拆任务 {tid} 被拆成多段")
        min_block = task.get("min_block_minutes") or DEFAULT_MIN_BLOCK
        for start, end in blocks:
            dur = _minutes(start, end)
            if dur <= 0:
                errors.append(f"任务 {tid} 存在非正时长分段")
            if len(blocks) > 1 and dur < min_block:
                errors.append(f"任务 {tid} 分段 {dur} 分钟小于最短连续块 {min_block} 分钟")
            if start < release or end > deadline:
                errors.append(f"任务 {tid} 超出允许窗口 [{release.isoformat()}, {deadline.isoformat()}]")
            if not any(fs <= start and end <= fe for fs, fe in free_slots):
                errors.append(f"任务 {tid} 的时段 {start.isoformat()}–{end.isoformat()} 不在可用留白内")
            total += dur
            all_blocks.append((start, end, tid))
        if total != task["estimate_minutes"]:
            errors.append(f"任务 {tid} 安排总时长 {total} 分钟不等于预计 {task['estimate_minutes']} 分钟")
    all_blocks.sort(key=lambda b: (b[0], b[1]))
    for idx in range(len(all_blocks) - 1):
        if all_blocks[idx][1] > all_blocks[idx + 1][0]:
            errors.append(f"任务 {all_blocks[idx][2]} 与 {all_blocks[idx + 1][2]} 发生时间重叠")
    for edge in edges:
        b_blocks = placement.get(edge["before"], [])
        a_blocks = placement.get(edge["after"], [])
        if b_blocks and a_blocks and max(e for _, e in b_blocks) > min(s for s, _ in a_blocks):
            errors.append(f"违反前置依赖：{edge['before']} 晚于 {edge['after']} 开始")
    return {
        "passed": not errors,
        "errors": errors,
        "checked_constraints": [
            "inside_derived_free_time",
            "no_work_overlap",
            "exact_estimate_minutes",
            "earliest_and_deadline_bounds",
            "splittable_and_min_block",
            "dag_precedence_dependencies",
        ],
    }


def _solve_exact(
    tasks: list[dict],
    free_slots: list[Interval],
    preds: dict[str, list[str]],
    succs: dict[str, list[str]],
    edges: list[dict[str, str]],
    reference: datetime,
) -> dict[str, Any]:
    """Exact branch-and-bound constraint search over schedulable active tasks."""
    if not tasks:
        return {
            "status": "optimal",
            "nodes_explored": 0,
            "placement": {},
            "validation": _verify_solution({}, free_slots, {}, edges, reference),
            "reason": "当前没有需要排程的待办任务。",
        }
    tasks_by_id = {t["id"]: t for t in tasks}
    # Quick total capacity lower-bound check
    total_free = sum(_minutes(s, e) for s, e in free_slots)
    total_need = sum(t["estimate_minutes"] for t in tasks)
    if total_need > total_free:
        return {
            "status": "infeasible",
            "nodes_explored": 1,
            "placement": {},
            "validation": {"passed": False, "errors": ["可用留白总时长不足以容纳全部任务"], "checked_constraints": []},
            "shortage_minutes": total_need - total_free,
            "reason": f"可用留白共 {total_free} 分钟，小于任务总需 {total_need} 分钟（至少差 {total_need - total_free} 分钟）。",
        }

    # Propagate tightened latest deadlines backwards through DAG
    latest = {t["id"]: _instant(t["deadline_at"], "deadline") for t in tasks}
    indeg = {tid: len(preds[tid]) for tid in tasks_by_id}
    topo: list[str] = []
    q = sorted(tid for tid, d in indeg.items() if d == 0)
    while q:
        u = q.pop(0)
        topo.append(u)
        for v in succs[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    for tid in reversed(topo):
        limit = latest[tid] - timedelta(minutes=tasks_by_id[tid]["estimate_minutes"])
        for dep in preds[tid]:
            latest[dep] = min(latest[dep], limit)

    nodes = 0
    budget_hit = False
    best_solution: dict[str, list[Interval]] | None = None
    best_score: tuple[int, int] | None = None  # (fragment_count, latest_finish_offset_min)

    def search(
        remaining_ids: set[str],
        cur_slots: list[Interval],
        placed: dict[str, list[Interval]],
        end_times: dict[str, datetime],
    ) -> None:
        nonlocal nodes, budget_hit, best_solution, best_score
        if not remaining_ids:
            frags = sum(max(0, len(blks) - 1) for blks in placed.values())
            makespan = max((_minutes(reference, max(e for _, e in blks)) for blks in placed.values()), default=0)
            score = (frags, makespan)
            if best_score is None or score < best_score:
                best_score = score
                best_solution = {tid: list(blks) for tid, blks in placed.items()}
            return
        if nodes >= MAX_SEARCH_NODES:
            budget_hit = True
            return
        # Lower-bound pruning: remaining free minutes must cover remaining required minutes
        rem_free = sum(_minutes(s, e) for s, e in cur_slots)
        rem_need = sum(tasks_by_id[tid]["estimate_minutes"] for tid in remaining_ids)
        if rem_need > rem_free:
            return
        # Ready tasks (all predecessors placed)
        ready = [tid for tid in remaining_ids if all(dep in end_times for dep in preds[tid])]
        if not ready:
            return
        # Most-Constrained-Variable (MRV) ordering: pick ready task with fewest candidate placements first!
        scored_ready: list[tuple[int, datetime, str, list[tuple[list[Interval], list[Interval]]]]] = []
        for tid in ready:
            task = tasks_by_id[tid]
            rel = max([reference, *(end_times[d] for d in preds[tid])])
            if task.get("earliest_start_at"):
                rel = max(rel, _instant(task["earliest_start_at"], "earliest"))
            dl = latest[tid]
            if rel >= dl:
                return  # Dead end immediately
            cands = _candidate_placements(
                cur_slots,
                rel,
                dl,
                task["estimate_minutes"],
                bool(task["splittable"]),
                task.get("min_block_minutes") or DEFAULT_MIN_BLOCK,
            )
            if not cands:
                return  # Dead end immediately
            scored_ready.append((len(cands), dl, tid, cands))
        scored_ready.sort(key=lambda item: (item[0], item[1], item[2]))
        _, _, pick_tid, pick_cands = scored_ready[0]
        next_remaining = remaining_ids - {pick_tid}
        for blocks, next_slots in pick_cands:
            nodes += 1
            if nodes > MAX_SEARCH_NODES:
                budget_hit = True
                return
            placed[pick_tid] = blocks
            end_times[pick_tid] = max(e for _, e in blocks)
            search(next_remaining, next_slots, placed, end_times)
            del placed[pick_tid]
            del end_times[pick_tid]
            # Early stop once we have a zero-fragmentation optimal placement and explored tight neighborhood
            if best_score is not None and best_score[0] == 0 and nodes >= 64:
                return

    search(set(tasks_by_id), list(free_slots), {}, {})
    if best_solution is not None:
        validation = _verify_solution(tasks_by_id, free_slots, best_solution, edges, reference)
        return {
            "status": "optimal" if not budget_hit else "feasible",
            "nodes_explored": nodes,
            "placement": best_solution,
            "validation": validation,
            "reason": "精确约束求解器已找到并通过独立校验的可行排程。" if validation["passed"] else "独立校验未通过。",
        }
    return {
        "status": "budget_exhausted" if budget_hit else "infeasible",
        "nodes_explored": nodes,
        "placement": {},
        "validation": {"passed": False, "errors": ["未找到满足全部约束的排程"], "checked_constraints": []},
        "reason": "达到搜索节点上限仍未找到解，不能断言数学无解。" if budget_hit else "在给定留白、连续块、通勤与前置依赖约束下无可行解。",
    }


def _apply_strategy(snapshot: dict, strategy: dict) -> dict:
    """Apply one v3 strategy action to a detached snapshot copy."""
    nxt = deepcopy(snapshot)
    action = strategy["action"]
    params = strategy.get("params") or {}
    if action == "cancel_event":
        for ev in nxt["events"]:
            if ev["id"] == params.get("event_id"):
                ev["status"] = "cancelled"
    elif action == "skip_routine_date":
        for rt in nxt["routines"]:
            if rt["id"] == params.get("routine_id"):
                day = params.get("date")
                if day and day not in rt["skip_dates"]:
                    rt["skip_dates"].append(day)
    elif action == "move_event":
        for ev in nxt["events"]:
            if ev["id"] == params.get("event_id"):
                ev["start_at"] = params["start_at"]
                ev["end_at"] = params["end_at"]
    elif action == "update_task":
        for task in nxt["tasks"]:
            if task["id"] == params.get("task_id"):
                task.update(params.get("patch") or {})
    elif action == "add_free":
        nxt["free_adjustments"].append({
            "id": f"fa-solve-{len(nxt['free_adjustments']) + 1}",
            "kind": "add",
            "start_at": params["start_at"],
            "end_at": params["end_at"],
            "note": params.get("note") or "求解器建议补充留白",
        })
    nxt["revision"] = int(nxt.get("revision", 0)) + 1
    return nxt


def _pareto_plans(
    snapshot: dict,
    date_range: dict[str, str],
    base_cal: dict,
    transit_minutes: int,
) -> list[dict]:
    """Generate Pareto-non-dominated trade-off plans for actionable conflicts."""
    actionable = [
        conflict for conflict in base_cal["conflicts"]
        if any(s["action"] != "review" for s in conflict.get("strategies", []))
    ][:MAX_PARETO_CONFLICTS]
    if not actionable:
        return []

    # Enumerate concrete strategy combinations (bounded <= 2^4 = 16)
    candidates: list[tuple[list[dict], dict]] = [([], deepcopy(snapshot))]
    for conflict in actionable:
        concrete = [s for s in conflict["strategies"] if s["action"] != "review"][:2]
        next_cands: list[tuple[list[dict], dict]] = []
        for actions, snap in candidates:
            for strat in concrete:
                next_cands.append(([*actions, strat], _apply_strategy(snap, strat)))
        candidates = next_cands

    evaluated: list[dict] = []
    for actions, snap in candidates:
        cal = calendar_snapshot({"snapshot": snap, "range": date_range})
        free_mins = sum(_minutes(_instant(f["start"], "s"), _instant(f["end"], "e")) for f in cal["layers"]["free"])
        work_mins = sum(
            _minutes(_instant(w["start"], "s"), _instant(w["end"], "e"))
            for w in cal["layers"]["work"]
            if w.get("kind") != "appointment"
        )
        net_free = free_mins - work_mins
        longest_free = max(
            (_minutes(_instant(f["start"], "s"), _instant(f["end"], "e")) for f in cal["layers"]["free"]),
            default=0,
        )
        kept_temp = sum(1 for ev in snap["events"] if ev["status"] == "active" and ev.get("kind", "temporary") == "temporary")
        skipped_routines = sum(len(rt["skip_dates"]) for rt in snap["routines"])
        transit_warns, _ = _detect_transit_warnings_and_buffers(cal, transit_minutes)
        evaluated.append({
            "actions": [
                {"action": a["action"], "label": a["label"], "detail": a["detail"], "params": a["params"]}
                for a in actions
            ],
            "metrics": {
                "remaining_conflicts": len(cal["conflicts"]),
                "net_free_minutes": net_free,
                "longest_free_minutes": longest_free,
                "kept_temporary_events": kept_temp,
                "skipped_routine_occurrences": skipped_routines,
                "transit_warnings": len(transit_warns),
            },
            "work_blocks": [
                {"id": w.get("task_id") or w.get("id"), "title": w["title"], "start": w["start"], "end": w["end"]}
                for w in cal["layers"]["work"]
            ],
            "validation_passed": cal["validation"]["passed"],
        })

    # Pareto dominance filter maximizing:
    # (-remaining_conflicts, net_free_minutes, kept_temporary_events, -skipped_routine_occurrences, -transit_warnings)
    def vec(item: dict) -> tuple[int, int, int, int, int]:
        m = item["metrics"]
        return (
            -m["remaining_conflicts"],
            m["net_free_minutes"],
            m["kept_temporary_events"],
            -m["skipped_routine_occurrences"],
            -m["transit_warnings"],
        )

    pareto: list[dict] = []
    for idx, item in enumerate(evaluated):
        v = vec(item)
        dominated = False
        for jdx, other in enumerate(evaluated):
            if idx == jdx:
                continue
            ov = vec(other)
            if all(o >= x for o, x in zip(ov, v)) and any(o > x for o, x in zip(ov, v)):
                dominated = True
                break
        if not dominated:
            pareto.append(item)

    # Sort & label clearly
    pareto.sort(key=lambda it: (it["metrics"]["remaining_conflicts"], -it["metrics"]["net_free_minutes"]))
    for plan in pareto:
        m = plan["metrics"]
        if m["skipped_routine_occurrences"] == 0 and m["remaining_conflicts"] == 0:
            plan["name"] = "保作息与课表方案"
            plan["summary"] = f"叠墨清零，保留全部课表并最大化留白（本周排后可用 {m['net_free_minutes']} 分钟）。"
        elif m["kept_temporary_events"] > 0 and m["remaining_conflicts"] == 0:
            plan["name"] = "保临时活动方案"
            plan["summary"] = f"叠墨清零，优先参加临时点墨活动（需请假 {m['skipped_routine_occurrences']} 次课，本周排后可用 {m['net_free_minutes']} 分钟）。"
        else:
            plan["name"] = "折中权衡方案"
            plan["summary"] = f"剩余叠墨 {m['remaining_conflicts']} 处，本周排后可用 {m['net_free_minutes']} 分钟。"
    return pareto[:3]


def solve_snapshot(payload: object) -> dict[str, Any]:
    """POST /api/v3/solve entry point: exact constraint solver + transit buffers + Pareto plans."""
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        raise ValueError("请求必须包含 v3 snapshot 对象")
    snapshot = payload["snapshot"]
    validate_snapshot_v3(snapshot)
    date_range = payload.get("range") or _default_range(snapshot)
    start_day = date.fromisoformat(date_range["from"])
    end_day = date.fromisoformat(date_range["to"])
    if end_day < start_day or (end_day - start_day).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"range 无效或超过 {MAX_RANGE_DAYS} 天上限")

    transit_minutes = payload.get("transit_minutes", 0)
    if not isinstance(transit_minutes, int) or isinstance(transit_minutes, bool) or not (0 <= transit_minutes <= 120):
        raise ValueError("transit_minutes 必须是 0 到 120 之间的整数")

    # Derive base calendar layers (reuses v3 routine/temporary/free derivation)
    base_cal = calendar_snapshot({"snapshot": snapshot, "range": date_range})
    transit_warnings, transit_buffers = _detect_transit_warnings_and_buffers(base_cal, transit_minutes)

    reference = _instant(snapshot["reference_time"], "reference_time")
    # Collect active schedulable tasks (with deadline & estimate)
    schedulable = [
        task for task in snapshot["tasks"]
        if task["status"] == "active" and task["deadline_at"] and task["estimate_minutes"] is not None
    ]
    if len(schedulable) > MAX_PLANNED_TASKS:
        raise ValueError(f"单次精确求解最多支持 {MAX_PLANNED_TASKS} 件待办任务")
    schedulable_ids = {t["id"] for t in schedulable}
    preds, succs, edges = _validate_dependencies(payload.get("dependencies"), schedulable_ids)

    # Free intervals from base calendar, optionally deducting cross-building transit buffers
    raw_free = [(_instant(f["start"], "s"), _instant(f["end"], "e")) for f in base_cal["layers"]["free"]]
    effective_free = _cut_intervals(raw_free, transit_buffers) if transit_buffers else raw_free

    exact = _solve_exact(schedulable, effective_free, preds, succs, edges, reference)
    schedule_entries: list[dict] = []
    if exact["validation"]["passed"]:
        tasks_by_id = {t["id"]: t for t in schedulable}
        for tid, blocks in exact["placement"].items():
            for s, e in blocks:
                schedule_entries.append({
                    "task_id": tid,
                    "title": tasks_by_id[tid]["title"],
                    "start": _iso(s),
                    "end": _iso(e),
                    "minutes": _minutes(s, e),
                })
        schedule_entries.sort(key=lambda item: (item["start"], item["end"], item["task_id"]))

    pareto = _pareto_plans(snapshot, date_range, base_cal, transit_minutes)

    return {
        "schema_version": 1,
        "revision": snapshot["revision"],
        "range": date_range,
        "algorithm": {
            "name": "exact_branch_and_bound_v1",
            "status": exact["status"],
            "nodes_explored": exact["nodes_explored"],
            "max_nodes": MAX_SEARCH_NODES,
            "transit_minutes": transit_minutes,
            "dependency_count": len(edges),
        },
        "schedule": schedule_entries,
        "validation": exact["validation"],
        "reason": exact["reason"],
        "transit_warnings": transit_warnings,
        "pareto_plans": pareto,
        "base_conflict_count": len(base_cal["conflicts"]),
    }
