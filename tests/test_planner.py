from copy import deepcopy
from datetime import timedelta
from itertools import combinations
import http.client
from http.server import ThreadingHTTPServer
import json
import random
import threading
import unittest
from unittest.mock import patch

from campus_agent.planner import plan_snapshot
from campus_agent.schedule_validator import validate_schedule
from campus_agent.server import CampusAgentHandler, MAX_SNAPSHOT_BODY_BYTES
from campus_agent.snapshot import _instant
from tests.planning_fixtures import at, fixed, milestone, snapshot, task


class PlannerTests(unittest.TestCase):
    def feasible(self, payload):
        before = deepcopy(payload)
        result = plan_snapshot(payload)
        self.assertEqual(result['status'], 'feasible_found', result)
        self.assertTrue(result['validation']['passed'])
        self.assertTrue(validate_schedule(payload, result['schedule'])['passed'])
        self.assertEqual(payload, before)
        self.assertEqual(result['revision'], payload['revision'])
        self.assertNotIn('tasks', result)
        return result

    def test_basic_full_plan_is_read_only_and_exact(self):
        result = self.feasible(snapshot())
        self.assertEqual(result['schedule'], [{'task_id': 'work', 'kind': 'work', 'start': at('09:00'), 'end': at('10:00')}])

    def test_nine_minutes_is_a_proven_capacity_shortage(self):
        payload = snapshot([task(minutes=180)], [{'start': at('11:51'), 'end': at('12:00')}], reference=at('11:51'))
        result = plan_snapshot(payload)
        self.assertEqual(result['status'], 'conflicts_detected')
        self.assertEqual(result['schedule'], [])

    def test_fixed_occupancy_and_preparation_are_not_double_counted(self):
        result = self.feasible(snapshot([task(minutes=120), fixed()]))
        work = [entry for entry in result['schedule'] if entry['kind'] == 'work']
        self.assertEqual([(entry['start'], entry['end']) for entry in work], [(at('09:00'), at('10:00')), (at('11:00'), at('12:00'))])

    def test_adjacent_fixed_events_do_not_conflict(self):
        self.feasible(snapshot([fixed('a', at('09:00'), at('10:00')), fixed('b', at('10:00'), at('11:00'))]))

    def test_overlapping_fixed_events_have_no_successful_plan(self):
        result = plan_snapshot(snapshot([fixed('a'), fixed('b', at('10:30'), at('11:30'))]))
        self.assertEqual(result['status'], 'conflicts_detected')
        self.assertFalse(result['schedule'])

    def test_fixed_event_does_not_have_to_lie_in_work_availability(self):
        self.feasible(snapshot([task(minutes=30), fixed()], [{'start': at('09:00'), 'end': at('09:30')}]))

    def test_ongoing_fixed_occupancy_only_shows_its_remaining_portion(self):
        result = self.feasible(snapshot([fixed(start=at('08:30'), end=at('09:30')), task(minutes=30)]))
        event = next(entry for entry in result['schedule'] if entry['kind'] == 'fixed')
        self.assertEqual(event['start'], at('09:00'))
        self.assertEqual(event['end'], at('09:30'))

    def test_past_fixed_event_requires_status_review(self):
        result = plan_snapshot(snapshot([fixed(start=at('08:00'), end=at('08:30'))]))
        self.assertEqual(result['status'], 'information_needed')
        self.assertIn('past_fixed_status', {issue['code'] for issue in result['issues']})

    def test_unknown_availability_differs_from_explicit_zero(self):
        payload = snapshot(availability=[]); payload['availability_confirmed'] = False
        self.assertEqual(plan_snapshot(payload)['status'], 'information_needed')
        payload['availability_confirmed'] = True
        self.assertEqual(plan_snapshot(payload)['status'], 'conflicts_detected')

    def test_draft_and_unconfirmed_optional_constraints_are_not_bypassed(self):
        payload = snapshot(); payload['tasks'][0]['status'] = 'draft'
        self.assertEqual(plan_snapshot(payload)['status'], 'information_needed')
        payload = snapshot([task(earliest_start_at=at('10:00'))])
        payload['tasks'][0]['confirmed_fields'].remove('earliest_start_at')
        self.assertEqual(plan_snapshot(payload)['status'], 'information_needed')

    def test_completed_and_cancelled_are_not_rescheduled(self):
        done = task('done', 0, status='completed')
        cancelled = task('cancelled', status='cancelled')
        result = self.feasible(snapshot([done, cancelled, task('active')]))
        self.assertEqual({entry['task_id'] for entry in result['schedule']}, {'active'})
        self.assertEqual(plan_snapshot(snapshot([done, cancelled]))['status'], 'nothing_to_schedule')

    def test_cancelled_dependency_is_not_treated_as_done(self):
        result = plan_snapshot(snapshot([task('cancelled', status='cancelled'), task(depends_on=['cancelled'])]))
        self.assertEqual(result['status'], 'information_needed')
        self.assertIn('cancelled_dependency', {issue['code'] for issue in result['issues']})

    def test_three_hours_cannot_be_split_into_half_hour_gaps_if_indivisible(self):
        windows = [{'start': at(f'{hour:02d}:00'), 'end': at(f'{hour:02d}:30')} for hour in range(9, 15)]
        result = plan_snapshot(snapshot([task(minutes=180, deadline=at('15:00'), splittable=False, min_block_minutes=None)], windows))
        self.assertEqual(result['status'], 'conflicts_detected')
        self.assertTrue(any(conflict['type'] == 'individual_window_shortage' for conflict in result['conflicts']))

    def test_split_tail_is_adjusted_so_every_chunk_meets_the_minimum(self):
        windows = [{'start': at('09:00'), 'end': at('09:50')}, {'start': at('10:00'), 'end': at('10:50')}]
        result = self.feasible(snapshot([task(minutes=65, min_block_minutes=30)], windows))
        durations = [(_instant(row['end'], 'end') - _instant(row['start'], 'start')).total_seconds() / 60 for row in result['schedule']]
        self.assertEqual(durations, [35, 30])

    def test_alternate_sorting_can_recover_from_a_deadline_first_failure(self):
        windows = [{'start': at('09:00'), 'end': at('10:00')}, {'start': at('10:30'), 'end': at('11:00')}, {'start': at('11:30'), 'end': at('12:00')}]
        jobs = [task('early', 30, deadline=at('11:00')), task('continuous', 60, splittable=False, min_block_minutes=None)]
        result = self.feasible(snapshot(jobs, windows))
        self.assertGreater(result['algorithm']['attempts'], 1)
        self.assertEqual(result['algorithm']['strategy'], 'long_first')

    def test_unplaced_minimum_blocks_are_not_called_mathematically_impossible(self):
        windows = [{'start': at('09:00'), 'end': at('09:30')}, {'start': at('10:00'), 'end': at('10:30')}]
        result = plan_snapshot(snapshot([task(minutes=40, min_block_minutes=30)], windows))
        self.assertEqual(result['status'], 'not_found')
        self.assertEqual(result['algorithm']['attempts'], 4)
        self.assertFalse(result['schedule'])

    def test_dependency_and_release_are_observed(self):
        result = self.feasible(snapshot([task('before', 30), task('after', 30, earliest_start_at=at('10:00'), depends_on=['before'])]))
        after = next(row for row in result['schedule'] if row['task_id'] == 'after')
        self.assertGreaterEqual(after['start'], at('10:00'))

    def test_preparation_is_pulled_before_a_fixed_successor(self):
        result = self.feasible(snapshot([task('prep', 60), fixed(depends_on=['prep'])]))
        prep = next(row for row in result['schedule'] if row['task_id'] == 'prep')
        self.assertLessEqual(prep['end'], at('10:00'))

    def test_milestone_dependencies_are_zero_duration_boundaries(self):
        result = self.feasible(snapshot([task('before', 30), milestone(deadline=at('10:00'), depends_on=['before']), task('after', 30, depends_on=['node'])]))
        node = next(row for row in result['schedule'] if row['task_id'] == 'node')
        self.assertEqual(node['start'], node['end'])
        after = next(row for row in result['schedule'] if row['task_id'] == 'after')
        self.assertGreaterEqual(after['start'], at('10:00'))

    def test_lower_priority_optional_work_is_not_silently_dropped(self):
        result = self.feasible(snapshot([task('important', 30, priority=100), task('optional', 30, priority=0, must_do=False)]))
        self.assertEqual({row['task_id'] for row in result['schedule']}, {'important', 'optional'})

    def test_scope_limits_and_window_tasks_are_explicit(self):
        for payload in [snapshot([task(str(index), 1) for index in range(41)]), snapshot([task(deadline=at('12:00', '2031-05-01'))]), snapshot([task(kind='visit_window', status='draft')])]:
            self.assertEqual(plan_snapshot(payload)['status'], 'unsupported')

    def test_exact_scope_boundary_and_equivalent_timezone_offsets_are_supported(self):
        jobs = [task(str(index), 1, deadline=at('09:00', '2031-04-14')) for index in range(40)]
        self.feasible(snapshot(jobs, [{'start': '2031-03-14T01:00:00Z', 'end': '2031-03-14T02:00:00Z'}]))

    def test_past_milestone_is_not_moved_to_tomorrow(self):
        result = plan_snapshot(snapshot([milestone(deadline=at('08:00'))]))
        self.assertEqual(result['status'], 'conflicts_detected')
        self.assertEqual(result['schedule'], [])

    def test_extreme_workload_does_not_overflow_timedelta(self):
        result = plan_snapshot(snapshot([task(minutes=10_000_000)]))
        self.assertEqual(result['status'], 'conflicts_detected')

    def test_fractional_availability_and_duplicate_windows_are_handled_exactly(self):
        begin, end = '2031-03-14T09:00:00.123456+08:00', '2031-03-14T09:01:00.123456+08:00'
        payload = snapshot([task(minutes=1)], [{'start': begin, 'end': end}, {'start': begin, 'end': end}], reference=begin)
        self.feasible(payload)

    def test_a_failed_independent_validation_never_exposes_the_candidate(self):
        rejected = {'passed': False, 'errors': [{'code': 'test', 'task_id': None, 'reason': 'test'}], 'checked_constraints': []}
        with patch('campus_agent.planner.validate_schedule', return_value=rejected):
            result = plan_snapshot(snapshot())
        self.assertEqual(result['status'], 'validation_failed')
        self.assertFalse(result['schedule'])

    def test_seeded_mixed_workloads_never_publish_invalid_plans(self):
        rng = random.Random(1138)
        found = 0
        for case in range(40):
            jobs = [task(f'{case}-{index}', rng.randint(5, 25), priority=rng.randint(0, 100), splittable=bool(rng.getrandbits(1)), min_block_minutes=None) for index in range(5)]
            for job in jobs:
                if job['splittable']:
                    job['min_block_minutes'] = 1
                    job['confirmed_fields'] = sorted(set(job['confirmed_fields']) | {'min_block_minutes'})
            payload = snapshot(jobs + [fixed()])
            result = plan_snapshot(payload)
            if result['status'] == 'feasible_found':
                found += 1
                self.assertTrue(validate_schedule(payload, result['schedule'])['passed'])
        self.assertGreaterEqual(found, 30)

    def test_small_exhaustive_oracle_never_sees_a_false_proven_conflict(self):
        # Enumerate all integer 15-minute placements independently of placement
        # helpers. An oracle witness proves feasibility; absent witnesses are
        # not used to claim continuous-time impossibility.
        rng = random.Random(2067)
        witnessed = 0
        for case in range(80):
            available = set(range(8)) - set(rng.sample(range(8), rng.randint(0, 2)))
            specs, jobs = [], []
            def clock(slot):
                return at(f'{9 + slot // 4:02d}:{(slot % 4) * 15:02d}')
            for index in range(3):
                release = rng.randint(0, 3)
                deadline = rng.randint(release + 1, 8)
                units = rng.randint(1, 3)
                split = bool(rng.getrandbits(1))
                minimum = rng.randint(1, units) if split else units
                deps = [index - 1] if index and rng.getrandbits(1) else []
                specs.append((release, deadline, units, split, minimum, deps))
                jobs.append(task(str(index), units * 15, deadline=clock(deadline), earliest_start_at=clock(release), splittable=split, min_block_minutes=minimum * 15 if split else None, depends_on=[str(item) for item in deps]))
            choices = []
            for release, deadline, units, split, minimum, _ in specs:
                valid = []
                for occupied in combinations(sorted(slot for slot in available if release <= slot < deadline), units):
                    runs, length = [], 0
                    previous = None
                    for slot in occupied:
                        if previous is None or slot == previous + 1:
                            length += 1
                        else:
                            runs.append(length); length = 1
                        previous = slot
                    runs.append(length)
                    if (split or len(runs) == 1) and min(runs) >= minimum:
                        valid.append(set(occupied))
                choices.append(valid)
            def search(index, used, finishes):
                if index == len(specs):
                    return True
                for occupied in choices[index]:
                    if used & occupied or any(finishes[dep] > min(occupied) for dep in specs[index][-1]):
                        continue
                    if search(index + 1, used | occupied, finishes + [max(occupied) + 1]):
                        return True
                return False
            has_witness = search(0, set(), [])
            windows = [{'start': clock(slot), 'end': clock(slot + 1)} for slot in sorted(available)]
            payload = snapshot(jobs, windows)
            result = plan_snapshot(payload)
            if has_witness:
                witnessed += 1
                self.assertNotIn(result['status'], ['conflicts_detected', 'information_needed', 'unsupported', 'validation_failed'], (case, specs, available, result))
            if result['status'] == 'feasible_found':
                self.assertTrue(validate_schedule(payload, result['schedule'])['passed'])
        self.assertGreaterEqual(witnessed, 10)


class IndependentValidatorTests(unittest.TestCase):
    def test_mutations_of_a_valid_schedule_are_rejected(self):
        payload = snapshot([task('a', 30), task('b', 30), fixed()])
        entries = plan_snapshot(payload)['schedule']
        mutations = []
        mutations.append(entries[1:])
        wrong = deepcopy(entries); wrong[0]['end'] = at('09:29'); mutations.append(wrong)
        wrong = deepcopy(entries); wrong[0]['start'] = at('08:59'); mutations.append(wrong)
        wrong = deepcopy(entries); wrong[0]['task_id'] = 'unknown'; mutations.append(wrong)
        wrong = deepcopy(entries); wrong[-1]['end'] = at('11:01'); mutations.append(wrong)
        wrong = deepcopy(entries); wrong[1]['start'] = wrong[0]['start']; mutations.append(wrong)
        wrong = deepcopy(entries); wrong[0]['extra'] = True; mutations.append(wrong)
        for mutated in mutations:
            with self.subTest(mutated=mutated):
                self.assertFalse(validate_schedule(payload, mutated)['passed'])

    def test_indivisibility_and_dependency_tampering_are_rejected(self):
        payload = snapshot([task('a', 60, splittable=False, min_block_minutes=None)])
        rows = [{'task_id': 'a', 'kind': 'work', 'start': at('09:00'), 'end': at('09:30')}, {'task_id': 'a', 'kind': 'work', 'start': at('10:00'), 'end': at('10:30')}]
        self.assertFalse(validate_schedule(payload, rows)['passed'])
        payload = snapshot([task('a', 30), task('b', 30, depends_on=['a'])])
        rows = [{'task_id': 'b', 'kind': 'work', 'start': at('09:00'), 'end': at('09:30')}, {'task_id': 'a', 'kind': 'work', 'start': at('09:30'), 'end': at('10:00')}]
        self.assertFalse(validate_schedule(payload, rows)['passed'])

    def test_minimum_and_release_constraints_are_checked_even_when_total_work_matches(self):
        payload = snapshot([task('a', 60, min_block_minutes=30)])
        rows = [{'task_id': 'a', 'kind': 'work', 'start': at('09:00'), 'end': at('09:20')}, {'task_id': 'a', 'kind': 'work', 'start': at('09:30'), 'end': at('09:50')}, {'task_id': 'a', 'kind': 'work', 'start': at('10:00'), 'end': at('10:20')}]
        self.assertIn('minimum_block', {error['code'] for error in validate_schedule(payload, rows)['errors']})
        payload = snapshot([task('a', 60, earliest_start_at=at('10:00'))])
        rows = [{'task_id': 'a', 'kind': 'work', 'start': at('09:00'), 'end': at('10:00')}]
        self.assertIn('release_or_deadline', {error['code'] for error in validate_schedule(payload, rows)['errors']})


class QuietHandler(CampusAgentHandler):
    def log_message(self, *args):
        pass


class PlanningHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(5)

    def request(self, payload, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request('POST', '/api/v2/plan', json.dumps(payload).encode(), headers=headers or {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_actual_http_plan_never_calls_the_model(self):
        with patch('campus_agent.server.extract_notice') as model:
            status, result = self.request(snapshot())
            model.assert_not_called()
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'feasible_found')

    def test_invalid_snapshot_and_oversized_body_have_explicit_errors(self):
        self.assertEqual(self.request({'bad': 'input'})[0], 400)
        self.assertEqual(self.request({}, {'Content-Length': str(MAX_SNAPSHOT_BODY_BYTES + 1)})[0], 413)

    def test_plan_accepts_a_valid_snapshot_above_the_legacy_one_mib_limit(self):
        payload = snapshot()
        payload['sources'] = [{'id': str(index), 'kind': 'synthetic', 'text': 'x' * 100000, 'published_at': None} for index in range(11)]
        self.assertGreater(len(json.dumps(payload).encode()), 1048576)
        status, result = self.request(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'feasible_found')


if __name__ == '__main__':
    unittest.main()
