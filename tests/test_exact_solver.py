"""Tests for exact constraint solver, cross-building transit buffers, DAG dependencies and Pareto plans."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from campus_agent.exact_solver import solve_snapshot
from campus_agent.layers import calendar_snapshot
from campus_agent.server import process_post

ROOT = Path(__file__).resolve().parents[1]


def _base_snapshot(reference_time="2026-11-25T13:30:00+08:00", wed_window=("14:00", "18:00")):
    return {
        "schema_version": 3,
        "revision": 1,
        "timezone": "Asia/Shanghai",
        "reference_time": reference_time,
        "sources": [{"id": "s1", "kind": "synthetic", "text": "测试", "published_at": None}],
        "daily_window": [
            {"start": "08:00", "end": "22:00"},
            {"start": "08:00", "end": "22:00"},
            {"start": wed_window[0], "end": wed_window[1]},
            {"start": "08:00", "end": "22:00"},
            {"start": "08:00", "end": "22:00"},
            {"start": "09:00", "end": "22:00"},
            {"start": "09:00", "end": "22:00"},
        ],
        "routines_confirmed": True,
        "routines": [],
        "free_adjustments": [],
        "events": [],
        "tasks": [],
    }


def _task(tid, title, deadline, estimate, *, splittable=True, earliest=None):
    return {
        "id": tid,
        "title": title,
        "deadline_at": deadline,
        "estimate_minutes": estimate,
        "splittable": splittable,
        "min_block_minutes": 30,
        "earliest_start_at": earliest,
        "priority": None,
        "status": "active",
        "location": None,
        "materials": [],
        "source_id": "s1",
        "evidence": [],
        "notes": [],
    }


class ExactSolverTests(unittest.TestCase):
    def test_solves_greedy_trap_where_all_heuristics_fail(self):
        # Bin 1: 14:00-15:40 (100m), Bin 2: 16:00-17:30 (90m), separated by a 20m fixed meeting.
        # Four unsplit tasks with identical deadline 17:30: 60m, 50m, 50m, 30m (sum = 190m = exact capacity).
        # Every greedy heuristic (deadline/tight_window/long_first/priority) puts 60m into Bin 1 (leaving 40m),
        # then 50m into Bin 2 (leaving 40m), stranding the second 50m task!
        # Exact branch-and-bound puts 50m+50m in Bin 1 and 60m+30m in Bin 2 -> optimal & verified!
        snap = _base_snapshot(wed_window=("14:00", "17:30"))
        snap["events"].append({
            "id": "e-mid",
            "title": "短会",
            "start_at": "2026-11-25T15:40:00+08:00",
            "end_at": "2026-11-25T16:00:00+08:00",
            "location": "学活204",
            "status": "active",
            "source_id": "s1",
            "evidence": [],
            "notes": [],
        })
        snap["tasks"] = [
            _task("t-1", "任务60", "2026-11-25T17:30:00+08:00", 60, splittable=False),
            _task("t-2", "任务50A", "2026-11-25T17:30:00+08:00", 50, splittable=False),
            _task("t-3", "任务50B", "2026-11-25T17:30:00+08:00", 50, splittable=False),
            _task("t-4", "任务30", "2026-11-25T17:30:00+08:00", 30, splittable=False),
        ]
        greedy = calendar_snapshot({"snapshot": snap, "range": {"from": "2026-11-25", "to": "2026-11-25"}})
        self.assertEqual(greedy["status"], "conflicts")
        solved = process_post("/api/v3/solve", {"snapshot": snap, "range": {"from": "2026-11-25", "to": "2026-11-25"}})
        self.assertEqual(solved["algorithm"]["status"], "optimal")
        self.assertTrue(solved["validation"]["passed"])
        self.assertEqual(len(solved["schedule"]), 4)
        self.assertEqual(sum(item["minutes"] for item in solved["schedule"]), 190)

    def test_enforces_dag_dependencies_and_rejects_cycles(self):
        snap = _base_snapshot(wed_window=("14:00", "17:00"))
        # Task 2 has earlier deadline than Task 1, but Task 1 MUST happen before Task 2
        snap["tasks"] = [
            _task("t-buy", "先买材料", "2026-11-25T17:00:00+08:00", 30, splittable=False),
            _task("t-submit", "再交表", "2026-11-25T16:00:00+08:00", 30, splittable=False),
        ]
        solved = solve_snapshot({
            "snapshot": snap,
            "range": {"from": "2026-11-25", "to": "2026-11-25"},
            "dependencies": [{"before": "t-buy", "after": "t-submit"}],
        })
        self.assertTrue(solved["validation"]["passed"])
        slots = {item["task_id"]: (item["start"], item["end"]) for item in solved["schedule"]}
        self.assertLessEqual(slots["t-buy"][1], slots["t-submit"][0])
        with self.assertRaisesRegex(ValueError, "循环"):
            solve_snapshot({
                "snapshot": snap,
                "range": {"from": "2026-11-25", "to": "2026-11-25"},
                "dependencies": [{"before": "t-buy", "after": "t-submit"}, {"before": "t-submit", "after": "t-buy"}],
            })

    def test_cross_building_transit_warning_and_buffer(self):
        snap = _base_snapshot(wed_window=("14:00", "18:00"))
        snap["routines"].append({
            "id": "r1",
            "title": "专业课",
            "weekdays": [3],
            "start": "14:00",
            "end": "15:00",
            "from": "2026-09-07",
            "until": None,
            "week_interval": 1,
            "skip_dates": [],
            "location": "新闻楼402",
            "source_id": "s1",
        })
        snap["events"].append({
            "id": "e1",
            "title": "招新说明会",
            "start_at": "2026-11-25T15:05:00+08:00",
            "end_at": "2026-11-25T16:00:00+08:00",
            "location": "学活204",
            "status": "active",
            "source_id": "s1",
            "evidence": [],
            "notes": [],
        })
        solved = solve_snapshot({
            "snapshot": snap,
            "range": {"from": "2026-11-25", "to": "2026-11-25"},
            "transit_minutes": 15,
        })
        self.assertEqual(len(solved["transit_warnings"]), 1)
        self.assertEqual(solved["transit_warnings"][0]["gap_minutes"], 5)
        self.assertIn("新闻楼402", solved["transit_warnings"][0]["reason"])
        self.assertIn("学活204", solved["transit_warnings"][0]["reason"])

    def test_pareto_plans_on_photo_club_conflict(self):
        snap = json.loads((ROOT / "examples/roadshow-photo-club.snapshot.json").read_text(encoding="utf-8"))
        snap = deepcopy(snap)
        snap["events"].append({
            "id": "e-photo",
            "title": "摄影社招新说明会",
            "start_at": "2026-11-25T14:30:00+08:00",
            "end_at": "2026-11-25T15:30:00+08:00",
            "location": "学活204",
            "status": "active",
            "source_id": snap["sources"][0]["id"],
            "evidence": [],
            "notes": [],
        })
        snap["tasks"].append(_task("t-photo", "提交社团报名表", "2026-11-25T18:00:00+08:00", 30))
        snap["tasks"][0]["source_id"] = snap["sources"][0]["id"]
        solved = solve_snapshot({"snapshot": snap, "range": {"from": "2026-11-23", "to": "2026-11-29"}})
        self.assertEqual(solved["base_conflict_count"], 1)
        self.assertEqual(len(solved["pareto_plans"]), 2)
        names = [p["name"] for p in solved["pareto_plans"]]
        self.assertIn("保作息与课表方案", names)
        self.assertIn("保临时活动方案", names)
        # Keep-course plan has 30 more net free minutes than skip-course plan
        keep_plan = next(p for p in solved["pareto_plans"] if p["name"] == "保作息与课表方案")
        event_plan = next(p for p in solved["pareto_plans"] if p["name"] == "保临时活动方案")
        self.assertEqual(keep_plan["metrics"]["skipped_routine_occurrences"], 0)
        self.assertEqual(keep_plan["metrics"]["kept_temporary_events"], 0)
        self.assertEqual(event_plan["metrics"]["skipped_routine_occurrences"], 1)
        self.assertEqual(event_plan["metrics"]["kept_temporary_events"], 1)
        self.assertEqual(keep_plan["metrics"]["net_free_minutes"], event_plan["metrics"]["net_free_minutes"])
        self.assertEqual(keep_plan["metrics"]["remaining_conflicts"], 0)
        self.assertEqual(event_plan["metrics"]["remaining_conflicts"], 0)


if __name__ == "__main__":
    unittest.main()
