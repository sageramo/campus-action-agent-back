"""Check actual minutes, uncertainty, and recommendations under user constraints."""
import random
import unittest
from datetime import timedelta

from campus_agent.conflict import detect_conflicts, free_intervals, merge_intervals, parse_dt
from campus_agent.models import ConflictTask

NOW = "2026-10-26 09:00"
DEADLINE = "2026-10-26 12:00"


def task(name="报告", minutes=180, **kwargs):
    return dict(task_id=name, name=name, remaining_minutes=minutes, deadline=DEADLINE, **kwargs)


def window(start=NOW, end=DEADLINE):
    return dict(start=start, end=end)


class CapacityTests(unittest.TestCase):
    def check(self, tasks, availability=None, now=NOW):
        return detect_conflicts(tasks, now=now, availability=availability)

    def test_nine_minutes_remains_nine_with_missing_availability(self):
        result = self.check([dict(task_id="R", name="报告", estimated_hours=3, deadline="2026-10-26 23:59")], now="2026-10-26 23:50")
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 9)
        self.assertIn("capacity_shortage", [item["type"] for item in result["conflicts"]])
        self.assertEqual(result["capacity_checks"][0]["basis"], "physical_upper_bound")

    def test_missing_availability_cannot_prove_feasibility(self):
        result = self.check([task(minutes=60)])
        self.assertEqual(result["status"], "information_needed")
        self.assertTrue(result["warnings"])
        self.assertEqual(result["recommendations"]["drop_or_delegate"], [])

    def test_explicit_empty_availability_means_zero(self):
        result = self.check([task(minutes=1)], [])
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 0)
        self.assertEqual(result["status"], "conflicts_detected")

    def test_fixed_meeting_is_subtracted_from_personal_availability(self):
        fixed = dict(task_id="meeting", name="会议", fixed_time=window("2026-10-26 10:00", "2026-10-26 11:00"), estimated_hours=1)
        result = self.check([task(minutes=120), fixed], [window()])
        check = result["capacity_checks"][0]
        self.assertEqual(check["available_minutes"], 120)
        self.assertEqual(check["known_required_minutes"], 120)
        self.assertEqual(result["status"], "no_conflict_detected")
        self.assertNotEqual(result["status"], "feasible")

    def test_duplicate_availability_and_busy_intervals_count_once(self):
        tasks = [task(minutes=90), dict(task_id="A", name="会议A", fixed_time=window("2026-10-26 10:00", "2026-10-26 11:00")), dict(task_id="B", name="会议B", fixed_time=window("2026-10-26 10:30", "2026-10-26 11:30"))]
        result = self.check(tasks, [window(), window("2026-10-26 10:00", "2026-10-26 12:00"), window()])
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 90)
        self.assertIn("hard_time_overlap", [item["type"] for item in result["conflicts"]])

    def test_availability_is_clipped_to_now_and_deadline(self):
        result = self.check([task(minutes=180)], [window("2026-10-25 00:00", "2026-10-27 23:00")])
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 180)

    def test_cross_midnight_does_not_add_full_calendar_days(self):
        data = dict(task_id="R", name="报告", remaining_minutes=30, deadline="2026-10-27 00:20")
        result = self.check([data], [window("2026-10-26 23:50", "2026-10-27 00:20")], now="2026-10-26 23:50")
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 30)

    def test_completed_cancelled_tasks_and_their_fixed_blocks_are_excluded(self):
        tasks = [task(minutes=180), dict(name="已完成", status="completed", estimated_hours=999, deadline=DEADLINE), dict(name="已取消", status="cancelled", fixed_time=window())]
        result = self.check(tasks, [window()])
        self.assertEqual(result["capacity_checks"][0]["known_required_minutes"], 180)
        self.assertFalse(result["conflicts"])

    def test_remaining_work_overrides_original_estimate(self):
        result = self.check([task(minutes=15, estimated_hours=20)], [window()])
        self.assertEqual(result["capacity_checks"][0]["known_required_minutes"], 15)

    def test_direct_dataclass_construction_respects_remaining_work(self):
        data = ConflictTask(task_id="A", name="报告", category="学习", estimated_hours=10, remaining_minutes=15)
        self.assertEqual(data.work_minutes, 15)

    def test_unknown_remaining_is_not_zero_or_original_estimate(self):
        result = self.check([task(minutes=None, estimated_hours=20)], [window()])
        self.assertEqual(result["status"], "information_needed")
        self.assertEqual(result["capacity_checks"][0]["known_required_minutes"], 0)
        self.assertEqual(result["capacity_checks"][0]["unknown_workload_count"], 1)
        self.assertTrue(result["missing_information"])

    def test_task_serialization_preserves_workload_on_roundtrip(self):
        for data in [dict(name="legacy", estimated_hours=2), dict(name="unknown", estimated_hours=2, remaining_minutes=None), dict(name="done", status="completed", estimated_hours=2)]:
            with self.subTest(data=data):
                first = ConflictTask.from_dict(data)
                self.assertEqual(ConflictTask.from_dict(first.to_dict()).work_minutes, first.work_minutes)

    def test_each_unique_deadline_checked_once_with_cumulative_work(self):
        tasks = [task(name="A", minutes=100), task(name="B", minutes=100)]
        result = self.check(tasks, [window()])
        self.assertEqual(len(result["capacity_checks"]), 1)
        self.assertEqual(result["capacity_checks"][0]["known_required_minutes"], 200)
        self.assertEqual(set(result["conflicts"][0]["task_ids"]), {"A", "B"})

    def test_earlier_deadline_shortage_not_hidden_by_later_free_time(self):
        early = dict(name="紧急", task_id="early", remaining_minutes=90, deadline="2026-10-26 10:00")
        result = self.check([early, task(minutes=60)], [window()])
        self.assertEqual(result["capacity_checks"][0]["result"], "shortage")
        self.assertEqual(result["capacity_checks"][1]["result"], "necessary_bound_passed")
        self.assertEqual(result["status"], "conflicts_detected")

    def test_deadline_at_now_or_in_past_is_explicitly_overdue(self):
        for deadline in [NOW, "2026-10-25 12:00"]:
            with self.subTest(deadline=deadline):
                result = self.check([dict(name="未完成", remaining_minutes=10, deadline=deadline)], [window()])
                self.assertIn("deadline_passed", [item["type"] for item in result["conflicts"]])

    def test_historical_fixed_overlaps_are_not_current_conflicts(self):
        tasks = [dict(name=name, fixed_time=window("2026-10-25 10:00", "2026-10-25 11:00")) for name in ["A", "B"]]
        self.assertFalse(self.check(tasks, [window()])["conflicts"])

    def test_adjacent_intervals_do_not_overlap(self):
        tasks = [dict(name="A", fixed_time=window("2026-10-26 10:00", "2026-10-26 11:00")), dict(name="B", fixed_time=window("2026-10-26 11:00", DEADLINE))]
        self.assertFalse(self.check(tasks, [window()])["conflicts"])

    def test_priority_does_not_invent_a_reason_to_abandon(self):
        result = self.check([task(minutes=30, priority=10, must_do=False)], [window()])
        self.assertEqual(result["recommendations"]["drop_or_delegate"], [])

    def test_all_mandatory_conflict_requires_adjustment_not_fake_resolution(self):
        result = self.check([task(name="A", must_do=True), task(name="B", must_do=True)], [window()])
        self.assertEqual(result["status"], "conflicts_detected")
        self.assertEqual(result["recommendations"]["drop_or_delegate"], [])
        self.assertEqual(len(result["recommendations"]["adjust"]), 2)

    def test_missing_duration_and_deadline_are_reported(self):
        result = self.check([dict(name="尚不明确")], [window()])
        self.assertEqual(result["status"], "information_needed")
        self.assertEqual(len(result["missing_information"][0]["fields"]), 2)

    def test_invalid_input_never_silently_falls_back(self):
        bad_tasks = [dict(name="x", must_do="false"), dict(name="x", estimated_hours=-1), dict(name="x", estimated_hours=float("nan")), dict(name="x", estimated_hours=float("inf")), dict(name="x", estimated_hours=True), dict(name="x", estimated_hours=10**400), dict(name="x", priority=101), dict(name="x", priority=50.5), dict(name="x", status=[]), dict(name="x", deadline="2026-02-30 12:00"), dict(name="x", deadline="2026-10-25"), dict(name="x", fixed_time=window(DEADLINE, NOW)), dict(name="x", status="completed", remaining_minutes=5), None]
        for data in bad_tasks:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.check([data], [window()])
        for now in ["bad", "2026-10-26", "2026-10-26 24:01"]:
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.check([], [window()], now=now)

    def test_invalid_or_duplicate_intervals_and_task_ids(self):
        for intervals in [[window(DEADLINE, NOW)], [window(NOW, NOW)], [dict(start=NOW)], "9-12"]:
            with self.subTest(intervals=intervals), self.assertRaises(ValueError):
                self.check([task()], intervals)
        with self.assertRaises(ValueError):
            self.check([task(), task()], [window()])

    def test_same_names_do_not_link_unrelated_task_recommendations(self):
        urgent = dict(task_id="A", name="同名", remaining_minutes=200, deadline=DEADLINE, priority=20)
        later = dict(task_id="B", name="同名", remaining_minutes=1, deadline="2026-10-30 12:00", priority=20)
        result = self.check([urgent, later], [window(NOW, "2026-10-30 12:00")])
        self.assertEqual(len(result["recommendations"]["drop_or_delegate"]), 1)

    def test_interval_algebra_matches_independent_minute_grid(self):
        # Deterministic randomized oracle: count occupied individual minutes,
        # independent of the interval merge/subtraction implementation.
        randomizer = random.Random(602410)
        origin = parse_dt(NOW)
        for _ in range(100):
            free_pairs = [sorted(randomizer.sample(range(181), 2)) for _ in range(6)]
            busy_pairs = [sorted(randomizer.sample(range(181), 2)) for _ in range(6)]
            intervals = lambda pairs: [(origin + timedelta(minutes=a), origin + timedelta(minutes=b)) for a, b in pairs]
            actual = free_intervals(origin, origin + timedelta(minutes=180), intervals(free_pairs), intervals(busy_pairs))
            grid = sum(any(a <= m < b for a, b in free_pairs) and not any(a <= m < b for a, b in busy_pairs) for m in range(180))
            self.assertEqual(sum((b-a).total_seconds()/60 for a, b in actual), grid)
            self.assertEqual(merge_intervals(actual), actual)


if __name__ == "__main__":
    unittest.main()
