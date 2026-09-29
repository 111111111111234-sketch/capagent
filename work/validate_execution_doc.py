"""Validate document examples and their consistency; does not execute robot code."""

import ast
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


ROOT = Path('/Users/agiuser/Documents/Codex/2026-09-24/ca')
DOC = ROOT / 'outputs/code-generation-execution-dev.md'
document = DOC.read_text()
planning = (ROOT / 'outputs/task-planning-progress-dev.md').read_text()
fences = re.findall(r'^```([^\n]*)\n(.*?)^```\s*$', document, re.M | re.S)
assert len(re.findall(r'^```', document, re.M)) == 2 * len(fences)
examples = [json.loads(body) for language, body in fences if language == 'json']
for language, body in fences:
    if language == 'python':
        ast.parse(body)

proposal = next(item for item in examples if 'code' in item)
report = next(item for item in examples if 'report_id' in item)
tree = ast.parse(proposal['code'])
api_source = (ROOT / 'cap-x/capx/integrations/franka/control_reduced.py').read_text()
api_calls = [node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
for name in api_calls:
    assert f'def {name}(' in api_source, name
assert api_calls == ['solve_ik', 'move_to_joints']
code_hash = hashlib.sha256(proposal['code'].encode()).hexdigest()
if 'code_hash' in report:
    assert report['code_hash'] == f'sha256:{code_hash}'

planning_examples = [json.loads(body) for body in re.findall(
    r'^```json\n(.*?)^```\s*$', planning, re.M | re.S)]
plan = next(item for item in planning_examples if 'subgoals' in item)
subgoal = next(item for item in plan['subgoals'] if item['id'] == report['subgoal_id'])
conditions = {item['id']: item for item in subgoal['success_conditions']}
for field in ['entry_conditions', 'expected_conditions', 'continue_conditions']:
    for condition in proposal[field]:
        assert condition == conditions[condition['id']]
assert set(api_calls) <= set(subgoal['candidate_skills'])
assert report['plan_version'] == plan['plan_version']
assert report['state_version_before'] == proposal['based_on_state_version']
assert report['api_summary']['requested'] == len(api_calls)
assert report['api_summary']['returned'] == len(api_calls)
assert report['budget_usage']['api_calls'] == len(api_calls)
assert report['status'] in {'completed', 'error', 'timed_out', 'cancelled', 'outcome_unknown'}
assert report['status'] == 'completed' and report['runtime_rc'] == 0
assert report['backend_motion_state'] == 'idle'
start = datetime.fromisoformat(report['started_at'].replace('Z', '+00:00'))
end = datetime.fromisoformat(report['ended_at'].replace('Z', '+00:00'))
assert (end - start).total_seconds() == report['budget_usage']['wall_time_s']
required = {'episode_id', 'execution_id', 'plan_version', 'subgoal_id', 'attempt_id',
            'status', 'runtime_rc', 'started_at', 'ended_at', 'evidence_refs'}
assert required <= report.keys()

source_refs = re.findall(r'`(capx/[^`:]+):(\d+)`', document)
for filename, line in source_refs:
    source_lines = (ROOT / 'cap-x' / filename).read_text().splitlines()
    assert 1 <= int(line) <= len(source_lines)
    print(f'{filename}:{line}: {source_lines[int(line)-1].strip()}')

for label, target in re.findall(r'\[([^\]]+)\]\(([^)]+)\)', document):
    if target.startswith('https://'):
        continue
    path = Path(target)
    if not path.is_absolute():
        path = DOC.parent / path
    assert path.is_file(), (label, target)

tests = re.findall(r'^\| (X\d{2}) \|', document, re.M)
assert tests == [f'X{number:02}' for number in range(1, 25)]
for index, line in enumerate(document.splitlines()):
    if re.match(r'^#{1,6} ', line):
        assert document.splitlines()[index+1] == '', line

print(f'CODE_SHA256={code_hash}')
print(f'PASS: {len(fences)} fenced blocks, {len(examples)} JSON examples, '
      f'{len(source_refs)} source references, {len(tests)} test cases; '
      'Python syntax, planning condition/API compatibility, report identity, '
      'timing and local document links checked. No robot execution performed.')
