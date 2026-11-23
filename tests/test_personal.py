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

    def test_day_periods_morning_afternoon_and_comma_split_inherit_day(self):
        from campus_agent.layers import validate_snapshot_v3

        # Exact user screenshot case: 2026-11-23 is Monday -> 这周四 is 2026-11-26
        result = process_post("/api/v3/extract", {"text": "这周四上午要彩排，下午要表演", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual(result["events"], [])
        self.assertEqual(len(result["tasks"]), 2)
        t1, t2 = result["tasks"]
        self.assertEqual((t1["title"], t1["earliest_start_at"], t1["deadline_at"]),
                         ("彩排", "2026-11-26T08:00:00+08:00", "2026-11-26T12:00:00+08:00"))
        self.assertEqual((t2["title"], t2["earliest_start_at"], t2["deadline_at"]),
                         ("表演", "2026-11-26T13:00:00+08:00", "2026-11-26T18:00:00+08:00"))
        # Evidence quotes must pass strict v3 validation
        validate_snapshot_v3({
            "schema_version": 3, "revision": 1, "timezone": "Asia/Shanghai", "reference_time": "2026-11-23T15:00:00+08:00",
            "sources": [result["source"]], "daily_window": [{"start": "08:00", "end": "23:00"}] * 7,
            "routines_confirmed": True, "routines": [], "free_adjustments": [],
            "events": result["events"], "tasks": result["tasks"],
        })

        # Multi-period + mixed event/task + deadline-before-period + ASCII comma modifier
        mixed = process_post("/api/v3/extract", {"text": "这周四上午要彩排，下午3点到5点正式表演", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(t["title"], t["earliest_start_at"], t["deadline_at"]) for t in mixed["tasks"]],
                         [("彩排", "2026-11-26T08:00:00+08:00", "2026-11-26T12:00:00+08:00")])
        self.assertEqual([(e["title"], e["start_at"], e["end_at"]) for e in mixed["events"]],
                         [("正式表演", "2026-11-26T15:00:00+08:00", "2026-11-26T17:00:00+08:00")])

        before_period = process_post("/api/v3/extract", {"text": "提醒我周五上午前交报告,大概1小时", "reference_date": "2026-11-23", "mode": "rules"})
        (bp,) = before_period["tasks"]
        self.assertEqual((bp["title"], bp["earliest_start_at"], bp["deadline_at"], bp["estimate_minutes"]),
                         ("交报告", None, "2026-11-27T12:00:00+08:00", 60))
        self.assertIn(bp["evidence"][0]["quote"], before_period["source"]["text"])

    def test_period_anchored_time_ranges_become_clean_appointments(self):
        # Real user trial case: "周一中午到晚上五点开会" (2026-11-23 is Monday)
        r1 = process_post("/api/v3/extract", {"text": "周一中午到晚上五点开会", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual(r1["tasks"], [])
        self.assertEqual([(e["title"], e["kind"], e["start_at"], e["end_at"]) for e in r1["events"]],
                         [("开会", "appointment", "2026-11-23T12:00:00+08:00", "2026-11-23T17:00:00+08:00")])

        r2 = process_post("/api/v3/extract", {"text": "周二中午到下午3点彩排", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"], e["end_at"]) for e in r2["events"]],
                         [("彩排", "2026-11-24T12:00:00+08:00", "2026-11-24T15:00:00+08:00")])

        r3 = process_post("/api/v3/extract", {"text": "周三下午3点到晚上彩排", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"], e["end_at"]) for e in r3["events"]],
                         [("彩排", "2026-11-25T15:00:00+08:00", "2026-11-25T22:00:00+08:00")])

        r4 = process_post("/api/v3/extract", {"text": "周五上午到下午排练", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"], e["end_at"]) for e in r4["events"]],
                         [("排练", "2026-11-27T08:00:00+08:00", "2026-11-27T18:00:00+08:00")])

        # Bare time-only fragments must never become fake untimed tasks like "下午五点" or "中午到"
        for bare in ("下午五点", "中午到晚上五点"):
            with self.assertRaises(ValueError):
                process_post("/api/v3/extract", {"text": bare, "reference_date": "2026-11-23", "mode": "rules"})

        # Long single-line notice whose relative date lacks reference_date must never use literal "user input" as title
        long_notice = "各位好，关于组织二级关工委赴中央礼品文物管理中心开展学习教育的通知，如有意愿参加，请于今天前联系孙剑仪老师并扫码填写个人报名信息。"
        res_long = process_post("/api/v3/extract", {"text": long_notice, "reference_date": None, "mode": "rules"})
        self.assertEqual(len(res_long["tasks"]), 1)
        self.assertNotEqual(res_long["tasks"][0]["title"], "user input")
        self.assertEqual(res_long["tasks"][0]["title"], "关于组织二级关工委赴中央礼品文物管理中心开展学习教育的通知")

    def test_leading_wo_zhinei_going_events_and_space_weekday_notices(self):
        # Leading "我" must still route through personal parser without dropping clauses or falling back to untimed tasks
        r_wo = process_post("/api/v3/extract", {"text": "我这周四上午要彩排，下午要表演", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual(
            [(t["title"], t["earliest_start_at"], t["deadline_at"]) for t in r_wo["tasks"]],
            [
                ("彩排", "2026-11-26T08:00:00+08:00", "2026-11-26T12:00:00+08:00"),
                ("表演", "2026-11-26T13:00:00+08:00", "2026-11-26T18:00:00+08:00"),
            ],
        )
        r_wo_ev = process_post("/api/v3/extract", {"text": "我明天下午3点去取快递", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"]) for e in r_wo_ev["events"]], [("去取快递", "2026-11-24T15:00:00+08:00")])

        # "今天之内" strips "之内" cleanly and marks deadline
        r_in = process_post("/api/v3/extract", {"text": "提醒我今天之内完成实验报告", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(t["title"], t["deadline_at"]) for t in r_in["tasks"]], [("完成实验报告", "2026-11-23T23:59:00+08:00")])

        # Common student show-up events become appointments, not deadlines
        r_def = process_post("/api/v3/extract", {"text": "明天下午3点答辩", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"]) for e in r_def["events"]], [("答辩", "2026-11-24T15:00:00+08:00")])
        r_mov = process_post("/api/v3/extract", {"text": "周五晚上7点看电影", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"]) for e in r_mov["events"]], [("看电影", "2026-11-27T19:00:00+08:00")])

        # Space-separated weekday in notice + roadshow / 集合 event classification
        r_sp = process_post("/api/v3/extract", {"text": "11月24日 周二 15:00在中传礼堂举办主题大课", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"], e["location"]) for e in r_sp["events"]], [("主题大课", "2026-11-24T15:00:00+08:00", "中传礼堂")])
        self.assertFalse(any("相对时间" in w for w in r_sp["warnings"]))
        r_rs = process_post("/api/v3/extract", {"text": "11月24日 14:15 智能体节路演", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"]) for e in r_rs["events"]], [("智能体节路演", "2026-11-24T14:15:00+08:00")])
        r_jh = process_post("/api/v3/extract", {"text": "11月25日08:00在南操场集合", "reference_date": "2026-11-23", "mode": "rules"})
        self.assertEqual([(e["title"], e["start_at"]) for e in r_jh["events"]], [("南操场集合", "2026-11-25T08:00:00+08:00")])
