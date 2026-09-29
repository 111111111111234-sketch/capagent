"""Check CoF document fixtures and cross-document consistency, not vision quality."""

import ast
import json
import re
from pathlib import Path


ROOT = Path('/Users/agiuser/Documents/Codex/2026-09-24/ca')
DOC = ROOT / 'outputs/cof-execution-feedback-dev.md'
document = DOC.read_text()
planning = (ROOT / 'outputs/task-planning-progress-dev.md').read_text()
fences = re.findall(r'^```([^\n]*)\n(.*?)^```\s*$', document, re.M | re.S)
assert len(re.findall(r'^```', document, re.M)) == len(fences) * 2
examples = [json.loads(body) for language, body in fences if language == 'json']
for language, body in fences:
    if language == 'python':
        ast.parse(body)

spec = next(item for item in examples if 'queries' in item)
feedback = next(item for item in examples if 'feedback_id' in item)
assert feedback['query_spec_ref'] == spec['query_spec_id']
assert feedback['time_basis'] == spec['time_basis']
queries = {item['query_id']: item for item in spec['queries']}
assert len(queries) == len(spec['queries'])
plan_examples = [json.loads(body) for body in re.findall(
    r'^```json\n(.*?)^```\s*$', planning, re.M | re.S)]
plan = next(item for item in plan_examples if 'subgoals' in item)
subgoal = next(item for item in plan['subgoals'] if item['id'] == feedback['subgoal_id'])
conditions = {item['id']: item for item in subgoal['success_conditions']}
for query in queries.values():
    assert query['condition'] == conditions[query['condition']['id']]
    assert query['window_s'][0] <= query['window_s'][1]
    assert query['temporal_mode'] == 'at_end'
assert feedback['plan_version'] == plan['plan_version']

frames = {}
for line in document.splitlines():
    if re.match(r'^\| f\d+ \|', line):
        values = [value.strip() for value in line.split('|')[1:-1]]
        fid, time, camera, group, call, label = values
        assert fid not in frames
        frames[fid] = {'t': float(time), 'camera': camera, 'group': group,
                       'call': call, 'label': label}
assert len(frames) == feedback['coverage']['selected_frame_count'] == 6
times = [frame['t'] for frame in frames.values()]
assert times == sorted(times)
assert feedback['coverage']['window_s'] == [min(times), max(times)]
gap = max(b - a for a, b in zip(times, times[1:]))
assert abs(gap - feedback['coverage']['max_selected_gap_s']) < 1e-9
assert feedback['coverage']['continuous_guarantee'] is False

calls = {'call_close': (0.1, 0.5), 'call_lift': (0.5, 1.5)}
for frame in frames.values():
    if frame['call'] != 'null':
        assert frame['call'] in calls
        start, end = calls[frame['call']]
        assert start <= frame['t'] <= end

def check_refs(item):
    assert item['evidence_refs']
    assert set(item['evidence_refs']) <= frames.keys()


events = {item['event_id']: item for item in feedback['events']}
assert len(events) == len(feedback['events'])
for event in events.values():
    check_refs(event)
    start, end = event['interval_s']
    assert 0 <= start <= end <= 2.0
    assert set(event['call_refs']) <= calls.keys()
    for ref in event['evidence_refs']:
        assert start <= frames[ref]['t'] <= end
    for ref in event['call_refs']:
        assert max(start, calls[ref][0]) <= min(end, calls[ref][1])

for claim in feedback['observed_changes']:
    check_refs(claim)
    assert type(claim['candidate_value']) is bool
    assert claim['at_s'] in [frames[ref]['t'] for ref in claim['evidence_refs']]

seen_queries = set()
for item in feedback['condition_evidence']:
    check_refs(item)
    query = queries[item['query_id']]
    assert item['condition_id'] == query['condition']['id']
    assert item['assessment'] in {'supported', 'refuted', 'unknown'}
    start, end = query['window_s']
    assert start <= item['observed_at_s'] <= end
    for ref in item['evidence_refs']:
        assert start <= frames[ref]['t'] <= end
    assert item['query_id'] not in seen_queries
    seen_queries.add(item['query_id'])
assert seen_queries == queries.keys()
for item in feedback['deviations']:
    assert set(item['query_refs']) <= queries.keys()
    assert set(item['event_refs']) <= events.keys()
assert not feedback['cause_hypotheses']
assert any(item['kind'] == 'cause_unobserved' for item in feedback['uncertainties'])
assert feedback['analysis_status'] in {'ready', 'partial', 'unavailable', 'invalid'}

source_refs = re.findall(r'`(capx/[^`:]+):(\d+)`', document)
for filename, line in source_refs:
    source_lines = (ROOT / 'cap-x' / filename).read_text().splitlines()
    assert 1 <= int(line) <= len(source_lines)
    print(f'{filename}:{line}: {source_lines[int(line)-1].strip()}')
for label, target in re.findall(r'\[([^\]]+)\]\(([^)]+)\)', document):
    if target.startswith('https://'):
        continue
    assert Path(target).is_absolute(), target
    assert Path(target).is_file(), (label, target)
test_ids = re.findall(r'^\| (F\d{2}) \|', document, re.M)
assert test_ids == [f'F{n:02}' for n in range(1, 27)]
lines = document.splitlines()
for index, line in enumerate(lines):
    if re.match(r'^#{1,6} ', line):
        assert lines[index + 1] == '', line

print(f'PASS: {len(fences)} fenced blocks, {len(examples)} JSON examples, '
      f'{len(frames)} timestamped frames, {len(events)} events, '
      f'{len(queries)} planning-compatible conditions, '
      f'{len(source_refs)} source references, {len(test_ids)} test cases. '
      'No model inference, simulation or robot execution performed.')
