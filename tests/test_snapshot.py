"""v2 semantic boundaries and actual HTTP responses; no browser/model calls."""
from __future__ import annotations

import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest

from campus_agent.server import CampusAgentHandler, MAX_BODY_BYTES, MAX_SNAPSHOT_BODY_BYTES, process_post
from campus_agent.snapshot import calculate_snapshot, critical_fields, validate_snapshot

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / "examples/nine-minutes.snapshot.json").read_text(encoding="utf-8"))


def snapshot():
    payload = copy.deepcopy(EXAMPLE)
    for task in payload["tasks"]:
        task["confirmed_fields"] = sorted(critical_fields(task))
    return payload


def flexible(payload, task_id="work", minutes=60, deadline="2026-10-27T12:00:00+08:00"):
    task = copy.deepcopy(payload["tasks"][0])
    task.update(id=task_id, title=task_id, source_id=None, deadline_at=deadline,
                remaining_minutes=minutes, evidence=[], confirmed_fields=[])
    task["confirmed_fields"] = sorted(critical_fields(task))
    return task


def fixed(payload, task_id, start, end):
    task = flexible(payload, task_id, 0, None)
    task.update(kind="fixed", fixed_start_at=start, fixed_end_at=end,
                splittable=None, min_block_minutes=None)
    task["confirmed_fields"] = sorted(critical_fields(task))
    return task


class SnapshotTests(unittest.TestCase):
    def test_nine_minutes_vs_three_hours_proves_shortage_and_echoes_revision(self):
        payload = snapshot()
        payload["revision"] = 72
        result = process_post("/api/v2/calculate", payload)
        self.assertEqual((result["schema_version"], result["revision"], result["status"]), (2, 72, "conflicts_detected"))
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 9)
        self.assertEqual(result["capacity_checks"][0]["known_required_minutes"], 180)

    def test_calculation_does_not_modify_snapshot(self):
        payload = snapshot()
        original = copy.deepcopy(payload)
        calculate_snapshot(payload)
        self.assertEqual(payload, original)
        result = calculate_snapshot(payload)
        self.assertNotIn("tasks", result)
        self.assertNotIn("recommendations", result)

    def test_volume_bound_pass_is_not_feasible_found(self):
        payload = snapshot()
        payload["tasks"][0]["remaining_minutes"] = 5
        result = calculate_snapshot(payload)
        self.assertEqual(result["status"], "no_conflict_detected")
        self.assertEqual(result["capacity_checks"][0]["result"], "necessary_bound_passed")
        self.assertTrue(any("不生成排程" in item for item in result["limitations"]))

    def test_empty_availability_means_zero_not_unknown(self):
        payload = snapshot()
        payload["availability"] = []
        payload["availability_confirmed"] = True
        result = calculate_snapshot(payload)
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 0)
        self.assertEqual(result["capacity_checks"][0]["basis"], "provided_availability")
        self.assertEqual(result["status"], "conflicts_detected")

    def test_empty_snapshot_is_valid_but_not_a_schedule(self):
        payload = snapshot()
        payload.update(tasks=[], availability=[], sources=[], availability_confirmed=True)
        result = calculate_snapshot(payload)
        self.assertEqual(result["status"], "no_conflict_detected")
        self.assertEqual(result["analyzed_task_ids"], [])

    def test_duplicate_task_ids_are_rejected(self):
        payload = snapshot()
        payload["tasks"].append(copy.deepcopy(payload["tasks"][0]))
        with self.assertRaisesRegex(ValueError, "id不可重复"):
            calculate_snapshot(payload)

    def test_duplicate_source_ids_are_rejected(self):
        payload = snapshot()
        payload["sources"].append(copy.deepcopy(payload["sources"][0]))
        with self.assertRaisesRegex(ValueError, "id不可重复"):
            validate_snapshot(payload)

    def test_invalid_timestamp_and_naive_reference_are_rejected(self):
        for value in ("2026-02-30T12:00:00+08:00", "2026-10-26T12:00:00", "2026-10-26T12:00:00+99:00"):
            with self.subTest(value=value):
                payload = snapshot()
                payload["reference_time"] = value
                with self.assertRaises(ValueError):
                    validate_snapshot(payload)

    def test_boolean_revision_and_minutes_are_rejected(self):
        for field in ("revision", "remaining_minutes"):
            with self.subTest(field=field):
                payload = snapshot()
                (payload if field == "revision" else payload["tasks"][0])[field] = True
                with self.assertRaises(ValueError):
                    validate_snapshot(payload)

    def test_rfc3339_lowercase_t_and_utc_offsets_calculate_same_instant(self):
        payload = snapshot()
        payload["reference_time"] = "2026-10-26t15:50:00Z"
        payload["tasks"][0]["deadline_at"] = "2026-10-26T15:59:00Z"
        self.assertEqual(calculate_snapshot(payload)["capacity_checks"][0]["available_minutes"], 9)

    def test_recomputation_reads_changed_remaining_work_and_new_revision(self):
        payload = snapshot()
        first = calculate_snapshot(payload)
        payload["tasks"][0]["remaining_minutes"] = 5
        payload["revision"] += 1
        second = calculate_snapshot(payload)
        self.assertEqual(first["status"], "conflicts_detected")
        self.assertEqual(second["status"], "no_conflict_detected")
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertEqual(second["capacity_checks"][0]["known_required_minutes"], 5)

    def test_reversed_and_zero_length_availability_are_rejected(self):
        for end in ("2026-10-26T23:40:00+08:00", "2026-10-26T23:50:00+08:00"):
            payload = snapshot()
            payload["availability"][0]["end"] = end
            with self.assertRaisesRegex(ValueError, "结束必须晚于开始"):
                validate_snapshot(payload)

    def test_reversed_fixed_range_is_rejected(self):
        payload = snapshot()
        payload["tasks"] = [fixed(payload, "lecture", "2026-10-27T11:00:00+08:00", "2026-10-27T10:00:00+08:00")]
        with self.assertRaisesRegex(ValueError, "结束必须晚于开始"):
            validate_snapshot(payload)

    def test_earliest_after_deadline_is_rejected(self):
        payload = snapshot()
        payload["tasks"][0]["earliest_start_at"] = "2026-10-27T09:00:00+08:00"
        with self.assertRaisesRegex(ValueError, "最早开始不能晚于截止"):
            validate_snapshot(payload)

    def test_unresolved_source_and_evidence_references_are_rejected(self):
        for target in ("task", "evidence"):
            payload = snapshot()
            item = payload["tasks"][0] if target == "task" else payload["tasks"][0]["evidence"][0]
            item["source_id"] = "missing"
            with self.assertRaisesRegex(ValueError, "source_id不存在"):
                validate_snapshot(payload)

    def test_fabricated_quote_is_rejected(self):
        payload = snapshot()
        payload["tasks"][0]["evidence"][0]["quote"] = "原文里不存在的时间"
        with self.assertRaisesRegex(ValueError, "原文证据不在对应来源"):
            validate_snapshot(payload)

    def test_dependency_missing_self_and_cycle_are_rejected(self):
        for mode in ("missing", "self", "cycle"):
            with self.subTest(mode=mode):
                payload = snapshot()
                first = payload["tasks"][0]
                first["depends_on"] = ["missing" if mode == "missing" else first["id"] if mode == "self" else "second"]
                if mode == "cycle":
                    second = flexible(payload, "second")
                    second["depends_on"] = [first["id"]]
                    payload["tasks"].append(second)
                with self.assertRaises(ValueError):
                    validate_snapshot(payload)

    def test_draft_workload_is_excluded_visibly_and_blocks_definitive_status(self):
        payload = snapshot()
        payload["tasks"][0].update(status="draft", remaining_minutes=None, confirmed_fields=[])
        result = calculate_snapshot(payload)
        self.assertEqual(result["status"], "information_needed")
        self.assertEqual(result["excluded_task_ids"], ["report-1"])
        self.assertEqual(result["capacity_checks"], [])
        self.assertTrue(result["missing_information"])

    def test_empty_and_whitespace_draft_titles_are_missing_information(self):
        for title in ("", "   "):
            with self.subTest(title=title):
                payload = snapshot()
                payload["tasks"][0].update(status="draft", title=title)
                result = calculate_snapshot(payload)
                self.assertEqual(result["status"], "information_needed")
                self.assertEqual(result["excluded_task_ids"], ["report-1"])
                self.assertTrue(any("title" in item["fields"] for item in result["missing_information"]))

    def test_non_draft_empty_and_whitespace_titles_remain_invalid(self):
        for title in ("", "   "):
            with self.subTest(title=title):
                payload = snapshot()
                payload["tasks"][0]["title"] = title
                with self.assertRaises(ValueError):
                    validate_snapshot(payload)

    def test_each_unconfirmed_critical_field_blocks_analysis(self):
        for field in sorted(critical_fields(snapshot()["tasks"][0])):
            with self.subTest(field=field):
                payload = snapshot()
                payload["tasks"][0]["confirmed_fields"].remove(field)
                result = calculate_snapshot(payload)
                self.assertEqual(result["status"], "information_needed")
                self.assertEqual(result["analyzed_task_ids"], [])
                self.assertIn(field, result["missing_information"][0]["fields"])

    def test_unknown_critical_value_is_not_defaulted(self):
        for field in ("must_do", "priority", "splittable", "min_block_minutes", "remaining_minutes", "deadline_at"):
            with self.subTest(field=field):
                payload = snapshot()
                payload["tasks"][0][field] = None
                result = calculate_snapshot(payload)
                self.assertEqual(result["status"], "information_needed")
                self.assertEqual(result["excluded_task_ids"], ["report-1"])

    def test_zero_minute_unfinished_flexible_needs_explicit_correction(self):
        payload = snapshot()
        payload["tasks"][0]["remaining_minutes"] = 0
        result = calculate_snapshot(payload)
        self.assertEqual(result["status"], "information_needed")
        self.assertEqual(result["analyzed_task_ids"], [])
        self.assertTrue(any("milestone" in item["reason"] for item in result["missing_information"]))

    def test_completed_task_does_not_count_and_must_have_zero_remaining(self):
        payload = snapshot()
        payload["tasks"][0].update(status="completed", remaining_minutes=0)
        result = calculate_snapshot(payload)
        self.assertEqual(result["capacity_checks"], [])
        self.assertEqual(result["inactive_task_ids"], ["report-1"])
        payload["tasks"][0]["remaining_minutes"] = 1
        with self.assertRaises(ValueError):
            validate_snapshot(payload)

    def test_volume_bound_with_draft_never_hides_excluded_work(self):
        payload = snapshot()
        payload["tasks"][0]["remaining_minutes"] = 5
        draft = flexible(payload, "draft", None)
        draft["status"] = "draft"
        payload["tasks"].append(draft)
        result = calculate_snapshot(payload)
        self.assertEqual(result["status"], "information_needed")
        self.assertEqual(result["capacity_checks"][0]["known_required_minutes"], 5)
        self.assertEqual(result["excluded_task_ids"], ["draft"])
        self.assertTrue(any("完整清单" in item for item in result["warnings"]))

    def test_proven_known_shortage_remains_visible_with_unknown_work(self):
        payload = snapshot()
        draft = flexible(payload, "draft", None)
        draft["status"] = "draft"
        payload["tasks"].append(draft)
        result = calculate_snapshot(payload)
        self.assertEqual(result["status"], "conflicts_detected")
        self.assertTrue(result["missing_information"])

    def test_unsupported_constraints_do_not_become_a_schedule(self):
        for mode in ("earliest_start_at", "depends_on", "splittable", "min_block_minutes", "uncertainties"):
            with self.subTest(mode=mode):
                payload = snapshot()
                task = payload["tasks"][0]
                task["remaining_minutes"] = 5
                if mode == "earliest_start_at":
                    task[mode] = "2026-10-26T23:55:00+08:00"
                elif mode == "depends_on":
                    previous = flexible(payload, "previous", 1, task["deadline_at"])
                    payload["tasks"].append(previous)
                    task[mode] = ["previous"]
                elif mode == "splittable":
                    task[mode] = False
                elif mode == "min_block_minutes":
                    task[mode] = 5
                else:
                    task[mode] = ["交件地点需要核实"]
                result = calculate_snapshot(payload)
                self.assertEqual(result["status"], "information_needed")
                self.assertTrue(result["capacity_checks"])
                self.assertTrue(any(mode in item["fields"] for item in result["missing_information"]))

    def test_visit_window_is_not_misrepresented_as_fixed_attendance(self):
        payload = snapshot()
        task = payload["tasks"][0]
        task.update(kind="visit_window", visit_windows=copy.deepcopy(payload["availability"]), remaining_minutes=5)
        task["confirmed_fields"] = sorted(critical_fields(task))
        result = calculate_snapshot(payload)
        self.assertEqual(result["status"], "information_needed")
        self.assertEqual(result["excluded_task_ids"], [task["id"]])
        self.assertEqual(result["capacity_checks"], [])

    def test_fixed_union_is_subtracted_once_and_fixed_work_not_double_counted(self):
        payload = snapshot()
        payload["reference_time"] = "2026-10-27T09:00:00+08:00"
        payload["availability"] = [{"start": payload["reference_time"], "end": "2026-10-27T12:00:00+08:00"}]
        payload["tasks"] = [flexible(payload),
            fixed(payload, "class", "2026-10-27T10:00:00+08:00", "2026-10-27T11:00:00+08:00"),
            fixed(payload, "meeting", "2026-10-27T10:30:00+08:00", "2026-10-27T11:30:00+08:00")]
        result = calculate_snapshot(payload)
        self.assertEqual(result["capacity_checks"][0]["available_minutes"], 90)
        self.assertEqual(result["capacity_checks"][0]["known_required_minutes"], 60)
        self.assertIn("hard_time_overlap", [item["type"] for item in result["conflicts"]])

    def test_milestone_is_zero_workload_but_past_deadline_is_visible(self):
        payload = snapshot()
        task = payload["tasks"][0]
        task.update(kind="milestone", remaining_minutes=0, deadline_at="2026-10-26T23:40:00+08:00")
        task["confirmed_fields"] = sorted(critical_fields(task))
        result = calculate_snapshot(payload)
        self.assertEqual(result["capacity_checks"], [])
        self.assertIn("deadline_passed", [item["type"] for item in result["conflicts"]])

    def test_mixed_fixed_and_flexible_data_is_rejected(self):
        payload = snapshot()
        payload["tasks"][0]["fixed_start_at"] = "2026-10-26T23:50:00+08:00"
        with self.assertRaisesRegex(ValueError, "非fixed"):
            validate_snapshot(payload)

    def test_extra_fixed_deadline_is_not_silently_ignored(self):
        payload = snapshot()
        task = fixed(payload, "lecture", "2026-10-27T10:00:00+08:00", "2026-10-27T11:00:00+08:00")
        task["deadline_at"] = "2026-10-27T09:00:00+08:00"
        payload["tasks"] = [task]
        result = calculate_snapshot(payload)
        self.assertEqual(result["status"], "information_needed")
        self.assertTrue(any("deadline_at" in item["fields"] for item in result["missing_information"]))

    def test_unknown_confirmation_field_is_rejected(self):
        payload = snapshot()
        payload["tasks"][0]["confirmed_fields"].append("made_up")
        with self.assertRaisesRegex(ValueError, "含未知字段"):
            validate_snapshot(payload)


class QuietHandler(CampusAgentHandler):
    def log_message(self, *args):
        pass


class SnapshotHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path, payload=None, *, raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            body = raw if raw is not None else None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            connection.request("GET" if body is None else "POST", path,
                               body=body, headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_health_advertises_snapshot_capability(self):
        status, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("snapshot_v2", body["capabilities"])

    def test_valid_snapshot_returns_200_and_same_revision(self):
        payload = snapshot()
        payload["revision"] = 88
        status, body = self.request("/api/v2/calculate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["revision"], 88)
        self.assertEqual(body["status"], "conflicts_detected")

    def test_semantic_error_is_a_json_400_not_server_crash(self):
        payload = snapshot()
        payload["tasks"][0]["source_id"] = "missing"
        status, body = self.request("/api/v2/calculate", payload)
        self.assertEqual(status, 400)
        self.assertIn("source_id不存在", body["error"])
        self.assertEqual(self.request("/api/health")[0], 200)

    def test_unknown_top_level_payload_is_rejected(self):
        status, body = self.request("/api/v2/calculate", {"tasks": []})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_empty_title_draft_does_not_hide_confirmed_capacity_shortage(self):
        payload = snapshot()
        draft = flexible(payload, "editing-draft", None)
        draft.update(title="", status="draft", confirmed_fields=[])
        payload["tasks"].append(draft)
        status, body = self.request("/api/v2/calculate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "conflicts_detected")
        self.assertEqual(body["excluded_task_ids"], ["editing-draft"])
        self.assertEqual(body["capacity_checks"][0]["available_minutes"], 9)
        self.assertEqual(body["capacity_checks"][0]["known_required_minutes"], 180)
        self.assertTrue(any(item["type"] == "capacity_shortage" for item in body["conflicts"]))
        self.assertTrue(any(item["task_id"] == "editing-draft" and "title" in item["fields"] for item in body["missing_information"]))

    def test_unknown_empty_availability_is_not_zero_capacity(self):
        payload = snapshot()
        payload["tasks"][0]["remaining_minutes"] = 5
        payload["availability"] = []
        for flag in (None, False):
            with self.subTest(flag=flag):
                if flag is not None:
                    payload["availability_confirmed"] = flag
                status, body = self.request("/api/v2/calculate", payload)
                self.assertEqual(status, 200)
                self.assertEqual(body["status"], "information_needed")
                self.assertEqual(body["capacity_checks"][0]["available_minutes"], 9)
                self.assertEqual(body["capacity_checks"][0]["basis"], "physical_upper_bound")
                self.assertTrue(any("availability_confirmed" in item["fields"] for item in body["missing_information"]))

    def test_explicit_zero_availability_is_a_confirmed_zero_bound(self):
        payload = snapshot()
        payload.update(availability=[], availability_confirmed=True)
        payload["tasks"][0]["remaining_minutes"] = 5
        status, body = self.request("/api/v2/calculate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "conflicts_detected")
        self.assertEqual(body["capacity_checks"][0]["available_minutes"], 0)
        self.assertEqual(body["capacity_checks"][0]["basis"], "provided_availability")
        self.assertEqual(body["missing_information"], [])

    def test_unconfirmed_nonempty_availability_does_not_supply_capacity(self):
        payload = snapshot()
        payload["availability_confirmed"] = False
        payload["tasks"][0]["remaining_minutes"] = 5
        payload["availability"][0]["end"] = "2026-10-26T23:51:00+08:00"
        status, body = self.request("/api/v2/calculate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "information_needed")
        self.assertEqual(body["capacity_checks"][0]["available_minutes"], 9)
        self.assertEqual(body["capacity_checks"][0]["basis"], "physical_upper_bound")
        payload["availability_confirmed"] = True
        self.assertEqual(self.request("/api/v2/calculate", payload)[1]["capacity_checks"][0]["available_minutes"], 1)

    def test_unknown_availability_still_exposes_proven_physical_shortage(self):
        payload = snapshot()
        payload["availability"] = []
        status, body = self.request("/api/v2/calculate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "conflicts_detected")
        self.assertEqual(body["capacity_checks"][0]["available_minutes"], 9)
        self.assertEqual(body["capacity_checks"][0]["basis"], "physical_upper_bound")
        self.assertTrue(body["missing_information"])

    def test_v2_accepts_unicode_snapshot_larger_than_v1_budget(self):
        payload = snapshot()
        for index in range(4):
            payload["sources"].append({"id": f"long-notice-{index}", "kind": "synthetic", "text": "数" * 100_000, "published_at": None})
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(raw), MAX_BODY_BYTES)
        status, body = self.request("/api/v2/calculate", raw=raw)
        self.assertEqual(status, 200)
        self.assertEqual(body["revision"], payload["revision"])

    def test_v2_budget_accepts_exact_limit_and_rejects_larger_body_with_413(self):
        raw = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
        raw += b" " * (MAX_SNAPSHOT_BODY_BYTES - len(raw))
        self.assertEqual(len(raw), 15_000_000)
        self.assertEqual(self.request("/api/v2/calculate", raw=raw)[0], 200)
        status, body = self.request("/api/v2/calculate", raw=b"", headers={"Content-Length": str(MAX_SNAPSHOT_BODY_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertIn("请导出并拆分清单", body["error"])

    def test_v1_retains_its_one_mib_limit(self):
        status, body = self.request("/api/parse", raw=b"", headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertIn("1MiB", body["error"])


if __name__ == "__main__":
    unittest.main()
