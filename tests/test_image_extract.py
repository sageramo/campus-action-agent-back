"""Tests for multimodal poster/notice image extraction (notice_image_v1)."""
import base64
import unittest
from unittest.mock import patch

from campus_agent.layers import validate_snapshot_v3
from campus_agent.server import process_post

TINY_PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode("ascii")


def _wrap_snapshot(res: dict) -> dict:
    snap = {
        "schema_version": 3,
        "revision": 1,
        "timezone": "Asia/Shanghai",
        "reference_time": "2026-11-23T12:00:00+08:00",
        "sources": [res["source"]],
        "daily_window": None,
        "routines_confirmed": False,
        "routines": [],
        "free_adjustments": [],
        "events": res["events"],
        "tasks": res["tasks"],
    }
    validate_snapshot_v3(snap)
    return snap


class NoticeImageExtractTests(unittest.TestCase):
    def test_weekly_club_poster_extracts_all_seven_items_with_deterministic_validation(self) -> None:
        mock_vision = {
            "title": "信息化实践与服务社 十一月第四周 11.23-11.29",
            "items": [
                {
                    "kind": "temporary",
                    "title": "智伴校园长者 · 共享信息服务",
                    "date": "2026-11-25",
                    "start_time": "14:00",
                    "end_time": "17:00",
                    "deadline_time": None,
                    "period": None,
                    "location": "艺术与传播大楼一层校园生活服务大厅",
                    "quote": "WED 11.25 14:00-17:00 智伴校园长者 · 共享信息服务 艺术与传播大楼一层校园生活服务大厅",
                    "note": "负责人 马诗媛",
                },
                {
                    "kind": "temporary",
                    "title": "红军长征主题演出 · 彩排",
                    "date": "2026-11-25",
                    "start_time": None,
                    "end_time": None,
                    "deadline_time": None,
                    "period": "中午",
                    "location": "黑匣子剧场",
                    "quote": "WED 11.25 中午 红军长征主题演出 · 彩排 黑匣子剧场",
                    "note": None,
                },
                {
                    "kind": "temporary",
                    "title": "体育小组晨跑",
                    "date": "2026-11-26",
                    "start_time": "06:30",
                    "end_time": "07:20",
                    "deadline_time": None,
                    "period": None,
                    "location": "南操",
                    "quote": "THU 11.26 6:30-7:20 体育小组晨跑 南操",
                    "note": "负责人 吴磊",
                },
                {
                    "kind": "temporary",
                    "title": "红军长征主题演出",
                    "date": "2026-11-26",
                    "start_time": "19:00",
                    "end_time": None,
                    "deadline_time": None,
                    "period": None,
                    "location": "黑匣子剧场",
                    "quote": "THU 11.26 19:00 红军长征主题演出 黑匣子剧场",
                    "note": None,
                },
                {
                    "kind": "temporary",
                    "title": "Smart Campus Bilingual Connect",
                    "date": "2026-11-28",
                    "start_time": "16:00",
                    "end_time": "18:00",
                    "deadline_time": None,
                    "period": None,
                    "location": "地点待定",
                    "quote": "SAT 11.28 16:00-18:00 Smart Campus Bilingual Connect 地点待定",
                    "note": "负责人 钱多多",
                },
                {
                    "kind": "deadline",
                    "title": "AI万象 · 智创未来 比赛报名截止",
                    "date": "2026-11-28",
                    "start_time": None,
                    "end_time": None,
                    "deadline_time": None,
                    "period": None,
                    "location": None,
                    "quote": "SAT 11.28 AI万象 · 智创未来 比赛报名截止",
                    "note": "负责人 田家昕",
                },
                {
                    "kind": "temporary",
                    "title": "AI万象-AI实践工作坊",
                    "date": "2026-11-29",
                    "start_time": None,
                    "end_time": None,
                    "deadline_time": None,
                    "period": None,
                    "location": "地点待定",
                    "quote": "SUN 11.29 时间待定 AI万象-AI实践工作坊 地点待定",
                    "note": "负责人 刘烨灵",
                },
            ],
            "warnings": [],
        }
        with patch("campus_agent.image_extract.chat_json", return_value=mock_vision) as mock_call:
            res = process_post("/api/v3/extract", {"mode": "image", "image": TINY_PNG, "reference_date": "2026-11-23"})
        _wrap_snapshot(res)
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(res["mode"], "image")
        self.assertEqual(len(res["events"]), 6)
        self.assertEqual(len(res["tasks"]), 1)
        by_title = {ev["title"]: ev for ev in res["events"]}
        self.assertEqual(by_title["智伴校园长者 · 共享信息服务"]["start_at"], "2026-11-25T14:00:00+08:00")
        self.assertEqual(by_title["智伴校园长者 · 共享信息服务"]["end_at"], "2026-11-25T17:00:00+08:00")
        self.assertEqual(by_title["红军长征主题演出 · 彩排"]["start_at"], "2026-11-25T12:00:00+08:00")
        self.assertIsNone(by_title["红军长征主题演出 · 彩排"]["end_at"])
        self.assertEqual(by_title["体育小组晨跑"]["start_at"], "2026-11-26T06:30:00+08:00")
        self.assertEqual(by_title["红军长征主题演出"]["start_at"], "2026-11-26T19:00:00+08:00")
        self.assertIsNone(by_title["红军长征主题演出"]["end_at"])
        # Placeholder locations ("地点待定") must be stripped to None
        self.assertIsNone(by_title["Smart Campus Bilingual Connect"]["location"])
        # "时间待定" stays None instead of hallucinating a clock
        self.assertIsNone(by_title["AI万象-AI实践工作坊"]["start_at"])
        self.assertEqual(res["tasks"][0]["deadline_at"], "2026-11-28T23:59:00+08:00")

    def test_invalid_image_bytes_rejected(self) -> None:
        with self.assertRaises(ValueError):
            process_post("/api/v3/extract", {"mode": "image", "image": "data:image/png;base64,bm90LXBuZw=="})


if __name__ == "__main__":
    unittest.main()
