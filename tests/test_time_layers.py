"""Four time layers on a calendar: routine, free, work, temporary (snapshot v3)."""
from copy import deepcopy
from unittest import TestCase

from campus_agent.layers import calendar_snapshot, validate_snapshot_v3

WEEKDAYS = [{'start': '08:00', 'end': '23:00'}] * 5 + [{'start': '09:00', 'end': '22:00'}] * 2


def snapshot(**changes):
    # 2026-11-09 is a Monday.
    base = {'schema_version': 3, 'revision': 1, 'timezone': 'Asia/Shanghai', 'reference_time': '2026-11-09T08:00:00+08:00',
            'sources': [], 'daily_window': WEEKDAYS, 'routines_confirmed': True, 'routines': [], 'free_adjustments': [],
            'events': [], 'tasks': []}
    base.update(changes)
    return base


def routine(**changes):
    item = {'id': 'r1', 'title': '高等数学', 'weekdays': [1, 3], 'start': '08:00', 'end': '09:40', 'from': '2026-09-01',
            'until': None, 'week_interval': 1, 'skip_dates': [], 'location': None, 'source_id': None}
    item.update(changes)
    return item


def event(**changes):
    item = {'id': 'e1', 'title': '讲座', 'start_at': '2026-11-09T09:00:00+08:00', 'end_at': '2026-11-09T11:00:00+08:00',
            'location': None, 'status': 'active', 'source_id': None, 'evidence': [], 'notes': []}
    item.update(changes)
    return item


def task(**changes):
    item = {'id': 't1', 'title': '课程报告', 'deadline_at': '2026-11-10T23:59:00+08:00', 'estimate_minutes': 120,
            'splittable': True, 'min_block_minutes': None, 'earliest_start_at': None, 'priority': None, 'status': 'active',
            'location': None, 'materials': [], 'source_id': None, 'evidence': [], 'notes': []}
    item.update(changes)
    return item


def week(data, **range_):
    return calendar_snapshot({'snapshot': data, **({'range': range_} if range_ else {})})


def spans(items):
    return [(item['start'][11:16], item['end'][11:16], item['start'][:10]) for item in items]


class ValidationTests(TestCase):
    def test_minimal_snapshot_is_valid(self):
        validate_snapshot_v3(snapshot())

    def test_structural_and_semantic_errors_are_rejected(self):
        bad = [
            snapshot(daily_window=WEEKDAYS[:6]),
            snapshot(daily_window=[{'start': '10:00', 'end': '09:00'}] + WEEKDAYS[1:]),
            snapshot(routines=[routine(weekdays=[0])]),
            snapshot(routines=[routine(week_interval=3)]),
            snapshot(routines=[routine(start='10:00', end='10:00')]),
            snapshot(routines=[routine(), routine()]),
            snapshot(events=[event(end_at='2026-11-09T08:00:00+08:00')]),
            snapshot(events=[event(source_id='missing')]),
            snapshot(tasks=[task(priority=101)]),
            snapshot(tasks=[task(estimate_minutes=0)]),
            snapshot(sources=[{'id': 's1', 'kind': 'pasted_notice', 'text': '原文', 'published_at': None}],
                     tasks=[task(source_id='s1', evidence=[{'field': 'deadline_at', 'origin': 'source_text', 'source_id': 's1', 'quote': '不在原文'}])]),
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    validate_snapshot_v3(data)


class RoutineTests(TestCase):
    def test_weekly_routine_expands_inside_its_term_and_skips_dates(self):
        result = week(snapshot(routines=[routine(skip_dates=['2026-11-11'])]))
        self.assertEqual(spans(result['layers']['routine']), [('08:00', '09:40', '2026-11-09')])
        result = week(snapshot(routines=[routine(until='2026-11-08')]))
        self.assertEqual(result['layers']['routine'], [])

    def test_alternate_weeks_follow_the_first_week(self):
        data = snapshot(routines=[routine(weekdays=[2], **{'from': '2026-11-03'}, week_interval=2)])
        self.assertEqual(week(data)['layers']['routine'], [])
        self.assertEqual(len(week(data, **{'from': '2026-11-16', 'to': '2026-11-22'})['layers']['routine']), 1)

    def test_routines_are_optional_and_conflicts_are_still_judged_without_them(self):
        two = [event(), event(id='e2', title='面试', start_at='2026-11-09T10:00:00+08:00', end_at='2026-11-09T12:00:00+08:00')]
        result = week(snapshot(routines_confirmed=False, events=two))
        self.assertEqual(result['definition']['routines'], 'unconfirmed')
        self.assertFalse(any(item['type'] == 'routines_unconfirmed' for item in result['undefined']))
        self.assertEqual([item['type'] for item in result['conflicts']], ['temporary_overlap'])
        self.assertTrue(result['layers']['free'])


class FreeTimeTests(TestCase):
    def test_free_time_is_the_daily_window_minus_routine_and_temporary(self):
        result = week(snapshot(routines=[routine()], events=[event(start_at='2026-11-09T14:00:00+08:00', end_at='2026-11-09T16:00:00+08:00')]))
        monday = [item for item in spans(result['layers']['free']) if item[2] == '2026-11-09']
        self.assertEqual(monday, [('09:40', '14:00', '2026-11-09'), ('16:00', '23:00', '2026-11-09')])

    def test_manual_adjustments_add_and_remove_free_time(self):
        data = snapshot(free_adjustments=[
            {'id': 'a1', 'kind': 'remove', 'start_at': '2026-11-09T12:00:00+08:00', 'end_at': '2026-11-09T13:00:00+08:00', 'note': '午睡'},
            {'id': 'a2', 'kind': 'add', 'start_at': '2026-11-09T23:00:00+08:00', 'end_at': '2026-11-09T23:30:00+08:00', 'note': None}])
        monday = [item for item in week(data)['layers']['free'] if item['start'].startswith('2026-11-09')]
        self.assertEqual(spans(monday), [('08:00', '12:00', '2026-11-09'), ('13:00', '23:30', '2026-11-09')])

    def test_a_day_set_to_null_has_no_free_time_and_null_window_is_undefined(self):
        result = week(snapshot(daily_window=WEEKDAYS[:6] + [None]))
        self.assertFalse(any(item['start'].startswith('2026-11-15') for item in result['layers']['free']))
        result = week(snapshot(daily_window=None, tasks=[task()]))
        self.assertEqual(result['definition']['free_time'], 'undefined')
        self.assertEqual(result['layers']['free'], [])
        self.assertEqual(result['tasks'][0]['state'], 'free_time_undefined')
        self.assertEqual(result['layers']['work'], [])


class ConflictTests(TestCase):
    def test_temporary_over_routine_is_a_conflict_with_overlap_minutes(self):
        result = week(snapshot(routines=[routine()], events=[event()]))
        conflict = next(item for item in result['conflicts'] if item['type'] == 'temporary_routine_overlap')
        self.assertEqual((conflict['minutes'], conflict['layers']), (40, ['routine', 'temporary']))
        self.assertEqual(result['status'], 'conflicts')

    def test_routine_and_temporary_overlaps_among_themselves(self):
        result = week(snapshot(routines=[routine(), routine(id='r2', title='英语', weekdays=[1], start='09:00', end='10:00')],
                               events=[event(), event(id='e2', title='面试', start_at='2026-11-09T10:30:00+08:00', end_at='2026-11-09T12:00:00+08:00')]))
        types = sorted(item['type'] for item in result['conflicts'])
        self.assertIn('routine_overlap', types)
        self.assertIn('temporary_overlap', types)

    def test_conflicts_of_temporary_items_are_found_outside_the_shown_week(self):
        data = snapshot(routines=[routine(weekdays=[3], start='14:00', end='15:40')],
                        events=[event(start_at='2026-11-18T14:00:00+08:00', end_at='2026-11-18T16:00:00+08:00'),
                                event(id='e2', title='面试', start_at='2026-11-18T15:30:00+08:00', end_at='2026-11-18T17:00:00+08:00')])
        result = week(data)
        self.assertEqual(result['range'], {'from': '2026-11-09', 'to': '2026-11-15'})
        types = sorted(item['type'] for item in result['conflicts'])
        self.assertEqual(types, ['temporary_overlap', 'temporary_routine_overlap', 'temporary_routine_overlap'])
        self.assertEqual(result['layers']['temporary'], [])

    def test_cancelled_events_and_touching_intervals_do_not_conflict(self):
        result = week(snapshot(routines=[routine()], events=[event(status='cancelled'), event(id='e2', start_at='2026-11-09T09:40:00+08:00')]))
        self.assertEqual(result['conflicts'], [])


class WorkTimeTests(TestCase):
    def test_work_is_placed_only_in_free_time_before_the_deadline(self):
        result = week(snapshot(routines=[routine()], events=[event(start_at='2026-11-09T09:40:00+08:00', end_at='2026-11-09T12:00:00+08:00')], tasks=[task()]))
        self.assertEqual(result['tasks'][0]['state'], 'scheduled')
        self.assertEqual(result['tasks'][0]['scheduled_minutes'], 120)
        self.assertTrue(result['validation']['passed'])
        for block in result['layers']['work']:
            self.assertGreaterEqual(block['start'], '2026-11-09T12:00:00+08:00')
        self.assertTrue(any('优先级' in note for note in result['tasks'][0]['assumptions']))
        self.assertEqual(result['status'], 'clear')

    def test_work_that_does_not_fit_is_reported_while_others_stay_scheduled(self):
        tight = snapshot(reference_time='2026-11-09T21:00:00+08:00', tasks=[
            task(id='t1', deadline_at='2026-11-09T22:00:00+08:00', estimate_minutes=60),
            task(id='t2', title='小测准备', deadline_at='2026-11-09T22:30:00+08:00', estimate_minutes=60)])
        result = week(tight)
        states = {item['id']: item for item in result['tasks']}
        self.assertEqual(states['t1']['state'], 'scheduled')
        self.assertEqual(states['t2']['state'], 'short')
        shortage = next(item for item in result['conflicts'] if item['type'] == 'work_shortage')
        self.assertEqual(shortage['minutes'], 30)
        self.assertEqual(result['status'], 'conflicts')

    def test_continuous_work_needs_one_gap(self):
        data = snapshot(reference_time='2026-11-09T08:00:00+08:00', daily_window=[{'start': '08:00', 'end': '10:00'}] * 7,
                        events=[event(start_at='2026-11-09T09:00:00+08:00', end_at='2026-11-09T09:10:00+08:00')],
                        tasks=[task(deadline_at='2026-11-09T10:00:00+08:00', estimate_minutes=90, splittable=False)])
        self.assertEqual(week(data)['tasks'][0]['state'], 'short')

    def test_undefined_estimate_deadline_overdue_and_far_deadlines(self):
        result = week(snapshot(tasks=[task(id='a', estimate_minutes=None), task(id='b', deadline_at=None),
                                      task(id='c', deadline_at='2026-11-08T12:00:00+08:00'),
                                      task(id='d', deadline_at='2027-01-30T12:00:00+08:00'),
                                      task(id='e', status='completed')]))
        states = {item['id']: item['state'] for item in result['tasks']}
        self.assertEqual(states, {'a': 'estimate_undefined', 'b': 'deadline_undefined', 'c': 'overdue', 'd': 'beyond_horizon', 'e': 'completed'})
        self.assertTrue(any(item['type'] == 'overdue' for item in result['conflicts']))
        undefined = {item['type'] for item in result['undefined']}
        self.assertTrue({'estimate_undefined', 'deadline_undefined', 'beyond_horizon'} <= undefined)
        self.assertTrue(any(marker['id'] == 'a' and marker['type'] == 'deadline' for marker in result['markers']))

    def test_work_never_starts_before_the_reference_time(self):
        result = week(snapshot(reference_time='2026-11-09T20:00:00+08:00', tasks=[task(estimate_minutes=30)]))
        self.assertGreaterEqual(result['layers']['work'][0]['start'], '2026-11-09T20:00:00+08:00')


class EventDefinitionTests(TestCase):
    def test_events_without_end_or_time_are_markers_or_pending(self):
        result = week(snapshot(events=[event(end_at=None), event(id='e2', start_at=None, end_at=None)]))
        self.assertEqual(result['layers']['temporary'], [])
        self.assertTrue(any(marker['type'] == 'event_start_only' for marker in result['markers']))
        undefined = {item['type'] for item in result['undefined']}
        self.assertEqual(undefined, {'event_end_undefined', 'event_time_pending'})
        self.assertEqual(result['status'], 'incomplete')


class RangeTests(TestCase):
    def test_default_range_is_the_reference_week_and_long_ranges_are_rejected(self):
        result = week(snapshot(reference_time='2026-11-11T08:00:00+08:00'))
        self.assertEqual(result['range'], {'from': '2026-11-09', 'to': '2026-11-15'})
        with self.assertRaises(ValueError):
            week(snapshot(), **{'from': '2026-11-01', 'to': '2026-12-15'})

    def test_work_planned_outside_the_range_is_still_counted(self):
        data = snapshot(tasks=[task(deadline_at='2026-11-20T12:00:00+08:00', estimate_minutes=30)])
        result = week(data, **{'from': '2026-11-16', 'to': '2026-11-22'})
        self.assertEqual(result['tasks'][0]['state'], 'scheduled')
        self.assertEqual(result['layers']['work'], [])

    def test_input_is_not_mutated(self):
        data = snapshot(routines=[routine()], events=[event()], tasks=[task()])
        before = deepcopy(data)
        week(data)
        self.assertEqual(data, before)


class AppointmentTests(TestCase):
    """Appointments the user makes (an outing with friends) are 落笔, not 点墨, but still occupy time."""

    def test_appointments_have_their_own_layer_and_block_work(self):
        data = snapshot(reference_time='2026-11-14T09:00:00+08:00',
                        events=[event(id='a1', title='和朋友去公园', kind='appointment', start_at='2026-11-14T09:00:00+08:00', end_at='2026-11-14T21:00:00+08:00')],
                        tasks=[task(deadline_at='2026-11-14T22:00:00+08:00', estimate_minutes=120)])
        result = week(data)
        self.assertEqual([item['id'] for item in result['layers']['appointment']], ['a1'])
        self.assertEqual(result['layers']['temporary'], [])
        self.assertEqual(result['tasks'][0]['state'], 'short', 'the outing takes the free time the work needed')

    def test_an_appointment_over_a_routine_or_a_temporary_item_is_a_conflict(self):
        data = snapshot(routines=[routine()], events=[
            event(id='a1', title='约饭', kind='appointment', start_at='2026-11-09T09:00:00+08:00', end_at='2026-11-09T10:00:00+08:00'),
            event(id='e2', title='讲座', start_at='2026-11-09T09:30:00+08:00', end_at='2026-11-09T11:00:00+08:00')])
        conflicts = {item['type']: item for item in week(data)['conflicts']}
        self.assertEqual(conflicts['appointment_routine_overlap']['layers'], ['routine', 'appointment'])
        self.assertEqual(conflicts['appointment_overlap']['minutes'], 30)
        self.assertEqual([item['layer'] for item in conflicts['appointment_overlap']['items']], ['appointment', 'temporary'])
        self.assertIn('temporary_routine_overlap', conflicts)

    def test_events_without_kind_are_temporary_and_bad_kinds_are_rejected(self):
        self.assertEqual(len(week(snapshot(events=[event()]))['layers']['temporary']), 1)
        with self.assertRaises(ValueError):
            validate_snapshot_v3(snapshot(events=[event(kind='party')]))


class CalendarRecoveryTests(TestCase):
    def test_legacy_invalid_times_remain_visible_but_do_not_prevent_other_work(self):
        midnight = "2026-11-09T00:00:00+08:00"
        data = snapshot(
            tasks=[task(id="legacy", earliest_start_at=midnight, deadline_at=midnight),
                   task(id="ok", estimate_minutes=30)],
            events=[event(id="bad-event", start_at=None)],
        )
        with self.assertRaises(ValueError):
            validate_snapshot_v3(data)
        before = deepcopy(data)
        result = week(data)
        self.assertEqual(data, before)
        self.assertEqual({item["type"] for item in result["undefined"]}, {"event_time_invalid", "task_time_invalid"})
        self.assertEqual([(item["id"], item["state"]) for item in result["tasks"]],
                         [("legacy", "time_invalid"), ("ok", "scheduled")])
        self.assertEqual([item["task_id"] for item in result["layers"]["work"]], ["ok"])
        self.assertFalse(result["layers"]["temporary"])
        self.assertTrue(result["validation"]["passed"])
