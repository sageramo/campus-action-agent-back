"""Adversarial provenance/date boundaries for the model candidate contract.

These deterministic tests do not measure a live model's extraction accuracy.
"""
from copy import deepcopy
import unittest

from campus_agent.candidates import normalize_extraction, validate_extraction_input


def model_output(text="2026年10月25日18:00前提交申请表", *, kind="flexible", time_quote="2026年10月25日18:00", **changes):
    candidate = {
        "title": "提交申请表", "kind": kind, "action_quote": text,
        "time_quote": time_quote, "location_quote": None,
        "materials_quotes": [], "uncertainties": [],
    }
    candidate.update(changes)
    return {"schema_version": 1, "candidates": [candidate], "warnings": []}


class ExtractionInputTests(unittest.TestCase):
    def test_input_preserves_original_whitespace_and_does_not_supply_today(self):
        self.assertEqual(validate_extraction_input({"text": " 原文\n"}), (" 原文\n", None, "Asia/Shanghai"))
        self.assertEqual(validate_extraction_input({"text": "原文", "reference_date": "2028-02-29", "timezone": "Asia/Shanghai"}), ("原文", "2028-02-29", "Asia/Shanghai"))

    def test_invalid_and_unknown_request_data_rejected(self):
        for payload in [None, [], "原文", {}, {"text": None}, {"text": True}, {"text": " \n"},
                        {"text": "原文", "model": "other"}, {"text": "原文", "timezone": None},
                        {"text": "原文", "timezone": "UTC"}, {"text": "原文", "reference_date": "2026-02-29"},
                        {"text": "原文", "reference_date": "2026-1-1"}, {"text": "原文", "reference_date": "2026-10-25T00:00:00+08:00"},
                        {"text": "原文", "reference_date": 20261025}, {"text": "原文", "reference_date": ""}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_extraction_input(payload)

    def test_character_budget_is_enforced(self):
        self.assertEqual(len(validate_extraction_input({"text": "字" * 20000})[0]), 20000)
        with self.assertRaises(ValueError):
            validate_extraction_input({"text": "字" * 20001})


_UNSET = object()


class ExtractionContractTests(unittest.TestCase):
    def normalize(self, raw=_UNSET, text="2026年10月25日18:00前提交申请表", **kwargs):
        return normalize_extraction(model_output(text) if raw is _UNSET else raw, text, **kwargs)

    def test_candidate_has_only_extraction_data_and_is_never_confirmed(self):
        result = self.normalize()
        self.assertEqual(set(result), {"schema_version", "provider", "parsing_status", "candidates", "warnings"})
        self.assertEqual((result["schema_version"], result["provider"], result["parsing_status"]), (1, "dify", "needs_confirmation"))
        candidate = result["candidates"][0]
        self.assertEqual(set(candidate), {"title", "kind", "deadline_at", "fixed_start_at", "fixed_end_at", "visit_windows", "location", "materials", "evidence", "uncertainties"})
        self.assertEqual(candidate["deadline_at"], "2026-10-25T18:00:00+08:00")
        for evidence in candidate["evidence"]:
            self.assertEqual(set(evidence), {"field", "origin", "quote"})
            self.assertEqual(evidence["origin"], "source_text")
            self.assertIn(evidence["quote"], "2026年10月25日18:00前提交申请表")

    def test_raw_data_is_not_mutated(self):
        raw = model_output()
        original = deepcopy(raw)
        self.normalize(raw)
        self.assertEqual(raw, original)

    def test_extra_user_owned_fields_are_rejected_instead_of_dropped(self):
        for field, value in [("priority", 100), ("remaining_minutes", 20), ("must_do", True), ("status", "confirmed"), ("confirmed_fields", ["title"]), ("source_id", "invented"), ("id", "new-task"), ("deadline_at", "2026-10-25T18:00:00+08:00")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.normalize(model_output(**{field: value}))
        raw = model_output()
        raw["model"] = "anything"
        with self.assertRaises(ValueError):
            self.normalize(raw)

    def test_all_quotes_must_be_exact_original_substrings(self):
        for field, value in [("action_quote", "原文没有的行动"), ("time_quote", "2026年10月26日18:00"), ("location_quote", "主楼405"), ("materials_quotes", ["身份证"])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.normalize(model_output(**{field: value}))

    def test_forged_quote_in_any_candidate_rejects_the_whole_output(self):
        raw = model_output()
        raw["candidates"].append(deepcopy(raw["candidates"][0]))
        raw["candidates"][1]["action_quote"] = "伪造"
        with self.assertRaises(ValueError):
            self.normalize(raw)

    def test_required_fields_invalid_types_and_unsupported_kinds_rejected(self):
        for field in model_output()["candidates"][0]:
            raw = model_output()
            del raw["candidates"][0][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                self.normalize(raw)
        for raw in [[], None, "```json\n{}\n```", {"schema_version": True, "candidates": [], "warnings": []},
                    model_output(kind="recurring"), model_output(title=""), model_output(title=" \n"),
                    model_output(time_quote=""), model_output(location_quote=" "),
                    model_output(materials_quotes=[None]), model_output(uncertainties=[""])]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.normalize(raw)

    def test_model_output_budgets(self):
        for changes in [{"title": "字" * 201}, {"action_quote": "字" * 3001}, {"time_quote": "字" * 3001},
                        {"location_quote": "字" * 501}, {"materials_quotes": ["字"] * 31},
                        {"materials_quotes": ["字" * 501]}, {"uncertainties": ["提示"] * 21},
                        {"uncertainties": ["字" * 1001]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.normalize(model_output(**changes))
        raw = model_output()
        raw["candidates"] *= 41
        with self.assertRaises(ValueError):
            self.normalize(raw)

    def test_notes_and_materials_are_deduplicated_and_exact_quotes_retained(self):
        text = "2026年10月25日18:00前提交申请表，地点为主楼405。"
        raw = model_output(text, location_quote="主楼405", materials_quotes=["申请表", "申请表"], uncertainties=["核对条件", "核对条件"])
        raw["warnings"] = ["原通知待核对", "原通知待核对"]
        result = self.normalize(raw, text)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["location"], "主楼405")
        self.assertEqual(candidate["materials"], ["申请表"])
        self.assertEqual(candidate["uncertainties"].count("核对条件"), 1)
        self.assertEqual(result["warnings"].count("原通知待核对"), 1)
        self.assertIn({"field": "location", "origin": "source_text", "quote": "主楼405"}, candidate["evidence"])

    def test_material_negation_is_not_solved_by_substring_provenance(self):
        text = "无需提交身份证，只需提交申请表。"
        raw = model_output(text, time_quote=None, materials_quotes=["身份证", "申请表"])
        candidate = self.normalize(raw, text)["candidates"][0]
        # An exact substring is not a semantic entitlement to carry a material.
        self.assertEqual(candidate["materials"], ["身份证", "申请表"])
        self.assertTrue(any("否定或条件" in note for note in candidate["uncertainties"]))
        self.assertTrue(any("否定条件" in note for note in self.normalize(raw, text)["warnings"]))

    def test_empty_result_is_explicit_and_not_replaced_with_invented_task(self):
        result = self.normalize({"schema_version": 1, "candidates": [], "warnings": []})
        self.assertEqual(result["candidates"], [])
        self.assertTrue(any("未得到候选" in note for note in result["warnings"]))


class ExtractionTimeTests(unittest.TestCase):
    def candidate(self, text, *, time_quote=None, kind="flexible", reference_date=None):
        raw = model_output(text, kind=kind, time_quote=text if time_quote is None else time_quote)
        return normalize_extraction(raw, text, reference_date)["candidates"][0]

    def assert_no_time(self, candidate):
        self.assertIsNone(candidate["deadline_at"])
        self.assertIsNone(candidate["fixed_start_at"])
        self.assertIsNone(candidate["fixed_end_at"])
        self.assertEqual(candidate["visit_windows"], [])
        self.assertTrue(candidate["uncertainties"])

    def test_missing_year_without_reference_is_unresolved_and_evidence_retained(self):
        candidate = self.candidate("10月25日18:00前提交")
        self.assert_no_time(candidate)
        self.assertTrue(any("服务器当前年份" in note for note in candidate["uncertainties"]))
        self.assertIn({"field": "deadline_at", "origin": "source_text", "quote": "10月25日18:00前提交"}, candidate["evidence"])

    def test_missing_year_uses_user_reference_year_and_warns(self):
        candidate = self.candidate("10月25日18:00前提交", reference_date="2032-10-24")
        self.assertEqual(candidate["deadline_at"], "2032-10-25T18:00:00+08:00")
        self.assertTrue(any("未给年份" in note and "2032" in note for note in candidate["uncertainties"]))
        self.assertFalse(any("2000" in note for note in candidate["uncertainties"]))

    def test_relative_day_and_week_are_anchored_only_to_supplied_date(self):
        for text, expected in [("明天下午三点前提交", "2032-01-01T15:00:00+08:00"), ("下周一上午9点前提交", "2032-01-05T09:00:00+08:00"), ("明年1月1日9点前提交", "2032-01-01T09:00:00+08:00")]:
            with self.subTest(text=text):
                self.assert_no_time(self.candidate(text))
                candidate = self.candidate(text, reference_date="2031-12-31")
                self.assertEqual(candidate["deadline_at"], expected)
                self.assertTrue(any("参照日" in note or "相对年份" in note for note in candidate["uncertainties"]))

    def test_fixed_interval_uses_both_endpoints_and_midnight_rollover(self):
        for text, expected_end in [("2026年10月25日18:00至20:00开会", "2026-10-25T20:00:00+08:00"), ("2026年10月25日18:00至次日01:00值班", "2026-10-26T01:00:00+08:00"), ("2026年10月25日18:00至2026年10月26日01:00值班", "2026-10-26T01:00:00+08:00"), ("2026年10月25日18:00至24:00值班", "2026-10-26T00:00:00+08:00")]:
            with self.subTest(text=text):
                candidate = self.candidate(text, kind="fixed")
                self.assertEqual(candidate["fixed_start_at"], "2026-10-25T18:00:00+08:00")
                self.assertEqual(candidate["fixed_end_at"], expected_end)
                self.assertIsNone(candidate["deadline_at"])

    def test_date_only_range_or_clock_only_never_becomes_full_day_or_deadline(self):
        for text in ["2026年10月25日截止", "2026年10月25日至2026年10月26日截止", "18:00前提交", "2026年10月25日至2026年10月26日9:00截止"]:
            for kind in ("flexible", "fixed", "visit_window"):
                with self.subTest(text=text, kind=kind):
                    self.assert_no_time(self.candidate(text, kind=kind))

    def test_invalid_or_unresolved_extra_time_prevents_partial_silent_acceptance(self):
        for text in ["2026年2月29日18:00截止", "2026年10月25日24:01截止", "2026年10月25日晚上12点截止", "2026年10月25日18:00至17:00开会", "2026年10月25日18:00及2026年2月29日18:00截止", "2026年10月25日18:00，下下周一9点截止"]:
            with self.subTest(text=text):
                self.assert_no_time(self.candidate(text, reference_date="2026-10-24"))

    def test_multiple_times_and_noncontinuous_windows_do_not_merge(self):
        for text in ["2026年10月25日9:00至10:00及14:00至15:00办理", "2026年10月25日9:00至10:00、2026年10月26日14:00至15:00办理", "2026年10月25日9:00或10:00截止"]:
            with self.subTest(text=text):
                self.assert_no_time(self.candidate(text, kind="visit_window"))

    def test_single_visit_window_never_becomes_fixed_occupation(self):
        candidate = self.candidate("2026年10月25日9:00至16:00办理", kind="visit_window")
        self.assertEqual(candidate["visit_windows"], [{"start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T16:00:00+08:00"}])
        self.assertIsNone(candidate["fixed_start_at"])
        self.assertIsNone(candidate["fixed_end_at"])
        self.assertIsNone(candidate["deadline_at"])
        self.assertTrue(any("不代表全程占用" in note for note in candidate["uncertainties"]))

    def test_kind_time_mismatch_does_not_repurpose_interval_or_point(self):
        for kind, text in [("visit_window", "2026年10月25日18:00办理"), ("flexible", "2026年10月25日18:00至20:00提交"), ("milestone", "2026年10月25日18:00至20:00公布")]:
            with self.subTest(kind=kind):
                self.assert_no_time(self.candidate(text, kind=kind))

    def test_registration_and_visit_actions_mislabelled_fixed_are_blocked(self):
        for text in ["报名时间为2026年10月25日9:00至16:00", "2026年10月25日9:00至16:00任选时间办理", "2026年10月25日9:00至16:00提交申请"]:
            with self.subTest(text=text):
                candidate = self.candidate(text, kind="fixed", time_quote="2026年10月25日9:00至16:00")
                self.assert_no_time(candidate)

    def test_recurrence_cannot_be_hidden_by_shortened_time_quote(self):
        for text, quote in [("2026年10月25日至2026年10月26日每天9:00至16:00办理", "2026年10月26日每天9:00至16:00"), ("每天2026年10月25日9:00至10:00值班", "2026年10月25日9:00至10:00"), ("每周安排值班，2026年10月25日9:00至10:00到场", "2026年10月25日9:00至10:00")]:
            with self.subTest(text=text):
                candidate = self.candidate(text, kind="fixed", time_quote=quote)
                self.assert_no_time(candidate)
                self.assertTrue(any("重复" in note for note in candidate["uncertainties"]))

    def test_tentative_or_approximate_qualifier_cannot_be_hidden_by_short_quote(self):
        quote = "2026年10月25日9:00至10:00"
        for text in ["暂定" + quote + "开会", quote + "左右开会", "预计" + quote + "开会", "约" + quote + "开会"]:
            with self.subTest(text=text):
                self.assert_no_time(self.candidate(text, kind="fixed", time_quote=quote))

    def test_missing_and_very_long_unresolved_time_quote_preserved_with_bounded_notes(self):
        text = "10月25日9:00" + "待补充" * 990
        candidate = self.candidate(text)
        self.assert_no_time(candidate)
        self.assertTrue(all(len(note) <= 1000 for note in candidate["uncertainties"]))
        self.assertLessEqual(len(candidate["uncertainties"]), 100)
        self.assertIn(text, [evidence["quote"] for evidence in candidate["evidence"]])
        raw = model_output("未公布日期的事项", time_quote=None)
        self.assert_no_time(normalize_extraction(raw, "未公布日期的事项")["candidates"][0])


if __name__ == "__main__":
    unittest.main()
