"""Authored synthetic planning inputs, with explicit confirmation."""
from campus_agent.snapshot import critical_fields


def at(clock, day='2031-03-14'):
    return f'{day}T{clock}:00+08:00'


def task(task_id='work', minutes=60, deadline=None, kind='flexible', **changes):
    value = dict(id=task_id, source_id=None, title=task_id, kind=kind, status='confirmed',
                 earliest_start_at=None, deadline_at=deadline if deadline else (at('12:00') if kind != 'fixed' else None),
                 fixed_start_at=None, fixed_end_at=None, visit_windows=[], remaining_minutes=minutes,
                 splittable=True if kind == 'flexible' else None, min_block_minutes=1 if kind == 'flexible' else None,
                 must_do=True, priority=50, location=None, materials=[], depends_on=[],
                 confirmed_fields=[], evidence=[], uncertainties=[])
    value.update(changes)
    value['confirmed_fields'] = sorted(critical_fields(value) | ({'earliest_start_at'} if value['earliest_start_at'] else set()) | ({'depends_on'} if value['depends_on'] else set()))
    return value


def fixed(task_id='class', start=None, end=None, **changes):
    return task(task_id, 0, kind='fixed', fixed_start_at=start or at('10:00'), fixed_end_at=end or at('11:00'), **changes)


def milestone(task_id='node', deadline=None, **changes):
    return task(task_id, 0, deadline=deadline, kind='milestone', **changes)


def snapshot(tasks=None, availability=None, reference=None):
    return dict(schema_version=2, revision=7, timezone='Asia/Shanghai', reference_time=reference or at('09:00'),
                sources=[], tasks=[task()] if tasks is None else tasks,
                availability=[{'start': at('09:00'), 'end': at('12:00')}] if availability is None else availability,
                availability_confirmed=True)
