"""Contract boundary checks; these do not validate task feasibility."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts/task-snapshot.v2.schema.json").read_text())
EXAMPLE = json.loads((ROOT / "examples/nine-minutes.snapshot.json").read_text())


class TaskContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Draft202012Validator.check_schema(SCHEMA)
        cls.validator = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
        if "date-time" not in cls.validator.format_checker.checkers:
            raise RuntimeError("Install requirements-dev.txt: date-time format checking must not be silently skipped.")

    def assert_rejected(self, payload):
        self.assertFalse(self.validator.is_valid(payload))

    def test_known_infeasible_example_is_structurally_valid(self):
        # A schema-valid payload can still be impossible to schedule.
        self.validator.validate(EXAMPLE)

    def test_unknown_duration_is_preserved_as_unknown(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["tasks"][0].update(status="draft", remaining_minutes=None, confirmed_fields=[])
        self.validator.validate(payload)

    def test_negative_remaining_work_is_rejected(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["tasks"][0]["remaining_minutes"] = -1
        self.assert_rejected(payload)

    def test_string_false_is_not_a_boolean_choice(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["tasks"][0]["must_do"] = "false"
        self.assert_rejected(payload)

    def test_timezone_is_required_for_reference_time(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["reference_time"] = "2026-10-26T23:50:00"
        self.assert_rejected(payload)

    def test_nonexistent_date_is_rejected(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["tasks"][0]["deadline_at"] = "2026-02-30T23:59:00+08:00"
        self.assert_rejected(payload)

    def test_completed_task_has_no_remaining_work(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["tasks"][0]["status"] = "completed"
        self.assert_rejected(payload)

    def test_confirmed_fixed_event_requires_end(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["tasks"][0].update(kind="fixed", remaining_minutes=0, fixed_start_at="2026-10-26T23:50:00+08:00", fixed_end_at=None)
        self.assert_rejected(payload)

    def test_source_evidence_cannot_be_empty(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["tasks"][0]["evidence"][0]["quote"] = ""
        self.assert_rejected(payload)

    def test_unrecognized_top_level_data_is_rejected(self):
        payload = copy.deepcopy(EXAMPLE)
        payload["make_it_feasible"] = True
        self.assert_rejected(payload)


if __name__ == "__main__":
    unittest.main()
