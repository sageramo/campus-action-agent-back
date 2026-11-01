"""Boundary and adversarial examples for candidates, never model accuracy claims."""
from datetime import timedelta
import unittest

from campus_agent.extractor import parse_notice_text
from campus_agent.time_parser import extract_time_candidates
from campus_agent.time_utils import parse_dt, require_datetime


class DateClockTests(unittest.TestCase):
    def test_midnight_rolls_over_calendar_boundaries(self):
        for raw, expected in [
            ("2026-10-25 24:00", "2026-10-26 00:00"),
            ("2026-12-31 24:00:00", "2027-01-01 00:00"),
            ("2028-02-28T24:00+08:00", "2028-02-29 00:00"),
        ]:
            with self.subTest(raw=raw):
                result = parse_dt(raw)
                self.assertEqual(result.strftime("%Y-%m-%d %H:%M"), expected)
                self.assertEqual(result.utcoffset(), timedelta(hours=8))

    def test_invalid_times_are_not_repaired_to_valid_ones(self):
        for raw in ["2026-02-29 12:00", "2026-10-25 24:01", "2026-10-25 24:00:01", "2026-10-25 25:00", "2026-10-25T12:00+08:99", "2026-10-25T12:00+99:00", "20261025", "2026-W43-7", "tomorrow", None]:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_dt(raw))

    def test_offset_equivalence(self):
        self.assertEqual(parse_dt("2026-10-25T01:00Z"), parse_dt("2026-10-25 09:00"))

    def test_date_only_cannot_silently_become_a_deadline(self):
        with self.assertRaises(ValueError):
            require_datetime("2026-10-25", "deadline")


class TimeCandidateTests(unittest.TestCase):
    def parse(self, text, **kwargs):
        return parse_notice_text(text, **kwargs).to_dict()

    def test_periods_and_chinese_numerals(self):
        samples = [
            ("下午三点半", "15:30"), ("晚上七点一刻", "19:15"),
            ("中午12点", "12:00"), ("凌晨12点", "00:00"),
            ("上午十一点三刻", "11:45"), ("下午15:30", "15:30"),
        ]
        for raw, expected in samples:
            with self.subTest(raw=raw):
                points = self.parse(f"2026年10月25日{raw}报名截止")["time_points"]
                self.assertEqual(points[0]["value"], f"2026-10-25 {expected}")

    def test_interval_retains_both_endpoints_and_original_evidence(self):
        text = "比赛时间为2026年10月26日18:00-20:00。"
        point = self.parse(text)["time_points"][0]
        self.assertEqual((point["value"], point["end_value"], point["kind"]), ("2026-10-26 18:00", "2026-10-26 20:00", "interval"))
        self.assertEqual(text[point["source_start"]:point["source_end"]], point["evidence"])

    def test_end_clock_inherits_period(self):
        point = self.parse("2026年10月25日下午3点至5点开会")["time_points"][0]
        self.assertEqual((point["value"], point["end_value"]), ("2026-10-25 15:00", "2026-10-25 17:00"))

    def test_explicit_cross_midnight_and_cross_day_ranges(self):
        for text in ["2026年10月25日23:00至次日01:00值班", "2026年10月25日23:00至2026年10月26日01:00值班"]:
            with self.subTest(text=text):
                points = self.parse(text)["time_points"]
                self.assertEqual(len(points), 1)
                self.assertEqual(points[0]["end_value"], "2026-10-26 01:00")

    def test_backwards_intervals_require_confirmation(self):
        for text in ["2026年10月25日23:00-01:00", "2026年10月26日18:00至2026年10月25日20:00"]:
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertFalse(result["time_points"])
                self.assertTrue(result["warnings"])

    def test_relative_dates_use_declared_reference_not_machine_day(self):
        samples = [("明天下午3点", "2031-12-31 15:00"), ("后天中午12点", "2032-01-01 12:00")]
        for text, expected in samples:
            with self.subTest(text=text):
                point = self.parse(text, reference_date="2031-12-30")["time_points"][0]
                self.assertEqual(point["value"], expected)

    def test_relative_week_and_year_are_anchored(self):
        self.assertEqual(self.parse("下周一上午9点", reference_date="2026-10-24")["time_points"][0]["value"], "2026-10-26 09:00")
        self.assertEqual(self.parse("明年1月1日9点", reference_date="2026-10-24")["time_points"][0]["value"], "2027-01-01 09:00")

    def test_relative_without_reference_is_unresolved(self):
        for text in ["明天下午3点报名", "下周一9点开会", "明年1月1日9点"]:
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertFalse(result["time_points"])
                self.assertTrue(any("reference_date" in item for item in result["warnings"]))

    def test_unsupported_next_next_week_is_not_read_as_next_week(self):
        result = self.parse("下下周一9点开会", reference_date="2026-10-24")
        self.assertFalse(any(point["precision"] == "datetime" for point in result["time_points"]))
        self.assertTrue(result["warnings"])

    def test_reference_date_and_timezone_validation(self):
        for kwargs in [{"reference_date": "2026-02-30"}, {"reference_date": "tomorrow"}, {"timezone": "bad-zone"}, {"reference_date": 20261024}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.parse("明天3点", **kwargs)

    def test_missing_year_is_flagged_and_not_hardcoded(self):
        result = self.parse("10月25日9:00", reference_date="2030-10-24")
        self.assertEqual(result["time_points"][0]["value"], "2030-10-25 09:00")
        self.assertTrue(any("未给年份" in item for item in result["warnings"]))

    def test_full_numeric_dates_do_not_drop_the_year(self):
        for text in ["2029/10/25 15:00", "2029.10.25 15:00", "2029-10-25 15:00"]:
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)["time_points"][0]["value"], "2029-10-25 15:00")

    def test_date_only_and_time_only_remain_imprecise(self):
        date_point = self.parse("2026年10月25日前提交")["time_points"][0]
        self.assertEqual((date_point["value"], date_point["precision"]), ("2026-10-25", "date"))
        time_point = self.parse("比赛18:00-20:00")["time_points"][0]
        self.assertEqual((time_point["value"], time_point["end_value"], time_point["precision"]), ("18:00", "20:00", "time"))

    def test_invalid_or_ambiguous_clocks_are_not_valid_candidates(self):
        for text in ["2026年13月45日99:99", "2026年10月25日99:99", "2026年10月25日24:01", "2026年10月25日晚上12点", "2026年10月25日下午0点"]:
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertFalse(result["time_points"])
                self.assertTrue(result["warnings"])

    def test_date_range_does_not_mean_continuous_fixed_occupation(self):
        point = self.parse("2026年10月25日至2026年10月26日提交材料")["time_points"][0]
        self.assertEqual((point["kind"], point["precision"]), ("date_range", "date"))

    def test_recurring_rules_are_not_promoted_to_fixed_blocks(self):
        result = self.parse("2026年10月25日至10月26日每天9:00-16:00办理")
        self.assertTrue(result["warnings"])
        self.assertTrue(all(item["kind"] == "recurring_unexpanded" for item in result["time_points"]))

    def test_distinct_sessions_on_one_day_are_not_lost(self):
        points = self.parse("2026年10月25日9:00-10:00及14:00-15:00开会")["time_points"]
        self.assertEqual(len(points), 2)
        self.assertEqual(points[1]["end_value"], "2026-10-25 15:00")

    def test_clause_boundary_prevents_registration_type_leak(self):
        points = self.parse("请于2026年10月25日15:00前报名，讲座于2026年10月26日18:00举行")["time_points"]
        self.assertEqual([point["time_type"] for point in points], ["报名截止", "活动/比赛时间"])

    def test_negation_and_multiple_locations_are_preserved_honestly(self):
        result = self.parse("无需提交身份证和学生证，只需提交申请表。地点为学院201。答辩地点为主楼405。")
        self.assertNotIn("身份证", result["materials"])
        self.assertNotIn("学生证", result["materials"])
        self.assertIn("申请表", result["materials"])
        self.assertEqual(result["locations"], ["学院201", "主楼405"])
        self.assertIsNone(result["location"])
        self.assertIn("地点与事项对应关系", result["missing_fields"])

    def test_limit_is_explicit_and_evidence_uses_original_offsets(self):
        warnings = []
        points = extract_time_candidates("2026年10月25日9:00。\n2026年10月26日9:00。", limit=1, warnings=warnings)
        self.assertEqual(len(points), 1)
        self.assertTrue(any("截断" in note for note in warnings))


if __name__ == "__main__":
    unittest.main()
