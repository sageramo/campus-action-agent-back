"""Tests for Backend 1.31: POST /api/v3/capacity/plan (capacity_plan_v1) and
/api/v3/chat schedule tools (agenda_chat_tools_v1).
"""
from __future__ import annotations

import copy
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from campus_agent.dify_client import ExtractionServiceError
from campus_agent.server import CampusAgentHandler, process_post

ROADSHOW_NOW = "2026-11-24T14:15:00+08:00"


def _ev(event_id: str, title: str, start_at: str, end_at: str, *, kind: str = "appointment", location: str | None = None) -> dict:
    return {
        "id": event_id,
        "title": title,
        "kind": kind,
        "start_at": start_at,
        "end_at": end_at,
        "location": location,
        "status": "active",
        "source_id": None,
        "evidence": [],
        "notes": [],
    }


def _roadshow_snapshot(*, with_photo_notice: bool = True, extra_events: list[dict] | None = None, extra_adjustments: list[dict] | None = None) -> dict:
    """Exact roadshow snapshot (2026-11-24 14:15 Tue; 11-25 摄影社说明会 overlaps 专业课)."""
    daily_window = [
        {"start": "08:00", "end": "23:00"},  # Mon
        {"start": "08:00", "end": "23:00"},  # Tue
        {"start": "14:00", "end": "18:00"},  # Wed
        {"start": "08:00", "end": "23:00"},  # Thu
        {"start": "08:00", "end": "23:00"},  # Fri
        {"start": "09:00", "end": "23:00"},  # Sat (14h = 840m)
        {"start": "09:00", "end": "23:00"},  # Sun (14h = 840m)
    ]
    routines = [
        {
            "id": "rt-major",
            "title": "专业课",
            "weekdays": [3],
            "start": "14:00",
            "end": "15:00",
            "from": "2026-09-07",
            "until": None,
            "week_interval": 1,
            "skip_dates": [],
            "location": "新闻楼402",
            "source_id": None,
        },
        {
            "id": "rt-math",
            "title": "高等数学",
            "weekdays": [1, 4],
            "start": "08:00",
            "end": "09:50",
            "from": "2026-09-07",
            "until": None,
            "week_interval": 1,
            "skip_dates": [],
            "location": "主楼301",
            "source_id": None,
        },
        {
            "id": "rt-eng",
            "title": "大学英语",
            "weekdays": [2, 5],
            "start": "10:10",
            "end": "12:00",
            "from": "2026-09-07",
            "until": None,
            "week_interval": 1,
            "skip_dates": [],
            "location": "外语楼204",
            "source_id": None,
        },
        {
            "id": "rt-club",
            "title": "社团例会",
            "weekdays": [4],
            "start": "19:00",
            "end": "20:00",
            "from": "2026-09-07",
            "until": None,
            "week_interval": 1,
            "skip_dates": [],
            "location": "学活204",
            "source_id": None,
        },
    ]
    events = []
    tasks = []
    if with_photo_notice:
        events.append(
            _ev(
                "ev-photo-briefing",
                "摄影社招新说明会",
                "2026-11-25T14:30:00+08:00",
                "2026-11-25T15:30:00+08:00",
                kind="temporary",
                location="学活204",
            )
        )
        tasks.append(
            {
                "id": "tk-photo-form",
                "title": "提交社团报名表",
                "deadline_at": "2026-11-25T18:00:00+08:00",
                "earliest_start_at": None,
                "estimate_minutes": 30,
                "splittable": False,
                "min_block_minutes": None,
                "priority": 70,
                "status": "active",
                "location": None,
                "materials": [],
                "source_id": None,
                "evidence": [],
                "notes": [],
            }
        )
    if extra_events:
        events.extend(extra_events)
    return {
        "schema_version": 3,
        "revision": 3,
        "timezone": "Asia/Shanghai",
        "reference_time": ROADSHOW_NOW,
        "sources": [],
        "daily_window": daily_window,
        "routines_confirmed": True,
        "routines": routines,
        "free_adjustments": list(extra_adjustments or []),
        "events": events,
        "tasks": tasks,
    }


def _post_json(url: str, body: dict) -> tuple[int, dict]:
    req = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class CapacityPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CampusAgentHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_health_declares_131_capabilities(self) -> None:
        with urlopen(f"{self.base_url}/api/health", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["version"], "1.31")
        self.assertIn("capacity_plan_v1", data["capabilities"])
        self.assertIn("agenda_chat_tools_v1", data["capabilities"])

    def test_weekend_single_activity_feasible_and_read_only(self) -> None:
        snap = _roadshow_snapshot()
        before = copy.deepcopy(snap)
        status, res = _post_json(
            f"{self.base_url}/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "scope": "weekend",
                "activities": [
                    {"title": "出去玩", "minutes": 240, "category": "leisure", "splittable": False, "preferred": "weekend"}
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(snap, before)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["scope"]["type"], "weekend")
        self.assertEqual(res["scope"]["label"], "本周末")
        self.assertEqual(res["available_minutes"], 1680)
        self.assertEqual(res["leftover_minutes"], 1440)
        self.assertEqual(res["longest_free_minutes"], 840)
        self.assertEqual(res["shortfall"], [])
        self.assertEqual(res["tradeoffs"], [])
        self.assertEqual(len(res["placements"]), 1)
        self.assertEqual(res["placements"][0]["status"], "placed")
        self.assertEqual(res["placements"][0]["blocks"][0]["start"], "2026-11-28T09:00:00+08:00")
        self.assertEqual(res["placements"][0]["blocks"][0]["end"], "2026-11-28T13:00:00+08:00")
        self.assertIn("排得开", res["summary"])

    def test_roadshow_three_activities_feasible_and_deterministic(self) -> None:
        snap = _roadshow_snapshot()
        payload = {
            "snapshot": snap,
            "now": ROADSHOW_NOW,
            "scope": "weekend",
            "activities": [
                {"title": "出去玩", "minutes": 240, "category": "leisure", "splittable": False, "preferred": "daytime"},
                {"title": "写作业", "minutes": 180, "category": "study", "splittable": True, "min_block_minutes": 60, "preferred": "any"},
                {"title": "社团活动", "minutes": 120, "category": "club", "splittable": False, "preferred": "evening"},
            ],
            "reserve_rest_minutes": 120,
        }
        res1 = process_post("/api/v3/capacity/plan", payload)
        res2 = process_post("/api/v3/capacity/plan", payload)
        self.assertEqual(res1, res2)
        self.assertTrue(res1["feasible"])
        self.assertTrue(res1["rest_satisfied"])
        self.assertEqual(res1["available_minutes"], 1680)
        self.assertEqual(res1["requested_minutes"], 540)
        self.assertEqual(res1["placed_minutes"], 540)
        self.assertEqual(res1["leftover_minutes"], 1140)
        self.assertEqual(res1["longest_free_minutes"], 840)
        self.assertEqual(res1["shortfall"], [])
        # Verify club preferred="evening" landed in 18:00-20:00
        club_p = res1["placements"][2]
        self.assertEqual(club_p["title"], "社团活动")
        self.assertTrue(club_p["matched_preferred"])
        self.assertEqual(club_p["blocks"][0]["start"], "2026-11-28T18:00:00+08:00")
        self.assertEqual(club_p["blocks"][0]["end"], "2026-11-28T20:00:00+08:00")

    def test_three_activities_infeasible_produces_verified_tradeoffs(self) -> None:
        # Fill most of Sat and Sun so only 7 hours (420m) remain on weekend:
        # Sat 09:00-13:00 (4h) + Sun 19:00-22:00 (3h)
        busy = [
            _ev("ev-sat-busy", "外出采风", "2026-11-28T13:00:00+08:00", "2026-11-28T23:00:00+08:00"),
            _ev("ev-sun-busy", "家庭聚餐", "2026-11-29T09:00:00+08:00", "2026-11-29T19:00:00+08:00"),
            _ev("ev-sun-late", "整理行李", "2026-11-29T22:00:00+08:00", "2026-11-29T23:00:00+08:00"),
        ]
        snap = _roadshow_snapshot(extra_events=busy)
        res = process_post(
            "/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "scope": "weekend",
                "activities": [
                    {"title": "出去玩", "minutes": 240, "category": "leisure", "splittable": False},
                    {"title": "写作业", "minutes": 180, "category": "study", "splittable": False},
                    {"title": "社团活动", "minutes": 120, "category": "club", "splittable": False},
                ],
            },
        )
        self.assertFalse(res["feasible"])
        self.assertEqual(res["available_minutes"], 420)
        self.assertEqual(res["total_shortfall_minutes"], 120)
        self.assertEqual(len(res["shortfall"]), 1)
        self.assertEqual(res["shortfall"][0]["title"], "社团活动")
        self.assertEqual(res["shortfall"][0]["missing_minutes"], 120)
        # Should produce 2-3 rule-based tradeoffs with real consequences
        self.assertGreaterEqual(len(res["tradeoffs"]), 2)
        self.assertLessEqual(len(res["tradeoffs"]), 3)
        self.assertTrue(any(t["feasible"] for t in res["tradeoffs"]))
        for t in res["tradeoffs"]:
            self.assertIn("title", t)
            self.assertIn("consequence", t)
            self.assertIn("leftover_minutes", t)
        self.assertIn("排不开", res["summary"])

    def test_continuity_shortage_triggers_split_tradeoff(self) -> None:
        # Three 2h slots on weekend = 6h total, no 3h continuous slot
        busy = [
            _ev("ev-sat-1", "排练", "2026-11-28T11:00:00+08:00", "2026-11-28T15:00:00+08:00"),
            _ev("ev-sat-2", "聚会", "2026-11-28T17:00:00+08:00", "2026-11-28T23:00:00+08:00"),
            _ev("ev-sun-1", "值班", "2026-11-29T11:00:00+08:00", "2026-11-29T23:00:00+08:00"),
        ]
        snap = _roadshow_snapshot(extra_events=busy)
        res = process_post(
            "/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "scope": "weekend",
                "activities": [
                    {"title": "写作业", "minutes": 180, "category": "study", "splittable": False},
                    {"title": "社团", "minutes": 120, "category": "club", "splittable": False},
                ],
            },
        )
        self.assertFalse(res["feasible"])
        self.assertEqual(res["available_minutes"], 360)
        split_tradeoff = next((t for t in res["tradeoffs"] if t["kind"] == "split_activity"), None)
        self.assertIsNotNone(split_tradeoff)
        self.assertTrue(split_tradeoff["feasible"])
        self.assertEqual(split_tradeoff["leftover_minutes"], 60)
        self.assertIn("拆成", split_tradeoff["title"])

    def test_reserve_rest_minutes_enforced(self) -> None:
        # Leave exactly 4h on Sunday (Sat blocked)
        busy = [
            _ev("ev-sat-all", "全天比赛", "2026-11-28T09:00:00+08:00", "2026-11-28T23:00:00+08:00"),
            _ev("ev-sun-most", "复盘", "2026-11-29T13:00:00+08:00", "2026-11-29T23:00:00+08:00"),
        ]
        snap = _roadshow_snapshot(extra_events=busy)
        res = process_post(
            "/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "scope": "weekend",
                "activities": [{"title": "写作业", "minutes": 180, "category": "study", "splittable": True}],
                "reserve_rest_minutes": 120,
            },
        )
        self.assertFalse(res["feasible"])
        self.assertFalse(res["rest_satisfied"])
        self.assertEqual(res["rest_shortfall_minutes"], 60)
        self.assertEqual(res["leftover_minutes"], 60)
        self.assertTrue(any(t["kind"] == "reduce_rest" for t in res["tradeoffs"]))

    def test_no_free_time_in_scope(self) -> None:
        busy = [
            _ev("ev-sat-full", "校外实践", "2026-11-28T09:00:00+08:00", "2026-11-28T23:00:00+08:00"),
            _ev("ev-sun-full", "校外实践二", "2026-11-29T09:00:00+08:00", "2026-11-29T23:00:00+08:00"),
        ]
        snap = _roadshow_snapshot(extra_events=busy)
        res = process_post(
            "/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "scope": "weekend",
                "activities": [{"title": "出去玩", "minutes": 180, "category": "leisure"}],
            },
        )
        self.assertFalse(res["feasible"])
        self.assertEqual(res["available_minutes"], 0)
        self.assertEqual(res["leftover_minutes"], 0)
        self.assertEqual(res["longest_free_minutes"], 0)
        self.assertEqual(res["shortfall"][0]["missing_minutes"], 180)
        self.assertIn("已无剩余留白", res["summary"])

    def test_undefined_daily_window_returns_graceful_infeasible(self) -> None:
        snap = _roadshow_snapshot()
        snap["daily_window"] = None
        res = process_post(
            "/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "scope": "weekend",
                "activities": [{"title": "出去玩", "minutes": 120}],
            },
        )
        self.assertFalse(res["free_time_defined"])
        self.assertFalse(res["feasible"])
        self.assertEqual(res["available_minutes"], 0)
        self.assertIn("作息", res["summary"])

    def test_empty_activities_missing_duration_and_invalid_inputs_rejected(self) -> None:
        snap = _roadshow_snapshot()
        bad_payloads = [
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "weekend", "activities": []},
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "weekend"},
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "weekend", "activities": [{"title": "出去玩"}]},
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "weekend", "activities": [{"title": "出去玩", "minutes": None}]},
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "weekend", "activities": [{"title": "出去玩", "minutes": 0}]},
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "weekend", "activities": [{"title": "出去玩", "minutes": -60}]},
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "weekend", "activities": [{"title": "出去玩", "minutes": True}]},
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "weekday", "activities": [{"title": "出去玩", "minutes": 60}]},
            {"snapshot": snap, "now": ROADSHOW_NOW, "scope": "unknown_scope", "activities": [{"title": "出去玩", "minutes": 60}]},
        ]
        for body in bad_payloads:
            status, err = _post_json(f"{self.base_url}/api/v3/capacity/plan", body)
            self.assertEqual(status, 400, f"Expected 400 for {body}")
            self.assertIn("error", err)

    def test_scopes_and_custom_from_to(self) -> None:
        snap = _roadshow_snapshot()
        # weekday: Friday (index=4) -> 2026-11-27 (08:00-23:00 minus 10:10-12:00 English = 13h10m = 790m)
        fri = process_post(
            "/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "scope": "weekday",
                "weekday_index": 4,
                "activities": [{"title": "看电影", "minutes": 120, "category": "leisure"}],
            },
        )
        self.assertTrue(fri["feasible"])
        self.assertEqual(fri["scope"]["label"], "本周五")
        self.assertEqual(fri["available_minutes"], 790)

        # Custom from/to dates
        custom = process_post(
            "/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "from": "2026-11-28",
                "to": "2026-11-28",
                "activities": [{"title": "看展", "minutes": 180, "category": "leisure"}],
            },
        )
        self.assertTrue(custom["feasible"])
        self.assertEqual(custom["scope"]["type"], "custom")
        self.assertEqual(custom["available_minutes"], 840)
        self.assertEqual(custom["leftover_minutes"], 660)


class AgendaChatToolsTests(unittest.TestCase):
    def test_chat_plan_activities_matches_capacity_plan_exact_numbers(self) -> None:
        """Roadshow acceptance check: model chat & direct capacity/plan return identical numbers."""
        snap = _roadshow_snapshot()
        direct = process_post(
            "/api/v3/capacity/plan",
            {
                "snapshot": snap,
                "now": ROADSHOW_NOW,
                "scope": "weekend",
                "activities": [
                    {"title": "出去玩", "minutes": 240, "category": "leisure", "splittable": False, "preferred": "weekend"},
                    {"title": "写作业", "minutes": 180, "category": "study", "splittable": True, "min_block_minutes": 60, "preferred": "any"},
                ],
            },
        )
        step1_tool = {
            "tool": "plan_activities",
            "arguments": {
                "scope": "weekend",
                "activities": [
                    {"title": "出去玩", "minutes": 240, "category": "leisure", "splittable": False, "preferred": "weekend"},
                    {"title": "写作业", "minutes": 180, "category": "study", "splittable": True, "min_block_minutes": 60, "preferred": "any"},
                ],
                "reserve_rest_minutes": 0,
            },
        }
        step2_reply = {"reply": "本周末排得开：出去玩（4 小时）可安排在 11月28日 周六 09:00–13:00，写作业（3 小时）安排在 13:00–16:00，排完后还剩 21 小时留白（最长连续 14 小时）。"}
        with patch("campus_agent.agenda_chat.chat_json", side_effect=[step1_tool, step2_reply]) as model:
            chat_res = process_post(
                "/api/v3/chat",
                {
                    "mode": "model",
                    "messages": [{"role": "user", "text": "这周末我想出去玩半天，还要写 3 小时作业，排得开吗"}],
                    "snapshot": snap,
                    "now": ROADSHOW_NOW,
                },
            )
        self.assertEqual(model.call_count, 2)
        # Raw snapshot must never be dumped into step-1 prompt
        self.assertNotIn("高等数学", model.call_args_list[0].args[1])
        self.assertEqual(chat_res["reply"], step2_reply["reply"])
        self.assertEqual(chat_res["capacity"], direct)
        self.assertTrue(chat_res["capacity"]["feasible"])
        self.assertEqual(chat_res["capacity"]["leftover_minutes"], 1260)

    def test_chat_check_capacity_tool(self) -> None:
        snap = _roadshow_snapshot()
        step1 = {"tool": "check_capacity", "arguments": {"scope": "weekend"}}
        step2 = {"reply": "本周末还剩 28 小时留白，最长连续 14 小时（周六 09:00–23:00、周日 09:00–23:00）。出去玩大概几个小时？"}
        with patch("campus_agent.agenda_chat.chat_json", side_effect=[step1, step2]) as model:
            res = process_post(
                "/api/v3/chat",
                {
                    "mode": "model",
                    "messages": [{"role": "user", "text": "我这周末还有时间出去玩吗"}],
                    "snapshot": snap,
                    "now": ROADSHOW_NOW,
                },
            )
        self.assertEqual(model.call_count, 2)
        self.assertEqual(res["reply"], step2["reply"])
        self.assertEqual(res["capacity"]["tool"], "check_capacity")
        self.assertEqual(res["capacity"]["available_minutes"], 1680)
        self.assertEqual(res["capacity"]["longest_free_minutes"], 840)
        self.assertEqual(len(res["capacity"]["free_slots"]), 2)

    def test_chat_missing_duration_in_plan_activities_gracefully_falls_back_to_check_capacity(self) -> None:
        snap = _roadshow_snapshot()
        step1 = {
            "tool": "plan_activities",
            "arguments": {"scope": "weekend", "activities": [{"title": "出去玩", "minutes": None}]},
        }
        step2 = {"reply": "本周末还剩 28 小时留白，出去玩大概几个小时？"}
        with patch("campus_agent.agenda_chat.chat_json", side_effect=[step1, step2]) as model:
            res = process_post(
                "/api/v3/chat",
                {
                    "mode": "model",
                    "messages": [{"role": "user", "text": "周末能出去玩吗"}],
                    "snapshot": snap,
                    "now": ROADSHOW_NOW,
                },
            )
        self.assertEqual(res["capacity"]["tool"], "check_capacity")
        self.assertIn("missing_duration_note", model.call_args_list[1].args[1])

    def test_chat_with_snapshot_allows_direct_reply_without_tool(self) -> None:
        snap = _roadshow_snapshot()
        with patch("campus_agent.agenda_chat.chat_json", return_value={"reply": "听起来最近挺忙的，想先聊聊哪件事？"}) as model:
            res = process_post(
                "/api/v3/chat",
                {
                    "mode": "model",
                    "messages": [{"role": "user", "text": "最近好累"}],
                    "snapshot": snap,
                    "now": ROADSHOW_NOW,
                },
            )
        self.assertEqual(model.call_count, 1)
        self.assertEqual(res, {"reply": "听起来最近挺忙的，想先聊聊哪件事？"})
        self.assertNotIn("capacity", res)

    def test_chat_unknown_tool_raises_502(self) -> None:
        snap = _roadshow_snapshot()
        with patch("campus_agent.agenda_chat.chat_json", return_value={"tool": "delete_all_tasks", "arguments": {}}):
            with self.assertRaises(ExtractionServiceError) as ctx:
                process_post(
                    "/api/v3/chat",
                    {
                        "mode": "model",
                        "messages": [{"role": "user", "text": "帮我排一下"}],
                        "snapshot": snap,
                        "now": ROADSHOW_NOW,
                    },
                )
        self.assertEqual(ctx.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
