"""Tests for personal commitment calibration loop (Planning-Fallacy bias correction)."""
import json
from pathlib import Path
import unittest

from campus_agent.server import process_post

ROOT = Path(__file__).resolve().parents[1]


class CalibrationTests(unittest.TestCase):
    def test_optimism_risk_triggers_when_nominal_fits_but_calibrated_overruns(self):
        # roadshow-capacity-shortage.snapshot.json has 70m remaining free time today.
        # Nominal 50m + 15m rest = 65m <= 70m (nominal fits with 5m surplus).
        # With historical 1.5x inflation on 写作, calibrated = 75m + 15m rest = 90m > 70m -> 20m short!
        snap = json.loads((ROOT / "examples/roadshow-capacity-shortage.snapshot.json").read_text(encoding="utf-8"))
        history = [
            {"title": "课程报告", "estimated_minutes": 60, "actual_minutes": 90},
            {"title": "实验总结", "estimated_minutes": 40, "actual_minutes": 60},
            {"title": "社团文案", "estimated_minutes": 30, "actual_minutes": 45},
        ]
        res = process_post("/api/v3/calibrate-capacity", {
            "snapshot": snap,
            "range": {"from": "2026-11-25", "to": "2026-11-25"},
            "history": history,
            "title": "写社团新闻稿",
            "nominal_minutes": 50,
            "reserve_minutes": 15,
            "splittable": False,
        })
        self.assertEqual(res["calibration"]["source"], "kind:写作")
        self.assertEqual(res["calibration"]["factor"], 1.5)
        self.assertEqual(res["calibrated_minutes"], 75)
        self.assertEqual(res["remaining_free_minutes"], 70)
        self.assertTrue(res["nominal_verdict"]["fits"])
        self.assertFalse(res["calibrated_verdict"]["fits"])
        self.assertEqual(res["calibrated_verdict"]["shortage_minutes"], 20)
        self.assertTrue(res["optimism_risk"])
        self.assertIn("乐观偏差预警", res["explanation"])
        self.assertFalse(res["snapshot_mutated"])

    def test_insufficient_history_keeps_factor_1_and_explains_honestly(self):
        snap = json.loads((ROOT / "examples/roadshow-capacity-shortage.snapshot.json").read_text(encoding="utf-8"))
        res = process_post("/api/v3/calibrate-capacity", {
            "snapshot": snap,
            "range": {"from": "2026-11-25", "to": "2026-11-25"},
            "history": [{"title": "报告", "estimated_minutes": 30, "actual_minutes": 60}],
            "title": "写报告",
            "nominal_minutes": 40,
            "reserve_minutes": 20,
        })
        self.assertEqual(res["calibration"]["factor"], 1.0)
        self.assertEqual(res["calibration"]["confidence"], "none")
        self.assertEqual(res["calibrated_minutes"], 40)
        self.assertFalse(res["optimism_risk"])
        self.assertIn("不足 2 条", res["explanation"])


if __name__ == "__main__":
    unittest.main()
