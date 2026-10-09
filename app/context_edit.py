"""Atomic, versioned edits of local classification; never edits external plans."""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import uuid

from .graphs import validate_context
from .project_read_model import registered_projects
from .model import ValidationError
from .store import StorageError, WorkspaceStore


class ContextConflict(Exception):
    pass


def version(context):
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def read_context(path, project_id, tracking):
    try:
        context = json.loads(Path(path).read_text())
        validate_context(context, project_id, tracking, registered_projects(Path(path).parent / "reader-sources.json"))
        return {'context': context, 'version': version(context)}
    except (OSError, ValueError) as exc:
        raise StorageError('归属说明暂不可用；未创建空文件') from exc


def _reference(context, kind, ref):
    if not isinstance(ref, dict) or set(ref) not in ({'id'}, {'title'}):
        raise ValidationError('请选择已有条目或输入新名称')
    items = context[kind]
    if 'id' in ref:
        node = next((n for n in items if n['id'] == ref['id']), None)
        if node is None:
            raise ValidationError('所选条目不存在，请重新读取')
    else:
        title = ref['title']
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise ValidationError('名称需要 1–120 个字符')
        title = title.strip()
        node = next((n for n in items if n['title'].strip().casefold() == title.casefold()), None)
        if node is None:
            node = {'id': ('area-' if kind == 'areas' else 'direction-') + uuid.uuid4().hex,
                    'title': title, 'status': 'confirmed'}
            items.append(node)
    node['status'] = 'confirmed'
    return node['id']


def apply_change(context, change, project_id, tracking, registered=()):
    if not isinstance(change, dict):
        raise ValidationError('修改内容无效')
    result = copy.deepcopy(context)
    projects = {project_id} | {f'tracking:project:{p["id"]}' for p in tracking['projects']}
    projects |= {p["id"] for p in registered}
    action = change.get('action')
    if action == 'restore' and set(change) == {'action', 'context'}:
        result = copy.deepcopy(change['context'])
    elif action == 'assign_area' and set(change) == {'action', 'project_id', 'area'}:
        subject = change['project_id']
        if not isinstance(subject, str) or subject not in projects:
            raise ValidationError('项目不存在')
        target = None if change['area'] is None else _reference(result, 'areas', change['area'])
        result['relations'] = [r for r in result['relations'] if not (r['from'] == subject and r['kind'] == 'belongs_to')]
        if target:
            result['relations'].append({'from': subject, 'to': target, 'kind': 'belongs_to', 'status': 'confirmed'})
        result['schema_version'] = 2
    elif action == 'set_directions' and set(change) == {'action', 'subject_id', 'directions'}:
        subject = change['subject_id']
        if not isinstance(subject, str) or subject not in projects | {n['id'] for n in result['areas']}:
            raise ValidationError('项目或领域不存在')
        refs = change['directions']
        if not isinstance(refs, list) or len(refs) > 100:
            raise ValidationError('方向列表无效')
        targets = [_reference(result, 'directions', ref) for ref in refs]
        if len(targets) != len(set(targets)):
            raise ValidationError('同一方向不能重复选择')
        result['relations'] = [r for r in result['relations'] if not (r['from'] == subject and r['kind'] == 'serves')]
        result['relations'] += [{'from': subject, 'to': target, 'kind': 'serves', 'status': 'confirmed'} for target in targets]
        result['schema_version'] = 2
    elif action == 'confirm_area' and set(change) == {'action', 'area_id'}:
        _reference(result, 'areas', {'id': change['area_id']})
    else:
        raise ValidationError('不支持的归属修改')
    validate_context(result, project_id, tracking, registered)
    return result


def save_context(path, project_id, tracking, expected_version, change):
    path = Path(path)
    # No parent creation: an absent project/volume must remain unavailable.
    try:
        with path.with_suffix(path.suffix + '.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            before = read_context(path, project_id, tracking)
            if not isinstance(expected_version, str) or expected_version != before['version']:
                raise ContextConflict('归属已被其他操作更新；重新读取后再保存，当前输入仍保留')
            after = apply_change(before['context'], change, project_id, tracking, registered_projects(path.parent / 'reader-sources.json'))
            new_version = version(after)
            if new_version != before['version']:
                WorkspaceStore._write_atomically(path.with_suffix(path.suffix + '.bak'), before['context'])
                # Detect non-cooperating saved edits before replacement too.
                if read_context(path, project_id, tracking)['version'] != expected_version:
                    raise ContextConflict('保存期间归属发生变化；没有覆盖新内容')
                WorkspaceStore._write_atomically(path, after)
            return {'context': after, 'version': new_version, 'previous': before['context'],
                    'changed': new_version != before['version']}
    except OSError as exc:
        raise StorageError('归属保存失败；请检查本机文件或磁盘后重试') from exc
