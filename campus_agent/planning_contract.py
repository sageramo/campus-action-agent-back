"""Input policy shared by placement and independent schedule validation."""
from datetime import timedelta

from .snapshot import INACTIVE, _instant, critical_fields

MAX_ACTIVE_TASKS = 40
MAX_HORIZON_DAYS = 31
MAX_SCHEDULE_BLOCKS = 4096
MINUTE_US = 60_000_000
CHECKS = ['confirmed_inputs', 'availability', 'fixed_occupancy', 'no_overlap',
          'release_and_deadline', 'exact_remaining_work', 'continuous_blocks', 'dependencies']


def duration_us(start, end):
    delta = end - start
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def planning_inputs(snapshot):
    """Return active tasks, horizon and explicit readiness/scope issues.

    Caller validates the v2 snapshot first. This is eligibility policy, not the
    placement algorithm or interval/coverage validation.
    """
    reference = _instant(snapshot['reference_time'], 'reference_time')
    active = [task for task in snapshot['tasks'] if task['status'] not in INACTIVE]
    by_id = {task['id']: task for task in snapshot['tasks']}
    issues = []

    def issue(task, code, fields, reason, unsupported=False):
        issues.append({'task_id': task['id'] if task else None, 'code': code,
                       'fields': fields, 'reason': reason, 'unsupported': unsupported})

    if len(active) > MAX_ACTIVE_TASKS:
        issue(None, 'task_limit', [], '本版最多同时安排40个未完成事项，请分批规划。', True)
    known = snapshot.get('availability_confirmed', bool(snapshot['availability']))
    if active and not known:
        issue(None, 'availability_unknown', ['availability'], '请先确认真实空闲时段；未填写不能当作全天空闲。')
    endpoints = [reference]
    for task in active:
        required = critical_fields(task)
        absent = sorted(field for field in required if task[field] is None or (field == 'title' and not task[field].strip()) or (field == 'visit_windows' and not task[field]))
        if task['status'] == 'draft':
            issue(task, 'draft', ['status'], '事项尚未逐项确认，不能自动放入日程。')
        if absent:
            issue(task, 'missing_fields', absent, '必要时间、工作量或个人选择尚未填写。')
        unconfirmed = required - set(task['confirmed_fields'])
        for optional in ('earliest_start_at', 'depends_on'):
            if task[optional] and optional not in task['confirmed_fields']:
                unconfirmed.add(optional)
        if unconfirmed:
            issue(task, 'unconfirmed_fields', sorted(unconfirmed), '这些影响安排的字段尚未确认。')
        if task['uncertainties']:
            issue(task, 'unresolved_notes', ['uncertainties'], '仍有待核对提醒，请核对后重新确认事项。')
        if task['kind'] == 'visit_window':
            issue(task, 'visit_window_unsupported', ['kind'], '本版尚未自动选择办理窗口；请先确定实际参与/办理时段。', True)
        if task['kind'] == 'flexible' and not task['remaining_minutes']:
            issue(task, 'unknown_or_zero_work', ['remaining_minutes'], '未完成工作需要正整数剩余分钟，不能按零工作量安排。')
        if task['kind'] == 'flexible' and task['splittable'] and task['min_block_minutes'] and task['remaining_minutes'] and task['min_block_minutes'] > task['remaining_minutes']:
            issue(task, 'minimum_exceeds_work', ['min_block_minutes'], '最小连续时长超过剩余工作量。')
        if task['kind'] != 'flexible' and task['earliest_start_at'] is not None:
            issue(task, 'extra_release_unsupported', ['earliest_start_at'], '本版只对待安排工作实施最早开始约束。', True)
        if task['kind'] == 'fixed' and task['deadline_at'] is not None:
            issue(task, 'fixed_deadline_unsupported', ['deadline_at'], '固定活动另带截止条件的含义需要先核对，本版不忽略此条件。', True)
        if task['kind'] == 'fixed' and task['fixed_end_at'] is not None and _instant(task['fixed_end_at'], 'end') <= reference:
            issue(task, 'past_fixed_status', ['status'], '固定活动已经结束，请核对并更新完成/取消状态，不将历史活动重新排到未来。')
        for dependency in task['depends_on']:
            if by_id[dependency]['status'] == 'cancelled':
                issue(task, 'cancelled_dependency', ['depends_on'], '前置事项已取消，不能自动视作完成；请核对依赖关系。')
        for field in ('deadline_at', 'fixed_end_at'):
            if task[field] is not None:
                endpoints.append(_instant(task[field], field))
    horizon = max(endpoints)
    if horizon - reference > timedelta(days=MAX_HORIZON_DAYS):
        issue(None, 'horizon_limit', [], '本版一次覆盖最长31天；请分阶段规划，不要为适配工具改写正式截止。', True)
    return active, reference, horizon, issues
