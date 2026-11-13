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
        self.assertEqual([item["title"] for item in result["events"]], ["学术讲座（主楼报告厅）"])
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
