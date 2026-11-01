"""Independent interval/workload/dependency checks, never calls the scheduler.

No placement helpers, free-slot subtraction or greedy state are shared here.
Only the input contract and timestamp primitives are shared.
"""
from .planning_contract import CHECKS, MAX_SCHEDULE_BLOCKS, MINUTE_US, duration_us, planning_inputs
from .snapshot import FORMAT_CHECKER, _instant, validate_snapshot


def validate_schedule(snapshot, entries):
    validate_snapshot(snapshot)
    active, reference, horizon, input_issues = planning_inputs(snapshot)
    errors = [{'task_id': issue['task_id'], 'code': issue['code'], 'reason': issue['reason']} for issue in input_issues]
    by_id = {task['id']: task for task in active}
    all_tasks = {task['id']: task for task in snapshot['tasks']}
    rows = {task['id']: [] for task in active}
    occupied = []

    def error(task_id, code, reason):
        errors.append({'task_id': task_id, 'code': code, 'reason': reason})

    if not isinstance(entries, list) or len(entries) > MAX_SCHEDULE_BLOCKS:
        error(None, 'schedule_shape', '安排必须是有界的时段列表。')
        return {'passed': False, 'errors': errors, 'checked_constraints': CHECKS}
    availability = sorted((_instant(item['start'], 'availability'), _instant(item['end'], 'availability')) for item in snapshot['availability'])
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'task_id', 'kind', 'start', 'end'} or not isinstance(entry['task_id'], str):
            error(None, 'entry_shape', '安排时段字段不匹配。')
            continue
        task_id = entry['task_id']
        if task_id not in by_id:
            error(task_id, 'unexpected_task', '安排中含未知或已完成/取消的事项。')
            continue
        task = by_id[task_id]
        expected_kind = {'flexible': 'work', 'fixed': 'fixed', 'milestone': 'milestone'}.get(task['kind'])
        if entry['kind'] != expected_kind:
            error(task_id, 'entry_kind', '安排时段与事项类型不匹配。')
        if any(not isinstance(entry[field], str) or not FORMAT_CHECKER.conforms(entry[field], 'date-time') for field in ('start', 'end')):
            error(task_id, 'entry_timestamp', '安排时间必须带明确时区。')
            continue
        try:
            start, end = (_instant(entry[field], field) for field in ('start', 'end'))
        except ValueError:
            error(task_id, 'entry_timestamp', '安排时间无效。')
            continue
        if start < reference or end > horizon or end < start or (task['kind'] != 'milestone' and end == start):
            error(task_id, 'entry_bounds', '安排超出计算范围或时段顺序无效。')
        rows[task_id].append((start, end))
        if end > start:
            occupied.append((start, end, task_id))
        if task['kind'] == 'flexible':
            # Check coverage by independently walking the original intervals.
            cursor = start
            for begin, finish in availability:
                if finish <= cursor:
                    continue
                if begin > cursor:
                    break
                cursor = max(cursor, finish)
                if cursor >= end:
                    break
            if cursor < end:
                error(task_id, 'outside_availability', '工作时段没有完全位于用户确认的空闲内。')
            release = max(reference, _instant(task['earliest_start_at'], 'earliest') if task['earliest_start_at'] else reference)
            if start < release or not task['deadline_at'] or end > _instant(task['deadline_at'], 'deadline'):
                error(task_id, 'release_or_deadline', '工作早于可开始时间或晚于截止。')

    previous_end = None
    for start, end, task_id in sorted(occupied):
        if previous_end is not None and start < previous_end:
            error(task_id, 'overlap', '工作或固定占用存在重叠。')
        previous_end = end if previous_end is None else max(previous_end, end)
    for task in active:
        task_id = task['id']
        placed = sorted(rows[task_id])
        if task['kind'] == 'flexible':
            if not placed or sum(duration_us(start, end) for start, end in placed) != (task['remaining_minutes'] or 0) * MINUTE_US:
                error(task_id, 'workload', '安排时长没有恰好覆盖剩余工作量。')
            if task['splittable'] is False and len(placed) != 1:
                error(task_id, 'indivisible', '不可拆分工作必须只有一个连续时段。')
            if task['splittable'] is True and any(duration_us(start, end) < (task['min_block_minutes'] or 1) * MINUTE_US for start, end in placed):
                error(task_id, 'minimum_block', '工作块短于确认的最小连续时长。')
        elif task['kind'] == 'fixed':
            if task['fixed_start_at'] and task['fixed_end_at']:
                expected = [(max(reference, _instant(task['fixed_start_at'], 'fixed_start')), _instant(task['fixed_end_at'], 'fixed_end'))]
                if placed != expected:
                    error(task_id, 'fixed_moved_or_missing', '固定活动被移动、拆分或遗漏。')
        elif task['kind'] == 'milestone':
            if task['deadline_at']:
                deadline = _instant(task['deadline_at'], 'deadline')
                if placed != [(deadline, deadline)]:
                    error(task_id, 'milestone', '截止节点必须在原截止时刻且不占用工时。')
        for dependency in task['depends_on']:
            if all_tasks[dependency]['status'] == 'completed':
                continue
            predecessor = rows.get(dependency, [])
            if not predecessor or not placed:
                error(task_id, 'dependency_missing', '前置事项或当前事项没有完整安排。')
                continue
            successor_start = _instant(task['fixed_start_at'], 'fixed_start') if task['kind'] == 'fixed' and task['fixed_start_at'] else min(start for start, _ in placed)
            if max(end for _, end in predecessor) > successor_start:
                error(task_id, 'dependency_order', '前置事项未在本事项开始前完成。')
    return {'passed': not errors, 'errors': errors, 'checked_constraints': CHECKS}
