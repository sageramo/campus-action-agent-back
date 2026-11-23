"""Tests for campus notice change tracking & incremental re-plan (Notice Diff)."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from campus_agent.layer_import import items_from_rules
from campus_agent.server import process_post

ROOT = Path(__file__).resolve().parents[1]


def _photo_club_after_first_notice():
    base = json.loads((ROOT / "examples/roadshow-photo-club.snapshot.json").read_text(encoding="utf-8"))
    snap = deepcopy(base)
    ext = items_from_rules(
        "【摄影社招新通知】\n2026年11月25日14:30-15:30举行摄影社招新说明会（学活204）。\n2026年11月25日18:00前提交社团报名表。",
        "2026-11-25",
    )
    snap["sources"].append(ext["source"])
    snap["events"].extend(ext["events"])
    snap["tasks"].extend(ext["tasks"])
    snap["tasks"][0]["estimate_minutes"] = 30
    return snap


class NoticeDiffTests(unittest.TestCase):
    def test_revision_notice_updates_existing_event_and_task_and_resolves_conflict(self):
        snap = _photo_club_after_first_notice()
        before_json = json.dumps(snap, ensure_ascii=False, sort_keys=True)
        update_notice = (
            "【紧急变更】摄影社招新说明会改到2026年11月26日19:00-20:00举行（48教A302）。\n"
            "社团报名表延后至2026年11月27日12:00前提交。"
        )
        res = process_post("/api/v3/notice-diff", {
            "snapshot": snap,
            "text": update_notice,
            "reference_date": "2026-11-25",
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
            "mode": "rules",
        })
        # Strictly read-only on caller snapshot
        self.assertEqual(json.dumps(snap, ensure_ascii=False, sort_keys=True), before_json)
        self.assertTrue(res["is_revision_notice"])
        self.assertEqual(res["summary"]["updated"], 2)
        self.assertEqual(res["summary"]["added"], 0)
        # Moving 说明会 to Thu 19:00-20:00 resolves Wed 专业课 overlap but collides with Thu 社团例会 19:00-20:00!
        self.assertEqual(res["impact"]["conflicts_before"], 1)
        self.assertEqual(res["impact"]["conflicts_after"], 1)
        ev_op = next(op for op in res["operations"] if op["kind"] == "event")
        self.assertEqual(ev_op["action"], "update")
        self.assertEqual(ev_op["changes"]["location"], {"from": "学活204", "to": "48教A302"})
        self.assertEqual(ev_op["changes"]["start_at"]["to"], "2026-11-26T19:00:00+08:00")
        tk_op = next(op for op in res["operations"] if op["kind"] == "task")
        self.assertEqual(tk_op["action"], "update")
        self.assertEqual(tk_op["changes"]["deadline_at"]["to"], "2026-11-27T12:00:00+08:00")
        # Preview snapshot did NOT duplicate event or task
        self.assertEqual(len(res["preview_snapshot"]["events"]), 1)
        self.assertEqual(len(res["preview_snapshot"]["tasks"]), 1)

    def test_cancellation_notice_cancels_event_and_clears_conflict(self):
        snap = _photo_club_after_first_notice()
        res = process_post("/api/v3/notice-diff", {
            "snapshot": snap,
            "text": "【通知】原定周三下午的摄影社招新说明会取消。",
            "reference_date": "2026-11-25",
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
        })
        self.assertEqual(res["summary"]["cancelled"], 1)
        self.assertEqual(res["impact"]["conflicts_before"], 1)
        self.assertEqual(res["impact"]["conflicts_after"], 0)
        self.assertEqual(res["impact"]["free_delta_minutes"], 30)
        self.assertEqual(res["preview_snapshot"]["events"][0]["status"], "cancelled")

    def test_unrelated_notice_adds_instead_of_false_matching(self):
        snap = _photo_club_after_first_notice()
        res = process_post("/api/v3/notice-diff", {
            "snapshot": snap,
            "text": "2026年11月28日19:00-21:00举行迎新晚会（中传礼堂）。",
            "reference_date": "2026-11-25",
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
        })
        self.assertEqual(res["summary"]["added"], 1)
        self.assertEqual(res["summary"]["updated"], 0)
        self.assertEqual(len(res["preview_snapshot"]["events"]), 2)

    def test_short_followup_date_or_time_correction_updates_latest_single_item(self):
        base = json.loads((ROOT / "examples/roadshow-photo-club.snapshot.json").read_text(encoding="utf-8"))
        snap = deepcopy(base)
        ext = items_from_rules(
            "【家长心理讲座通知】\n⌚讲座时间：本周日（11月29日）19:00-20:30\n📌讲座主题：远山有信，温言相伴——大学生家长的亲子沟通艺术",
            None,
        )
        snap["sources"].append(ext["source"])
        snap["events"].extend(ext["events"])

        res = process_post("/api/v3/notice-diff", {
            "snapshot": snap,
            "text": "消息错了，应该是11月28日（周六）",
            "reference_date": None,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
        })
        self.assertTrue(res["is_revision_notice"])
        self.assertEqual(res["summary"]["updated"], 1)
        self.assertEqual(res["summary"]["added"], 0)
        ev_op = res["operations"][0]
        self.assertEqual(ev_op["changes"]["start_at"], {"from": "2026-11-29T19:00:00+08:00", "to": "2026-11-28T19:00:00+08:00"})
        self.assertEqual(ev_op["changes"]["end_at"], {"from": "2026-11-29T20:30:00+08:00", "to": "2026-11-28T20:30:00+08:00"})

        res_clock = process_post("/api/v3/notice-diff", {
            "snapshot": snap,
            "text": "时间发错了，改到19:30-21:00",
            "reference_date": None,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
        })
        self.assertEqual(res_clock["summary"]["updated"], 1)
        self.assertEqual(res_clock["operations"][0]["changes"]["start_at"]["to"], "2026-11-29T19:30:00+08:00")
        self.assertEqual(res_clock["operations"][0]["changes"]["end_at"]["to"], "2026-11-29T21:00:00+08:00")

        # Ambiguous latest source (1 event + 1 task) must NOT guess which item "消息错了" refers to.
        ambiguous = _photo_club_after_first_notice()
        res_amb = process_post("/api/v3/notice-diff", {
            "snapshot": ambiguous,
            "text": "消息错了，应该是11月28日（周六）",
            "reference_date": "2026-11-25",
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
        })
        self.assertEqual(res_amb["summary"]["updated"], 0)
        self.assertEqual(res_amb["summary"]["added"], 0)

        # Short title-less cancellation ("发错了，取消了") cancels sole latest item instead of creating a fake task
        res_cancel = process_post("/api/v3/notice-diff", {
            "snapshot": snap,
            "text": "发错了，刚才那场取消了",
            "reference_date": None,
            "range": {"from": "2026-11-23", "to": "2026-11-29"},
        })
        self.assertEqual(res_cancel["summary"]["cancelled"], 1)
        self.assertEqual(res_cancel["summary"]["added"], 0)
        self.assertEqual(res_cancel["preview_snapshot"]["events"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
