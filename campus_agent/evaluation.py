"""Inspectable development-case checks; not a semantic accuracy benchmark."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path


TIME_FIELDS = ('deadline_at', 'fixed_start_at', 'fixed_end_at')
FACT_FIELDS = {'action_contains_any', 'kind', *TIME_FIELDS, 'visit_windows',
               'location_contains', 'materials_allowed', 'materials_required', 'notes_required'}


def load_suite(path: Path) -> dict:
    suite = json.loads(path.read_text(encoding='utf-8'))
    if suite.get('schema_version') != 1 or suite.get('source_type') != 'synthetic' or suite.get('independent_test_set') is not False:
        raise ValueError('Only the labelled synthetic development suite is supported.')
    ids = set()
    for case in suite['cases']:
        if case['id'] in ids:
            raise ValueError('Duplicate case ID.')
        ids.add(case['id'])
        if case['source_type'] != 'synthetic':
            raise ValueError('Do not silently send personal notices in this runner.')
        for fact in case['expected_facts']:
            if set(fact) != FACT_FIELDS or not fact['action_contains_any']:
                raise ValueError('Missing/unknown fact check; refusing an incomplete score.')
    return suite


def _instant(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError('A timestamp must have an explicit timezone.')
    return parsed


def _has_action(candidate: dict, expected: dict) -> bool:
    evidence = candidate.get('evidence', [])
    if not isinstance(evidence, list):
        return False
    quotes = [item['quote'] for item in evidence if isinstance(item, dict)
              and item.get('field') == 'title' and item.get('origin') == 'source_text' and isinstance(item.get('quote'), str)]
    return any(anchor in quote for anchor in expected['action_contains_any'] for quote in quotes)


def score_case(case: dict, result: dict) -> dict:
    """Match by original action evidence, never exact generated title wording.

    One candidate can satisfy at most one expected action. Missing and duplicate
    actions cannot disappear from the denominator. Conditions and fields still
    need independent human review beyond these deliberately narrow checks.
    """
    expected = case['expected_facts']
    candidates = result.get('candidates') if isinstance(result, dict) else None
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        return {'rule_passed': False, 'expected_count': len(expected), 'actual_count': None,
                'matched_actions': 0, 'issues': [{'rule': 'response_shape'}]}
    issues = []
    if len(candidates) != len(expected):
        issues.append({'rule': 'candidate_count', 'expected': len(expected), 'actual': len(candidates)})
    edges = [[index for index, candidate in enumerate(candidates) if _has_action(candidate, fact)] for fact in expected]
    owner = {}

    def assign(expected_index, visited):
        for candidate_index in edges[expected_index]:
            if candidate_index in visited:
                continue
            visited.add(candidate_index)
            if candidate_index not in owner or assign(owner[candidate_index], visited):
                owner[candidate_index] = expected_index
                return True
        return False

    for index in range(len(expected)):
        if not assign(index, set()):
            issues.append({'rule': 'unmatched_action', 'expected_index': index})
    for candidate_index, expected_index in sorted(owner.items()):
        candidate, fact = candidates[candidate_index], expected[expected_index]

        def issue(rule):
            issues.append({'rule': rule, 'expected_index': expected_index, 'candidate_index': candidate_index})

        if candidate.get('kind') != fact['kind']:
            issue('kind')
        for field in TIME_FIELDS:
            try:
                same = field in candidate and _instant(candidate[field]) == _instant(fact[field])
            except (ValueError, TypeError):
                same = False
            if not same:
                issue(field)
        try:
            windows = [(_instant(item['start']), _instant(item['end'])) for item in candidate['visit_windows']]
            target = [(_instant(item['start']), _instant(item['end'])) for item in fact['visit_windows']]
            same_windows = windows == target
        except (KeyError, TypeError, ValueError):
            same_windows = False
        if not same_windows:
            issue('visit_windows')
        location, required_location = candidate.get('location'), fact['location_contains']
        if (required_location is None and location is not None) or (required_location is not None and (not isinstance(location, str) or required_location not in location)):
            issue('location')
        materials = candidate.get('materials')
        if not isinstance(materials, list) or not all(isinstance(item, str) for item in materials):
            issue('materials_shape')
        else:
            if any(not any(anchor in quote for anchor in fact['materials_allowed']) for quote in materials):
                issue('unexpected_material')
            if any(not any(anchor in quote for quote in materials) for anchor in fact['materials_required']):
                issue('missing_material')
        notes = candidate.get('uncertainties', [])
        notes_text = '\n'.join(note for note in notes if isinstance(note, str))
        if any(anchor not in notes_text for anchor in fact['notes_required']):
            issue('condition_notes')
    return {'rule_passed': not issues, 'expected_count': len(expected), 'actual_count': len(candidates),
            'matched_actions': len(owner), 'issues': issues}
