"""Bounded deterministic heuristics; only independently valid full plans ship."""
from datetime import timedelta

from .conflict import free_intervals
from .planning_contract import MAX_SCHEDULE_BLOCKS, MINUTE_US, duration_us, planning_inputs
from .schedule_validator import validate_schedule
from .snapshot import _instant, calculate_snapshot, validate_snapshot

STRATEGIES = ('deadline', 'tight_window', 'long_first', 'priority')
LIMITATIONS = [
    '本版最多40个未完成事项、最长31天；尝试四种确定性排序，不保证最优，也不将未找到安排当作数学无解。',
    '本次仅按用户已确认的信息校验；未自动加入路程、转场、休息或临时任务，请在空闲和固定占用中自行预留。',
    '所有仍在清单中的可调整事项也纳入安排，不自动放弃低优先级事项；安排不改变任务状态或替用户报名。',
]


def _entry(task, start, end):
    return {'task_id': task['id'], 'kind': {'flexible': 'work', 'fixed': 'fixed', 'milestone': 'milestone'}[task['kind']],
            'start': start.isoformat(), 'end': end.isoformat()}


def _allocate(slots, release, deadline, required, minimum, splittable):
    placed = []
    left = required
    for begin, finish in slots:
        start, end = max(begin, release), min(finish, deadline)
        capacity = max(0, duration_us(start, end))
        if not splittable:
            if capacity >= required:
                placed = [(start, start + timedelta(microseconds=required))]
                left = 0
                break
            continue
        if capacity < minimum:
            continue
        amount = min(capacity, left)
        if 0 < left - amount < minimum:
            amount = left - minimum
        if amount < minimum:
            continue
        placed.append((start, start + timedelta(microseconds=amount)))
        left -= amount
        if left == 0:
            break
    if left:
        return None
    remaining = []
    for begin, end in slots:
        cursor = begin
        for start, finish in placed:
            if finish <= cursor or start >= end:
                continue
            if start > cursor:
                remaining.append((cursor, start))
            cursor = max(cursor, finish)
        if cursor < end:
            remaining.append((cursor, end))
    return placed, remaining


def plan_snapshot(snapshot):
    validate_snapshot(snapshot)
    active, reference, horizon, issues = planning_inputs(snapshot)
    base = calculate_snapshot(snapshot)
    response = {'schema_version': 1, 'revision': snapshot['revision'], 'status': 'information_needed',
                'horizon': {'start': reference.isoformat(), 'end': horizon.isoformat()}, 'schedule': [],
                'validation': {'passed': None, 'errors': [], 'checked_constraints': []},
                'issues': issues, 'conflicts': base['conflicts'], 'capacity_checks': base['capacity_checks'],
                'warnings': [], 'limitations': LIMITATIONS,
                'algorithm': {'name': 'bounded_greedy_v1', 'attempts': 0, 'max_attempts': len(STRATEGIES)}}
    if not active:
        response.update(status='nothing_to_schedule', validation=validate_schedule(snapshot, []))
        return response
    if issues:
        response['status'] = 'unsupported' if any(issue['unsupported'] for issue in issues) else 'information_needed'
        return response
    if base['conflicts']:
        response['status'] = 'conflicts_detected'
        return response
    tasks = {task['id']: task for task in active}
    completed = {task['id'] for task in snapshot['tasks'] if task['status'] == 'completed'}
    fixed = [(_instant(task['fixed_start_at'], 'fixed_start'), _instant(task['fixed_end_at'], 'fixed_end')) for task in active if task['kind'] == 'fixed']
    available = [(_instant(item['start'], 'availability'), _instant(item['end'], 'availability')) for item in snapshot['availability']]
    initial_slots = free_intervals(reference, horizon, available, fixed)
    latest = {task['id']: _instant(task['fixed_end_at'] if task['kind'] == 'fixed' else task['deadline_at'], 'end') for task in active}
    topo = []
    seen = set(completed)
    pending = set(tasks)
    while pending:
        ready = sorted(task_id for task_id in pending if all(dependency in seen for dependency in tasks[task_id]['depends_on']))
        if not ready:
            response['issues'].append({'task_id': None, 'code': 'dependency_unavailable', 'fields': ['depends_on'], 'reason': '前置关系尚不能完成排序。', 'unsupported': False})
            return response
        topo.extend(ready); seen.update(ready); pending.difference_update(ready)
    # Before subtracting duration, reject work that cannot fit even alone. This
    # also bounds timedelta arithmetic for arbitrarily large requested minutes.
    for task in active:
        if task['kind'] != 'flexible':
            continue
        release = max(reference, _instant(task['earliest_start_at'], 'earliest') if task['earliest_start_at'] else reference)
        eligible = [(max(start, release), min(end, latest[task['id']])) for start, end in initial_slots if start < latest[task['id']] and end > release]
        required = task['remaining_minutes'] * MINUTE_US
        total = sum(max(0, duration_us(start, end)) for start, end in eligible)
        longest = max((max(0, duration_us(start, end)) for start, end in eligible), default=0)
        if required > total or (task['splittable'] is False and required > longest):
            response['status'] = 'conflicts_detected'
            response['conflicts'].append({'type': 'individual_window_shortage', 'task_ids': [task['id']], 'tasks': [task['title']],
                                          'reason': '该工作即使单独安排，最早开始至截止之间的可用时长或最长连续空档也不足。'})
    if response['conflicts']:
        return response
    for task_id in reversed(topo):
        task = tasks[task_id]
        if task['kind'] == 'fixed':
            before = _instant(task['fixed_start_at'], 'fixed_start')
        elif task['kind'] == 'milestone':
            before = latest[task_id]
        else:
            if duration_us(reference, latest[task_id]) < task['remaining_minutes'] * MINUTE_US:
                response['status'] = 'conflicts_detected'
                response['conflicts'].append({'type': 'dependency_window_shortage', 'task_ids': [task_id], 'tasks': [task['title']],
                                              'reason': '前置关系要求该工作更早完成，即使没有其他占用也没有足够时间。'})
                return response
            before = latest[task_id] - timedelta(minutes=task['remaining_minutes'])
        for dependency in task['depends_on']:
            if dependency in latest:
                latest[dependency] = min(latest[dependency], before)
    failures = []
    for strategy in STRATEGIES:
        response['algorithm']['attempts'] += 1
        slots = list(initial_slots)
        end_times = {task_id: reference for task_id in completed}
        pending = set(tasks)
        entries = []
        failure = None

        def sort_key(task_id):
            task = tasks[task_id]
            duration = task['remaining_minutes'] or 0
            end = latest[task_id]
            importance = (not task['must_do'], -task['priority'])
            if strategy == 'long_first':
                return (-duration, end, *importance, task_id)
            if strategy == 'priority':
                return (*importance, end, -duration, task_id)
            if strategy == 'tight_window':
                release = _instant(task['earliest_start_at'], 'earliest') if task['earliest_start_at'] else reference
                return (duration_us(max(reference, release), end) - duration * MINUTE_US, end, *importance, task_id)
            return (end, *importance, -duration, task_id)

        while pending:
            ready = [task_id for task_id in pending if all(dep in end_times for dep in tasks[task_id]['depends_on'])]
            if not ready:
                failure = None
                break
            task_id = min(ready, key=sort_key)
            task = tasks[task_id]
            release = max([reference, *[end_times[dep] for dep in task['depends_on']]])
            if task['kind'] == 'fixed':
                start, end = _instant(task['fixed_start_at'], 'start'), _instant(task['fixed_end_at'], 'end')
                prerequisite_end = max((end_times[dep] for dep in task['depends_on'] if dep not in completed), default=start)
                if prerequisite_end > start or end > latest[task_id]:
                    failure = task_id
                    break
                entries.append(_entry(task, max(reference, start), end))
            elif task['kind'] == 'milestone':
                start = end = _instant(task['deadline_at'], 'deadline')
                if release > start or end > latest[task_id]:
                    failure = task_id
                    break
                entries.append(_entry(task, start, end))
            else:
                if task['earliest_start_at']:
                    release = max(release, _instant(task['earliest_start_at'], 'earliest'))
                allocation = _allocate(slots, release, latest[task_id], task['remaining_minutes'] * MINUTE_US,
                                       (task['min_block_minutes'] or 1) * MINUTE_US, task['splittable'])
                if allocation is None:
                    failure = task_id
                    break
                blocks, slots = allocation
                entries.extend(_entry(task, start, end) for start, end in blocks)
                end = blocks[-1][1]
            end_times[task_id] = end
            pending.remove(task_id)
            if len(entries) > MAX_SCHEDULE_BLOCKS:
                failure = task_id
                break
        if pending:
            if failure:
                failures.append(failure)
            continue
        entries.sort(key=lambda entry: (_instant(entry['start'], 'start'), _instant(entry['end'], 'end'), entry['task_id']))
        validation = validate_schedule(snapshot, entries)
        if validation['passed']:
            response.update(status='feasible_found', schedule=entries, validation=validation)
            response['algorithm']['strategy'] = strategy
            return response
        response.update(status='validation_failed', validation=validation)
        return response
    response['status'] = 'not_found'
    for task_id in dict.fromkeys(failures):
        response['issues'].append({'task_id': task_id, 'code': 'heuristic_unplaced', 'fields': [],
                                  'reason': '本次排序未找到满足全部前置与连续时长限制的空档；尚不能据此断言无解。', 'unsupported': False})
    return response
