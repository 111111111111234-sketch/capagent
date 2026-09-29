"""Check the development document's machine-readable examples and references."""

import ast
import json
import re
from pathlib import Path


root = Path('/Users/agiuser/Documents/Codex/2026-09-24/ca')
document = (root / 'outputs/task-planning-progress-dev.md').read_text()
fences = re.findall(r'^```([^\n]*)\n(.*?)^```\s*$', document, re.M | re.S)
assert len(re.findall(r'^```', document, re.M)) == len(fences) * 2
json_examples = [json.loads(body) for language, body in fences if language == 'json']
for language, body in fences:
    if language == 'python':
        ast.parse(body)

plan = next(example for example in json_examples if 'subgoals' in example)
nodes = {node['id']: node for node in plan['subgoals']}
assert len(nodes) == len(plan['subgoals'])
checked = set()
visiting = set()


def visit(node_id):
    assert node_id in nodes, f'Missing dependency: {node_id}'
    assert node_id not in visiting, f'Dependency cycle: {node_id}'
    if node_id in checked:
        return
    visiting.add(node_id)
    for dependency in nodes[node_id]['depends_on']:
        visit(dependency)
    visiting.remove(node_id)
    checked.add(node_id)


conditions = {}
api_source = (root / 'cap-x/capx/integrations/franka/control_reduced.py').read_text()
for node_id, node in nodes.items():
    visit(node_id)
    assert node['success_conditions']
    for condition in node['preconditions'] + node['success_conditions']:
        assert isinstance(condition['expected'], bool)
        previous = conditions.setdefault(condition['id'], condition)
        assert previous == condition, f'Conflicting condition: {condition["id"]}'
    for skill in node['candidate_skills']:
        assert f'def {skill}(' in api_source, f'Missing example API: {skill}'
for goal, targets in plan['goal_coverage'].items():
    assert goal in conditions
    for target in targets:
        assert any(c['id'] == goal for c in nodes[target]['success_conditions'])

references = re.findall(r'`(capx/[^`:]+):(\d+)`', document)
for filename, line in references:
    source_lines = (root / 'cap-x' / filename).read_text().splitlines()
    assert 1 <= int(line) <= len(source_lines)
    print(f'{filename}:{line}: {source_lines[int(line)-1].strip()}')
print(f'PASS: {len(fences)} fenced blocks; {len(json_examples)} JSON examples; '
      f'{len(nodes)} acyclic subgoals; consistent conditions and existing example APIs.')
