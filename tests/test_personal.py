"""研墨 also takes one-line personal reminders; they become 个人任务 or a self-set 约定."""
from unittest import TestCase
from unittest.mock import patch

from campus_agent.server import process_post

REF = "2026-11-13"  # a Friday


def extract(text, mode="rules"):
    return process_post("/api/v3/extract", {"text": text, "reference_date": REF, "mode": mode})


class PersonalReminderTests(TestCase):
    def test_a_reminder_for_today_is_a_task_pinned_to_today(self):
        result = extract("提醒我今天要完成实验报告")
        (task,) = result["tasks"]
        self.assertEqual((task["title"], task["deadline_at"], task["earliest_start_at"]),
                         ("完成实验报告", "2026-11-13T23:59:00+08:00", "2026-11-13T00:00:00+08:00"))
        self.assertIn("个人任务", task["notes"])
        self.assertEqual(task["source_id"], result["source"]["id"])

    def test_a_clock_time_with_somewhere_to_go_is_a_self_set_appointment(self):
        (event,) = extract("明天下午3点去取快递")["events"]
        self.assertEqual((event["title"], event["kind"], event["start_at"], event["end_at"]),
                         ("去取快递", "appointment", "2026-11-14T15:00:00+08:00", None))

    def test_before_means_a_deadline_and_a_stated_duration_is_the_estimate(self):
        (task,) = extract("帮我记一下：周三晚上8点前把作品集发给学姐，大概2小时")["tasks"]
        self.assertEqual((task["deadline_at"], task["earliest_start_at"], task["estimate_minutes"]),
                         ("2026-11-18T20:00:00+08:00", None, 120))
        self.assertEqual(extract("下周一上午10点去教务处盖章")["events"][0]["start_at"], "2026-11-16T10:00:00+08:00")

    def test_no_day_stays_undefined(self):
        (task,) = extract("记得买牛奶")["tasks"]
        self.assertEqual((task["title"], task["deadline_at"]), ("买牛奶", None))

    def test_notices_are_left_to_the_notice_extractor_and_mixed_text_gets_both(self):
        (event,) = extract("11月20日19:00 学术讲座")["events"]
        self.assertNotIn("个人任务", event["notes"])
        result = extract("【学生会通知】\n2026年11月20日19:00 学术讲座（主楼报告厅）。\n提醒我明天要买打印纸")
        self.assertEqual([(item["title"], item["location"]) for item in result["events"]], [("学术讲座", "主楼报告厅")])
        self.assertEqual([item["title"] for item in result["tasks"]], ["买打印纸"])
        ids = {item["source_id"] for item in [*result["events"], *result["tasks"]]}
        self.assertEqual(ids, {result["source"]["id"]})
        self.assertIn("提醒我明天要买打印纸", result["source"]["text"])

    def test_model_mode_only_sees_the_notice_part(self):
        seen = {}
        def fake(request):
            seen["text"] = request["text"]
            return {"candidates": [], "warnings": []}
        with patch("campus_agent.server.extract_notice", side_effect=fake):
            result = extract("2026年11月20日19:00 学术讲座。\n提醒我明天要买打印纸", mode="model")
        self.assertEqual(seen["text"], "2026年11月20日19:00 学术讲座")
        self.assertEqual(result["tasks"][0]["title"], "买打印纸")

    def test_personal_activity_uses_the_full_interval_or_explicit_duration(self):
        for line, title, start, end in [
            ("提醒我明天9点到11点开社团例会", "开社团例会", "09:00", "11:00"),
            ("提醒我明天下午三点开会，1小时30分钟", "开会", "15:00", "16:30"),
        ]:
            with self.subTest(line=line):
                result = extract(line)
                self.assertEqual(result["tasks"], [])
                (event,) = result["events"]
                self.assertEqual((event["title"], event["start_at"], event["end_at"]),
                                 (title, f"2026-11-14T{start}:00+08:00", f"2026-11-14T{end}:00+08:00"))

    def test_midnight_and_uncertain_dates_never_create_a_reversed_work_window(self):
        (task,) = extract("提醒我今天凌晨0点交作业")["tasks"]
        self.assertIsNone(task["earliest_start_at"])
        self.assertEqual(task["deadline_at"], f"{REF}T00:00:00+08:00")
        (task,) = extract("提醒我2027-01-04 18:00交作品")["tasks"]
        self.assertEqual(task["deadline_at"], "2027-01-04T18:00:00+08:00")
        result = extract("提醒我明晚25点交报告")
        self.assertIsNone(result["tasks"][0]["deadline_at"])
        self.assertTrue(any("时间待书" in warning for warning in result["warnings"]))

    def test_before_submit_is_a_deadline_without_stray_prefix(self):
        result = process_post("/api/v3/extract", {"text": "提醒我周三18:00前提交摄影社报名表，预计30分钟",
                                                  "reference_date": "2026-11-23", "mode": "rules"})
        (task,) = result["tasks"]
        self.assertEqual((task["title"], task["deadline_at"], task["earliest_start_at"], task["estimate_minutes"]),
                         ("提交摄影社报名表", "2026-11-25T18:00:00+08:00", None, 30))

    def test_mixed_multiline_notice_preserves_original_separators_for_evidence(self):
        from campus_agent.layers import validate_snapshot_v3
        from campus_agent.personal import with_personal

        original = "提醒我今天写实验报告\n招新说明会于2026年11月25日14:30-15:30举行\n地点为学活204"

        def fake_extract(rest):
            return {
                "events": [{"id": "e1", "title": "参加招新说明会", "start_at": "2026-11-25T14:30:00+08:00",
                            "end_at": "2026-11-25T15:30:00+08:00", "location": "学活204", "status": "active",
                            "source_id": "s-inner", "notes": [],
                            "evidence": [{"field": "title", "origin": "source_text", "source_id": "s-inner", "quote": rest}]}],
                "tasks": [], "warnings": [], "mode": "model",
            }

        imported = with_personal(original, "2026-11-25", fake_extract)
        self.assertEqual(imported["events"][0]["evidence"][0]["quote"], "招新说明会于2026年11月25日14:30-15:30举行\n地点为学活204")
        validate_snapshot_v3({
            "schema_version": 3, "revision": 1, "timezone": "Asia/Shanghai", "reference_time": "2026-11-25T14:00:00+08:00",
            "sources": [imported["source"]], "daily_window": [{"start": "14:00", "end": "18:00"}] * 7,
            "routines_confirmed": True, "routines": [], "free_adjustments": [],
            "events": imported["events"], "tasks": imported["tasks"],
        })
        # When a personal line sits in the middle, quotes spanning the gap are filtered instead of failing v3 validation.
        sandwiched = "2026年11月25日14:30-15:30举行招新说明会\n提醒我今天写实验报告\n地点为学活204"
        imported2 = with_personal(sandwiched, "2026-11-25", fake_extract)
        self.assertEqual(imported2["events"][0]["evidence"], [])
        validate_snapshot_v3({
            "schema_version": 3, "revision": 1, "timezone": "Asia/Shanghai", "reference_time": "2026-11-25T14:00:00+08:00",
            "sources": [imported2["source"]], "daily_window": [{"start": "14:00", "end": "18:00"}] * 7,
            "routines_confirmed": True, "routines": [], "free_adjustments": [],
            "events": imported2["events"], "tasks": imported2["tasks"],
        })

    def test_weekend_personal_task_resolves_to_saturday_sunday_and_questions_are_ignored(self):
        # 2026-11-18 is Wednesday -> this weekend is Sat 2026-11-21 to Sun 2026-11-22
        result = process_post("/api/v3/extract", {"text": "我这周末要写高数作业", "reference_date": "2026-11-18", "mode": "rules"})
        (task,) = result["tasks"]
        self.assertEqual(task["title"], "写高数作业")
        self.assertEqual(task["earliest_start_at"], "2026-11-21T00:00:00+08:00")
        self.assertEqual(task["deadline_at"], "2026-11-22T23:59:00+08:00")
        self.assertIn("个人任务", task["notes"])
        # Interrogative weekend question must NOT become a personal task
        from campus_agent.personal import split_personal
        events, tasks, rest = split_personal("周末我有没有时间参加社团活动", "2026-11-18", "s-q")
        self.assertEqual((events, tasks), ([], []))
        self.assertEqual(rest, "周末我有没有时间参加社团活动")
