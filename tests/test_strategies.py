"""叠墨 strategies: every conflict gets rule-based options, and applying one removes the conflict."""
from unittest import TestCase

from campus_agent.layers import calendar_snapshot
from tests.test_time_layers import event, routine, snapshot, task, week


def apply(data, strategy):
    params = strategy['params']
    data = {**data, 'events': [dict(item) for item in data['events']], 'tasks': [dict(item) for item in data['tasks']],
            'routines': [dict(item) for item in data['routines']], 'free_adjustments': list(data['free_adjustments'])}
    if strategy['action'] == 'skip_routine_date':
        next(item for item in data['routines'] if item['id'] == params['routine_id'])['skip_dates'].append(params['date'])
    elif strategy['action'] == 'cancel_event':
        next(item for item in data['events'] if item['id'] == params['event_id'])['status'] = 'cancelled'
    elif strategy['action'] == 'move_event':
        item = next(item for item in data['events'] if item['id'] == params['event_id'])
        item['start_at'], item['end_at'] = params['start_at'], params['end_at']
    elif strategy['action'] == 'update_task':
        next(item for item in data['tasks'] if item['id'] == params['task_id']).update(params['patch'])
    elif strategy['action'] == 'add_free':
        data['free_adjustments'].append({'id': 'a1', 'kind': 'add', 'start_at': params['start_at'], 'end_at': params['end_at'], 'note': params['note']})
    return data


class StrategyTests(TestCase):
    def test_every_conflict_has_an_id_advice_and_a_recommended_option(self):
        data = snapshot(routines=[routine()], events=[event()], tasks=[task(deadline_at='2026-11-08T12:00:00+08:00')])
        result = week(data)
        self.assertEqual([conflict['type'] for conflict in result['conflicts']], ['overdue', 'temporary_routine_overlap'])
        for conflict in result['conflicts']:
            self.assertTrue(conflict['id'] and conflict['advice'])
            self.assertEqual(sum(item['recommended'] for item in conflict['strategies']), 1)

    def test_skipping_the_class_once_clears_a_temporary_routine_overlap(self):
        data = snapshot(routines=[routine()], events=[event()])
        conflict = week(data)['conflicts'][0]
        best = next(item for item in conflict['strategies'] if item['recommended'])
        self.assertEqual((best['action'], best['params']), ('skip_routine_date', {'routine_id': 'r1', 'date': '2026-11-09'}))
        self.assertEqual(week(apply(data, best))['conflicts'], [])
        other = next(item for item in conflict['strategies'] if item['action'] == 'cancel_event')
        self.assertEqual(week(apply(data, other))['conflicts'], [])

    def test_an_appointment_is_moved_into_open_free_time_near_the_original(self):
        data = snapshot(routines=[routine()], events=[event(kind='appointment', title='和朋友吃饭')])
        conflict = week(data)['conflicts'][0]
        moves = [item for item in conflict['strategies'] if item['action'] == 'move_event']
        self.assertEqual(len(moves), 2)
        self.assertTrue(moves[0]['recommended'])
        self.assertEqual(moves[0]['params']['start_at'], '2026-11-09T09:40:00+08:00')
        for move in moves:
            self.assertEqual(week(apply(data, move))['conflicts'], [])

    def test_two_temporary_items_offer_keeping_either(self):
        data = snapshot(events=[event(), event(id='e2', title='面试', start_at='2026-11-09T10:00:00+08:00', end_at='2026-11-09T12:00:00+08:00')])
        conflict = week(data)['conflicts'][0]
        self.assertEqual(sorted(item['params']['event_id'] for item in conflict['strategies']), ['e1', 'e2'])
        for strategy in conflict['strategies']:
            self.assertEqual(week(apply(data, strategy))['conflicts'], [])

    def test_routine_overlap_recommends_checking_the_timetable(self):
        data = snapshot(routines=[routine(), routine(id='r2', title='英语', start='09:00', end='10:00')])
        conflict = week(data)['conflicts'][0]
        self.assertEqual(conflict['strategies'][0]['params'], {'page': 'time'})
        skip = next(item for item in conflict['strategies'] if item['action'] == 'skip_routine_date')
        self.assertEqual(len(week(apply(data, skip))['conflicts']), 1, 'only that date is skipped; Wednesday still overlaps')

    def test_work_shortage_offers_extra_free_time_that_fits(self):
        data = snapshot(daily_window=[{'start': '20:00', 'end': '21:00'}] * 7, tasks=[task(estimate_minutes=240, deadline_at='2026-11-10T23:59:00+08:00')])
        conflict = week(data)['conflicts'][0]
        self.assertEqual(conflict['type'], 'work_shortage')
        add = next(item for item in conflict['strategies'] if item['action'] == 'add_free')
        self.assertTrue(add['recommended'])
        self.assertEqual(week(apply(data, add))['conflicts'], [])

    def test_unsplittable_work_is_offered_splitting_first(self):
        data = snapshot(daily_window=[{'start': '20:00', 'end': '21:00'}] * 7, tasks=[task(estimate_minutes=90, splittable=False)])
        conflict = week(data)['conflicts'][0]
        self.assertEqual((conflict['strategies'][0]['params']['patch'], conflict['strategies'][0]['recommended']), ({'splittable': True}, True))
        self.assertEqual(week(apply(data, conflict['strategies'][0]))['conflicts'], [])

    def test_overdue_work_recommends_marking_it_done(self):
        data = snapshot(tasks=[task(deadline_at='2026-11-08T12:00:00+08:00')])
        best = week(data)['conflicts'][0]['strategies'][0]
        self.assertEqual(best['params']['patch'], {'status': 'completed'})
        self.assertEqual(week(apply(data, best))['conflicts'], [])
