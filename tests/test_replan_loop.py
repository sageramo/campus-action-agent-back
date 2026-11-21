"""Tests for Backend 1.29 closed-loop replanning, calendar-replay consistency, moved_minutes +
minimal_change comparison, needs_decision gate, KINDS + resting starter, and 3 fixed golden fixtures.
"""
from __future__ import annotations

from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from campus_agent.replan import ReplanConflictError, _preview_token, snapshot_fingerprint
from campus_agent.server import CampusAgentHandler, process_post

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_PATH = ROOT / "examples" / "replan-fixtures.v1.json"


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


class ReplanLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_doc = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CampusAgentHandler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_health_declares_129_replan_and_focus_capabilities(self) -> None:
        with urlopen(f"{self.base_url}/api/health", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["version"], "1.29")
        self.assertIn("replan_loop_v1", data["capabilities"])
        self.assertIn("focus_cognitive_scheduling_v1", data["capabilities"])

    def test_fixture_1_success_replan_confirm_and_calendar_exact_block_match(self) -> None:
        fx = self.fixture_doc["fixtures"]["success_replan"]
        status, preview = _post_json(f"{self.base_url}/api/v3/replan/preview", fx["preview_request"])
        self.assertEqual(status, 200)
        self.assertEqual(preview["status"], "ready")
        self.assertTrue(preview["feasible"])
        self.assertTrue(preview["confirmable"])
        self.assertTrue(preview["needs_decision"])
        self.assertIn("取舍", preview["decision_reason"])
        self.assertEqual(preview["base_revision"], 4)
        self.assertEqual(preview["snapshot_fingerprint"], fx["preview_response"]["snapshot_fingerprint"])

        # Policy: "先保底线，再少改，最后看专注" -> both pass baseline, so buffer_first (260m) is recommended over focus_matched (710m)
        self.assertEqual(preview["recommended_plan_id"], "buffer_first")
        plan_ids = [p["plan_id"] for p in preview["candidate_plans"]]
        self.assertEqual(plan_ids, ["buffer_first", "focus_matched"])
        buffer_plan, focus_plan = preview["candidate_plans"]
        self.assertTrue(buffer_plan["recommended"])
        self.assertFalse(focus_plan["recommended"])
        self.assertTrue(buffer_plan["metrics"]["passes_baseline"])
        self.assertTrue(focus_plan["metrics"]["passes_baseline"])
        self.assertEqual(buffer_plan["metrics"]["moved_minutes"], 260)
        self.assertEqual(focus_plan["metrics"]["moved_minutes"], 710)
        self.assertIsNone(buffer_plan["tradeoff_vs_recommended"])
        self.assertIn("多改 450 分钟", focus_plan["tradeoff_vs_recommended"])
        self.assertIn("整理社团招新素材", focus_plan["tradeoff_vs_recommended"])
        self.assertGreater(focus_plan["metrics"]["focus_score"], buffer_plan["metrics"]["focus_score"])
        self.assertGreater(
            buffer_plan["metrics"]["min_deadline_buffer_minutes"],
            focus_plan["metrics"]["min_deadline_buffer_minutes"],
        )
        self.assertEqual(len(preview["omitted_plans"]), 1)
        self.assertEqual(preview["omitted_plans"][0]["plan_id"], "minimal_change")
        self.assertEqual(preview["omitted_plans"][0]["same_as"], "buffer_first")

        # Hard constraint check: locked lunch rest (12:30-14:00) is verified & untouched
        self.assertIn("respects_locked_rest_blocks", buffer_plan["validation"]["checked_constraints"])
        for blk in buffer_plan["schedule"]:
            self.assertFalse(blk["start"] < "2026-11-25T14:00:00+08:00" and blk["end"] > "2026-11-25T12:30:00+08:00")

        # Specific KINDS starter (作品 for earliest task t-slides in buffer_first) & resting=False
        starter = preview["five_minute_starter"]
        self.assertIsNotNone(starter)
        self.assertEqual(starter["kind"], "作品")
        self.assertFalse(starter["resting"])
        self.assertIn("素材文件夹", starter["action"])

        # Confirm recommended plan (buffer_first) round-trip succeeds & matches /api/v3/calendar block-for-block
        c_status, confirmed = _post_json(f"{self.base_url}/api/v3/replan/confirm", fx["confirm_request"])
        self.assertEqual(c_status, 200)
        self.assertTrue(confirmed["confirmed"])
        self.assertTrue(confirmed["calendar_match"]["matched"])
        self.assertEqual((confirmed["previous_revision"], confirmed["new_revision"]), (4, 5))

        cal_status, cal = _post_json(
            f"{self.base_url}/api/v3/calendar",
            {"snapshot": confirmed["snapshot"], "range": fx["preview_request"]["range"]},
        )
        self.assertEqual(cal_status, 200)
        self.assertEqual(cal["conflicts"], [])
        actual_by_task: dict[str, list[tuple[str, str]]] = {}
        for blk in cal["layers"]["work"]:
            if blk.get("kind") != "appointment":
                actual_by_task.setdefault(blk["task_id"], []).append((blk["start"], blk["end"]))
        expected_by_task = {
            c["task_id"]: [(b["start"], b["end"]) for b in c["after"]]
            for c in buffer_plan["task_changes"]
        }
        self.assertEqual(actual_by_task, expected_by_task)
        self.assertEqual(actual_by_task["t-math-hw"], [("2026-11-25T14:00:00+08:00", "2026-11-25T15:30:00+08:00")])
        self.assertEqual(actual_by_task["t-slides"], [("2026-11-25T11:30:00+08:00", "2026-11-25T12:30:00+08:00")])

        # Also confirm alternative plan (focus_matched) and verify /api/v3/calendar matches 19:00-20:00 block-for-block!
        alt_confirm_req = {
            "snapshot": fx["preview_request"]["snapshot"],
            "range": fx["preview_request"]["range"],
            "base_revision": preview["base_revision"],
            "snapshot_fingerprint": preview["snapshot_fingerprint"],
            "plan_id": focus_plan["plan_id"],
            "preview_token": focus_plan["preview_token"],
            "preview_snapshot": focus_plan["preview_snapshot"],
            "expected_schedule": focus_plan["expected_schedule"],
        }
        ac_status, alt_confirmed = _post_json(f"{self.base_url}/api/v3/replan/confirm", alt_confirm_req)
        self.assertEqual(ac_status, 200)
        _, alt_cal = _post_json(
            f"{self.base_url}/api/v3/calendar",
            {"snapshot": alt_confirmed["snapshot"], "range": fx["preview_request"]["range"]},
        )
        self.assertEqual(alt_cal["conflicts"], [])
        alt_work = {b["task_id"]: (b["start"], b["end"]) for b in alt_cal["layers"]["work"] if b.get("kind") != "appointment"}
        self.assertEqual(alt_work["t-slides"], ("2026-11-25T19:00:00+08:00", "2026-11-25T20:00:00+08:00"))

    def test_confirm_rejects_when_calendar_replay_diverges_from_preview_after(self) -> None:
        fx = self.fixture_doc["fixtures"]["success_replan"]
        # Use focus_matched (t-slides at 19:00-20:00), strip earliest_start_at so /api/v3/calendar puts t-slides at 11:30
        focus_plan = next(p for p in fx["preview_response"]["candidate_plans"] if p["plan_id"] == "focus_matched")
        broken_preview_snap = deepcopy(focus_plan["preview_snapshot"])
        for t in broken_preview_snap["tasks"]:
            t["earliest_start_at"] = None
        exp_sched = focus_plan["expected_schedule"]
        req = {
            **fx["confirm_request"],
            "plan_id": focus_plan["plan_id"],
            "preview_snapshot": broken_preview_snap,
            "expected_schedule": exp_sched,
            "preview_token": _preview_token(
                fx["confirm_request"]["base_revision"],
                fx["confirm_request"]["snapshot_fingerprint"],
                focus_plan["plan_id"],
                broken_preview_snap,
                exp_sched,
            ),
        }
        status, body = _post_json(f"{self.base_url}/api/v3/replan/confirm", req)
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "schedule_mismatch")
        self.assertTrue(any(m["task_id"] == "t-slides" for m in body["mismatches"]))

    def test_baseline_fallback_recommends_largest_buffer_and_states_shortage_when_none_passes(self) -> None:
        # Task due at 17:15 with 90m estimate; after disruption 10:00-15:30 only 15:30-17:00 fits -> buffer=15m < 30m default floor
        snap = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"]["snapshot"])
        snap["tasks"] = [snap["tasks"][0]]
        snap["tasks"][0]["deadline_at"] = "2026-11-25T17:15:00+08:00"
        preview = process_post("/api/v3/replan/preview", {
            "snapshot": snap,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
            "disruptions": [
                {"id": "e-long", "title": "全天排练", "start_at": "2026-11-25T10:00:00+08:00", "end_at": "2026-11-25T15:30:00+08:00"},
            ],
            "task_profiles": {"t-math-hw": {"cognitive_load": "high"}},
        })
        self.assertEqual(preview["status"], "ready")
        self.assertTrue(preview["needs_decision"])
        self.assertIn("余量不足底线", preview["decision_reason"])
        self.assertFalse(preview["candidate_plans"][0]["metrics"]["passes_baseline"])
        self.assertEqual(preview["candidate_plans"][0]["metrics"]["min_deadline_buffer_minutes"], 15)

    def test_calibrated_buffer_floor_and_high_cognitive_low_focus_rejection(self) -> None:
        # With 1.5x inflation factor on a 90m task -> calibrated buffer floor = (1.5 - 1) * 90 = 45m.
        # Also verify high-load task placed inside a low focus window fails baseline.
        snap = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"]["snapshot"])
        snap["tasks"] = [snap["tasks"][0]]
        snap["tasks"][0]["deadline_at"] = "2026-11-25T17:40:00+08:00"  # 15:30-17:00 leaves 40m buffer (< 45m calibrated floor!)
        preview = process_post("/api/v3/replan/preview", {
            "snapshot": snap,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
            "disruptions": [
                {"id": "e-long", "title": "临时会", "start_at": "2026-11-25T10:00:00+08:00", "end_at": "2026-11-25T15:30:00+08:00"},
            ],
            "focus_windows": [
                {"date": "2026-11-25", "start": "15:30", "end": "17:00", "level": "low", "label": "低效疲惫期"},
            ],
            "task_profiles": {"t-math-hw": {"cognitive_load": "high"}},
            "calibration_history": [
                {"title": "线性代数习题", "kind": "作业", "estimated_minutes": 60, "actual_minutes": 90},
                {"title": "微积分作业", "kind": "作业", "estimated_minutes": 60, "actual_minutes": 90},
            ],
        })
        m = preview["candidate_plans"][0]["metrics"]
        self.assertEqual(m["required_buffer_floor_minutes"], 45)
        self.assertFalse(m["passes_baseline"])
        self.assertTrue(any("低于底线 45 分钟" in v for v in m["baseline_violations"]))
        self.assertTrue(any("落入低专注时段" in v for v in m["baseline_violations"]))

    def test_high_cognitive_task_after_locked_rest_passes_baseline_and_prefers_smaller_move(self) -> None:
        # Claude regression: Wed 14:00-22:00 free, routine 14:00-15:00, disruption 15:00-16:00, locked rest 16:30-17:30,
        # high focus 19:00-21:00; 报告 90m high splittable due Thu 12:00; 报名表 30m low due Wed 18:00.
        # Both buffer_first (报告 17:30-19:00, moved=180m) and focus_matched (报告 19:00-20:30, moved=270m) have
        # min_deadline_buffer=90m and pass baseline -> must recommend buffer_first (moved 180m < 270m).
        snap = {
            "schema_version": 3, "revision": 1, "timezone": "Asia/Shanghai",
            "reference_time": "2026-11-25T13:30:00+08:00", "sources": [],
            "daily_window": [
                {"start": "08:00", "end": "22:00"},
                {"start": "08:00", "end": "22:00"},
                {"start": "14:00", "end": "22:00"},
                {"start": "08:00", "end": "22:00"},
                {"start": "08:00", "end": "22:00"},
                {"start": "09:00", "end": "22:00"},
                {"start": "09:00", "end": "22:00"},
            ],
            "routines_confirmed": True,
            "routines": [{
                "id": "r-major", "title": "专业课", "weekdays": [3], "start": "14:00", "end": "15:00",
                "from": "2026-09-07", "until": None, "week_interval": 1, "skip_dates": [],
                "location": "新闻楼402", "source_id": None,
            }],
            "free_adjustments": [], "events": [],
            "tasks": [
                {
                    "id": "t-signup", "title": "提交报名表", "deadline_at": "2026-11-25T18:00:00+08:00",
                    "estimate_minutes": 30, "splittable": True, "min_block_minutes": 30,
                    "earliest_start_at": None, "priority": 70, "status": "active",
                    "location": None, "materials": [], "source_id": None, "evidence": [], "notes": [],
                },
                {
                    "id": "t-report", "title": "撰写课程报告", "deadline_at": "2026-11-26T12:00:00+08:00",
                    "estimate_minutes": 90, "splittable": True, "min_block_minutes": 30,
                    "earliest_start_at": None, "priority": 80, "status": "active",
                    "location": None, "materials": [], "source_id": None, "evidence": [], "notes": [],
                },
            ],
        }
        preview = process_post("/api/v3/replan/preview", {
            "snapshot": snap,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
            "disruptions": [
                {"id": "e-brief", "title": "临时说明会", "start_at": "2026-11-25T15:00:00+08:00", "end_at": "2026-11-25T16:00:00+08:00"},
            ],
            "rest_blocks": [
                {"id": "rest-pm", "label": "傍晚休息", "start": "2026-11-25T16:30:00+08:00", "end": "2026-11-25T17:30:00+08:00", "locked": True},
            ],
            "focus_windows": [
                {"date": "2026-11-25", "start": "19:00", "end": "21:00", "level": "high", "label": "晚间专注"},
            ],
            "task_profiles": {"t-report": {"cognitive_load": "high"}, "t-signup": {"cognitive_load": "low"}},
        })
        self.assertEqual(preview["recommended_plan_id"], "buffer_first")
        by_id = {p["plan_id"]: p for p in preview["candidate_plans"]}
        self.assertTrue(by_id["buffer_first"]["metrics"]["passes_baseline"])
        self.assertTrue(by_id["focus_matched"]["metrics"]["passes_baseline"])
        self.assertEqual(by_id["buffer_first"]["metrics"]["min_deadline_buffer_minutes"], 90)
        self.assertEqual(by_id["focus_matched"]["metrics"]["min_deadline_buffer_minutes"], 90)
        self.assertEqual(by_id["buffer_first"]["metrics"]["moved_minutes"], 180)
        self.assertEqual(by_id["focus_matched"]["metrics"]["moved_minutes"], 270)
        rep_change = next(c for c in by_id["buffer_first"]["task_changes"] if c["task_id"] == "t-report")
        self.assertEqual(rep_change["after"][0]["start"], "2026-11-25T17:30:00+08:00")
        self.assertEqual(rep_change["after"][0]["end"], "2026-11-25T19:00:00+08:00")

    def test_three_distinct_plans_focus_buffer_and_minimal_change(self) -> None:
        # Baseline: t-math-hw at 10:00-11:30 (disrupted), t-slides originally at 16:00-17:00 (due 22:00).
        # Focus windows: 14:00-16:00 high, 19:00-21:00 low.
        # -> focus_matched puts t-slides in 19:00-20:00 (high focus score)
        # -> buffer_first puts t-slides in 11:30-12:30 (max deadline buffer)
        # -> minimal_change keeps t-slides at 16:00-17:00 (0m moved for t-slides!)
        snap = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"]["snapshot"])
        snap["tasks"][1]["earliest_start_at"] = None
        # Add an existing routine 11:30-16:00 on baseline? Better: keep 11:30-12:30 free,
        # give t-slides baseline at 16:00-17:00 by placing a 10:00-11:30 free + 11:30-16:00 occupied?
        # Even simpler: baseline has t-math-hw (09:50-11:20) and an appointment 11:20-16:00 -> t-slides at 16:00-17:00;
        # wait: if appointment is 11:20-16:00 then buffer_first can't use 11:30.
        # Instead: t-math-hw (90m, due 21:00, high) + t-slides (60m, due 22:00, low, earliest 11:30 -> baseline at 11:30-12:30? No, earliest 15:30 -> baseline at 15:30-16:30!).
        # If t-slides has NO earliest_start_at, let's give baseline a third task t-mid (11:20-14:00, 160m) that gets cancelled via task_updates!
        # Then baseline t-slides was at 14:00-15:00 -> after disruption 10:00-11:30 & t-mid cancelled:
        # - buffer_first moves t-slides EARLY to 11:30-12:30
        # - focus_matched moves t-slides LATE to 19:00-20:00
        # - minimal_change keeps t-slides RIGHT AT 15:30-16:30 / closest to 14:00!
        snap["tasks"].append({
            "id": "t-mid", "title": "旧临时整理", "deadline_at": "2026-11-25T16:00:00+08:00",
            "estimate_minutes": 160, "splittable": True, "min_block_minutes": 30,
            "earliest_start_at": None, "priority": 70, "status": "active",
            "location": None, "materials": [], "source_id": None, "evidence": [], "notes": [],
        })
        preview = process_post("/api/v3/replan/preview", {
            "snapshot": snap,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
            "disruptions": [
                {"id": "e-d", "title": "临时加会", "start_at": "2026-11-25T10:00:00+08:00", "end_at": "2026-11-25T11:30:00+08:00"},
            ],
            "rest_blocks": [
                {"id": "r-l", "label": "午休", "start": "2026-11-25T12:30:00+08:00", "end": "2026-11-25T14:00:00+08:00", "locked": True},
            ],
            "task_updates": [{"task_id": "t-mid", "status": "cancelled"}],
            "focus_windows": [
                {"date": "2026-11-25", "start": "14:00", "end": "16:00", "level": "high", "label": "下午黄金专注"},
                {"date": "2026-11-25", "start": "19:00", "end": "21:00", "level": "low", "label": "晚间轻度整理"},
            ],
            "task_profiles": {"t-math-hw": {"cognitive_load": "high"}, "t-slides": {"cognitive_load": "low"}},
        })
        ids = {p["plan_id"] for p in preview["candidate_plans"]}
        self.assertEqual(ids, {"focus_matched", "buffer_first", "minimal_change"})
        by_id = {p["plan_id"]: p for p in preview["candidate_plans"]}
        self.assertEqual(preview["recommended_plan_id"], "minimal_change")
        self.assertLessEqual(by_id["minimal_change"]["metrics"]["moved_minutes"], by_id["focus_matched"]["metrics"]["moved_minutes"])
        self.assertLessEqual(by_id["minimal_change"]["metrics"]["moved_minutes"], by_id["buffer_first"]["metrics"]["moved_minutes"])

    def test_needs_decision_is_false_when_original_schedule_is_unaffected(self) -> None:
        # Disruption at 20:30-21:30 does not touch morning tasks (09:50-11:20, 11:20-12:20), no focus windows
        snap = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"]["snapshot"])
        preview = process_post("/api/v3/replan/preview", {
            "snapshot": snap,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
            "disruptions": [
                {"id": "e-late", "title": "晚间闲聊", "start_at": "2026-11-25T20:30:00+08:00", "end_at": "2026-11-25T21:30:00+08:00"},
            ],
        })
        self.assertEqual(preview["status"], "ready")
        self.assertFalse(preview["needs_decision"])
        self.assertIn("无需弹出", preview["decision_reason"])
        self.assertEqual(preview["candidate_plans"][0]["name"], "保持原排法")
        self.assertEqual(preview["candidate_plans"][0]["metrics"]["moved_tasks"], 0)
        self.assertEqual(preview["candidate_plans"][0]["metrics"]["moved_minutes"], 0)

    def test_focus_only_without_state_change_returns_needs_decision_false_with_focus_hint_and_keep_name(self) -> None:
        # Only focus_windows + task_profiles passed, NO disruptions/rest_blocks/task_updates:
        # must return needs_decision=False, populate focus_hint, and name the 0-move plan "保持原排法".
        req = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"])
        req["disruptions"] = []
        req["rest_blocks"] = []
        preview = process_post("/api/v3/replan/preview", req)
        self.assertEqual(preview["status"], "ready")
        self.assertFalse(preview["needs_decision"])
        self.assertIn("没有打断或状态变更", preview["decision_reason"])
        self.assertIsNotNone(preview["focus_hint"])
        self.assertIn("专注匹配优先方案", preview["focus_hint"])
        zero_plans = [p for p in preview["candidate_plans"] if p["metrics"]["moved_minutes"] == 0]
        self.assertEqual(len(zero_plans), 1)
        self.assertEqual(zero_plans[0]["name"], "保持原排法")
        self.assertTrue(zero_plans[0]["recommended"])

    def test_non_disrupting_locked_rest_with_focus_windows_returns_needs_decision_false_and_focus_hint(self) -> None:
        # Exact repro: success_replan snapshot + locked rest Wed 22:00-23:00 (doesn't touch any task)
        # + weekday high focus 14:00-16:00 + task_profiles -> needs_decision=False, focus_hint populated!
        snap = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"]["snapshot"])
        preview = process_post("/api/v3/replan/preview", {
            "snapshot": snap,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
            "rest_blocks": [
                {"id": "rest-late", "label": "睡前放松", "start": "2026-11-25T22:00:00+08:00", "end": "2026-11-25T23:00:00+08:00", "locked": True},
            ],
            "focus_windows": [
                {"weekdays": [1, 2, 3, 4, 5], "start": "14:00", "end": "16:00", "level": "high", "label": "下午黄金专注"},
            ],
            "task_profiles": {"t-math-hw": {"cognitive_load": "high"}, "t-slides": {"cognitive_load": "low"}},
        })
        self.assertEqual(preview["status"], "ready")
        self.assertFalse(preview["needs_decision"])
        self.assertIn("未挤动任何已排任务", preview["decision_reason"])
        self.assertEqual(preview["candidate_plans"][0]["name"], "保持原排法")
        self.assertEqual(preview["candidate_plans"][0]["metrics"]["moved_tasks"], 0)
        self.assertIsNotNone(preview["focus_hint"])
        self.assertIn("专注匹配优先方案", preview["focus_hint"])

    def test_confirm_rejects_tampered_schedule_or_preview_snapshot_with_409_preview_tampered(self) -> None:
        fx = self.fixture_doc["fixtures"]["success_replan"]
        # 1. Tamper expected_schedule
        tampered_sched = deepcopy(fx["confirm_request"])
        tampered_sched["expected_schedule"][0]["start"] = "2026-11-25T15:00:00+08:00"
        st1, body1 = _post_json(f"{self.base_url}/api/v3/replan/confirm", tampered_sched)
        self.assertEqual(st1, 409)
        self.assertEqual(body1["code"], "preview_tampered")
        self.assertTrue(body1["requires_repreview"])

        # 2. Tamper preview_snapshot
        tampered_snap = deepcopy(fx["confirm_request"])
        tampered_snap["preview_snapshot"]["tasks"][0]["earliest_start_at"] = "2026-11-25T15:00:00+08:00"
        st2, body2 = _post_json(f"{self.base_url}/api/v3/replan/confirm", tampered_snap)
        self.assertEqual(st2, 409)
        self.assertEqual(body2["code"], "preview_tampered")
        self.assertTrue(body2["requires_repreview"])

    def test_starter_marks_resting_true_during_locked_rest_block(self) -> None:
        req = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"])
        req["now"] = "2026-11-25T13:00:00+08:00"  # inside locked 午休恢复 12:30-14:00
        preview = process_post("/api/v3/replan/preview", req)
        starter = preview["five_minute_starter"]
        self.assertIsNotNone(starter)
        self.assertTrue(starter["resting"])
        self.assertEqual(starter["resting_label"], "午休恢复")
        self.assertIn("想歇就歇", starter["action"])

    def test_fixture_2_infeasible_before_deadline_reports_missing_windows(self) -> None:
        fx = self.fixture_doc["fixtures"]["infeasible_before_deadline"]
        status, preview = _post_json(f"{self.base_url}/api/v3/replan/preview", fx["preview_request"])
        self.assertEqual(status, 200)
        self.assertEqual(preview["status"], "infeasible")
        self.assertFalse(preview["feasible"])
        self.assertFalse(preview["confirmable"])
        self.assertTrue(preview["needs_decision"])
        self.assertEqual(preview["candidate_plans"], [])
        self.assertEqual(len(preview["missing_windows"]), 1)
        mw = preview["missing_windows"][0]
        self.assertEqual(mw["task_id"], "t-report")
        self.assertEqual((mw["required_minutes"], mw["available_minutes"], mw["shortfall_minutes"]), (120, 60, 60))
        blocker_titles = {b["title"] for b in mw["blocking_items"]}
        self.assertIn("专业课", blocker_titles)
        self.assertIn("临时辅导员谈话", blocker_titles)
        self.assertIn("锁定休息：喘息缓冲", blocker_titles)
        self.assertIn("缺少至少 60 分钟时间窗口", mw["summary"])

    def test_fixture_3_stale_snapshot_conflict_returns_http_409(self) -> None:
        fx = self.fixture_doc["fixtures"]["stale_snapshot_conflict"]
        status, body = _post_json(f"{self.base_url}/api/v3/replan/confirm", fx["confirm_request"])
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "stale_preview")
        self.assertEqual(body["expected_revision"], 4)
        self.assertEqual(body["current_revision"], 5)
        self.assertTrue(body["requires_repreview"])

    def test_confirm_also_rejects_same_revision_mutated_snapshot_via_fingerprint(self) -> None:
        fx = self.fixture_doc["fixtures"]["success_replan"]
        mutated = deepcopy(fx["confirm_request"])
        mutated["snapshot"]["tasks"][0]["title"] = "被悄悄修改的标题"
        status, body = _post_json(f"{self.base_url}/api/v3/replan/confirm", mutated)
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "stale_preview")

    def test_focus_is_soft_objective_and_never_violates_hard_deadline(self) -> None:
        snap = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"]["snapshot"])
        snap["tasks"] = [snap["tasks"][0]]
        snap["tasks"][0]["deadline_at"] = "2026-11-25T17:00:00+08:00"
        solved = process_post("/api/v3/solve", {
            "snapshot": snap,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
            "focus_windows": [
                {"date": "2026-11-25", "start": "19:00", "end": "21:00", "level": "high", "label": "晚间专注"},
            ],
            "task_profiles": {"t-math-hw": {"cognitive_load": "high"}},
            "rest_blocks": [
                {"id": "r1", "label": "强制午休", "start": "2026-11-25T12:00:00+08:00", "end": "2026-11-25T14:00:00+08:00", "locked": True},
            ],
        })
        self.assertTrue(solved["validation"]["passed"])
        self.assertLessEqual(solved["schedule"][0]["end"], "2026-11-25T17:00:00+08:00")
        self.assertIn("respects_locked_rest_blocks", solved["validation"]["checked_constraints"])

    def test_replan_reuses_calibration_history_when_apply_calibration_true(self) -> None:
        req = deepcopy(self.fixture_doc["fixtures"]["success_replan"]["preview_request"])
        req["calibration_history"] = [
            {"title": "写实验报告", "kind": "写作", "estimated_minutes": 60, "actual_minutes": 90},
            {"title": "写课程总结", "kind": "写作", "estimated_minutes": 60, "actual_minutes": 90},
            {"title": "做线性代数作业", "kind": "作业", "estimated_minutes": 60, "actual_minutes": 90},
        ]
        req["apply_calibration"] = True
        preview = process_post("/api/v3/replan/preview", req)
        self.assertEqual(preview["status"], "ready")
        self.assertTrue(any(n["applied"] and n["calibrated_minutes"] > n["nominal_minutes"] for n in preview["calibration_notes"]))

    def test_parse_state_rules_and_optional_deepseek_model(self) -> None:
        rules_res = process_post("/api/v3/replan/parse-state", {
            "text": "突然被叫去开会 14:00-15:30，晚上 19:00-20:00 想休息，现在有点困",
            "now": "2026-11-25T13:30:00+08:00",
            "mode": "rules",
        })
        self.assertTrue(rules_res["needs_confirmation"])
        self.assertEqual(rules_res["mode_used"], "rules")
        self.assertEqual(len(rules_res["parsed_changes"]["disruptions"]), 1)
        self.assertEqual(len(rules_res["parsed_changes"]["rest_blocks"]), 1)
        self.assertTrue(rules_res["parsed_changes"]["rest_blocks"][0]["locked"])
        self.assertEqual(len(rules_res["parsed_changes"]["focus_windows"]), 1)
        self.assertEqual(rules_res["parsed_changes"]["focus_windows"][0]["level"], "low")

        fake_llm = {
            "disruptions": [{"title": "临时班会", "start_at": "2026-11-25T15:00:00+08:00", "end_at": "2026-11-25T16:00:00+08:00", "location": None}],
            "rest_blocks": [{"label": "休息半小时", "start": "2026-11-25T16:00:00+08:00", "end": "2026-11-25T16:30:00+08:00", "locked": True}],
            "focus_windows": [],
            "summary": "识别到临时班会与锁定休息，请确认。",
        }
        with patch("campus_agent.replan.chat_json", return_value=fake_llm):
            model_res = process_post("/api/v3/replan/parse-state", {
                "text": "下午三点临时开一小时班会，之后歇半小时",
                "now": "2026-11-25T13:30:00+08:00",
                "mode": "model",
            })
        self.assertEqual(model_res["mode_used"], "model")
        self.assertTrue(model_res["needs_confirmation"])
        self.assertEqual(model_res["parsed_changes"]["disruptions"][0]["title"], "临时班会")

    def test_five_minute_starter_rules_and_optional_model(self) -> None:
        r_res = process_post("/api/v3/starter", {"title": "写操作系统实验报告", "mode": "rules"})
        self.assertEqual(r_res["kind"], "写作")
        self.assertEqual(r_res["duration_minutes"], 5)
        self.assertFalse(r_res["resting"])
        self.assertIn("提纲", r_res["action"])
        with patch("campus_agent.replan.chat_json", return_value={"action": "打开实验报告模板，先把实验目的和环境配置两行填完。"}):
            m_res = process_post("/api/v3/starter", {"title": "写操作系统实验报告", "mode": "model"})
        self.assertEqual(m_res["mode_used"], "model")
        self.assertIn("实验报告模板", m_res["action"])

    def test_direct_confirm_raises_conflict_error_on_revision_mismatch(self) -> None:
        fx = self.fixture_doc["fixtures"]["stale_snapshot_conflict"]
        with self.assertRaises(ReplanConflictError) as ctx:
            process_post("/api/v3/replan/confirm", fx["confirm_request"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "stale_preview")
        self.assertTrue(snapshot_fingerprint(fx["confirm_request"]["snapshot"]).startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
