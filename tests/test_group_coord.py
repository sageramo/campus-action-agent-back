"""Tests for privacy-preserving multi-agent group scheduling (bitmask export & aggregation)."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from campus_agent.server import process_post

ROOT = Path(__file__).resolve().parents[1]


class GroupCoordTests(unittest.TestCase):
    def test_export_mask_strips_all_schedule_details_and_coordinate_finds_zero_conflict_slot(self):
        base = json.loads((ROOT / "examples/roadshow-photo-club.snapshot.json").read_text(encoding="utf-8"))
        # Student A: Wed free 14:00-18:00 minus 专业课 14:00-15:00 -> free 15:00-18:00
        stu_a = deepcopy(base)
        # Student B: Wed free 14:00-18:00 minus a private counseling session 15:00-16:00 -> free 14:00-15:00 & 16:00-18:00
        stu_b = deepcopy(base)
        stu_b["routines"] = []
        stu_b["events"].append({
            "id": "e-private",
            "title": "心理咨询（极度隐私）",
            "start_at": "2026-11-25T15:00:00+08:00",
            "end_at": "2026-11-25T16:00:00+08:00",
            "location": "校医院三层",
            "status": "active",
            "source_id": stu_b["sources"][0]["id"],
            "evidence": [],
            "notes": [],
        })
        # Student C (organizer, weight=2): Wed free 16:00-18:00 only
        stu_c = deepcopy(base)
        stu_c["daily_window"][2] = {"start": "16:00", "end": "18:00"}
        stu_c["routines"] = []

        day_range = {"from": "2026-11-25", "to": "2026-11-25"}
        mask_a = process_post("/api/v3/group/export-mask", {"snapshot": stu_a, "range": day_range, "slot_minutes": 30})
        mask_b = process_post("/api/v3/group/export-mask", {"snapshot": stu_b, "range": day_range, "slot_minutes": 30})
        mask_c = process_post("/api/v3/group/export-mask", {"snapshot": stu_c, "range": day_range, "slot_minutes": 30})

        # Privacy assertion: zero schedule strings leaked in exported masks
        raw_b = json.dumps(mask_b, ensure_ascii=False)
        self.assertNotIn("心理咨询", raw_b)
        self.assertNotIn("校医院", raw_b)
        self.assertNotIn("专业课", json.dumps(mask_a, ensure_ascii=False))
        self.assertEqual(mask_a["slots_total"], 48)
        self.assertEqual(set(mask_a["free_mask"]), {"0", "1"})

        coord = process_post("/api/v3/group/coordinate", {
            "range": day_range,
            "slot_minutes": 30,
            "duration_minutes": 60,
            "top_k": 3,
            "participants": [
                {"label": "成员A", "weight": 1, "free_mask": mask_a["free_mask"]},
                {"label": "成员B", "weight": 1, "free_mask": mask_b["free_mask"]},
                {"label": "主讲C", "weight": 2, "free_mask": mask_c["free_mask"]},
            ],
        })
        self.assertEqual(coord["participant_count"], 3)
        best = coord["recommended_slots"][0]
        # 16:00-17:00 is the earliest 60m window where ALL 3 participants are free!
        self.assertEqual((best["start"][11:16], best["end"][11:16]), ("16:00", "17:00"))
        self.assertEqual(best["conflict_count"], 0)
        self.assertEqual(best["attendance_rate"], 1.0)
        self.assertEqual(len(coord["heatmap"]), 48)

    def test_rejects_mismatched_mask_length(self):
        with self.assertRaisesRegex(ValueError, "位图"):
            process_post("/api/v3/group/coordinate", {
                "range": {"from": "2026-11-25", "to": "2026-11-25"},
                "slot_minutes": 30,
                "duration_minutes": 60,
                "participants": [{"label": "A", "free_mask": "0101"}],
            })


if __name__ == "__main__":
    unittest.main()
