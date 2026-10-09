"""Read only registered ExecPlans; publish valid facts to a derived tracking cache."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .execplan_progress import ProgressError, inspect
from .tracking import TrackingError, save_tracking, validate_tracking


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def raw_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SourceSync:
    def __init__(self, registry, tracking_path):
        self.registry, self.tracking_path = Path(registry), Path(tracking_path)

    def scan(self):
        config = json.loads(self.registry.read_text())
        if config.get('schema_version') != 1 or not isinstance(config.get('projects'), list):
            raise ProgressError('来源登记格式无效')
        entries, errors, project_ids = [], {}, set()
        for project in config['projects']:
            pid = project['id']
            if not isinstance(pid, str) or pid in project_ids:
                raise ProgressError('来源项目 ID 重复或无效')
            project_ids.add(pid)
            root = Path(project['root'])
            if not root.is_absolute():
                raise ProgressError('来源根必须为绝对路径')
            try:
                if not root.is_dir():
                    raise OSError('项目目录不可用')
                paths = set()
                for folder in project['plan_dirs']:
                    directory = root / folder
                    if not directory.resolve().is_relative_to(root.resolve()):
                        raise ProgressError('计划目录越出登记项目')
                    if not directory.is_dir():
                        raise OSError('登记计划目录不可用')
                    for path in directory.rglob('*.md'):
                        if path.resolve().is_relative_to(directory.resolve()) and not path.is_symlink():
                            paths.add(path)
                    if len(paths) > 1000:
                        raise ProgressError('计划数量超过检查上限')
                for path in sorted(paths):
                    entry = {'path': str(path), 'project': project, 'record': None, 'status': 'pending', 'source_version': '', 'raw_sha256': '', 'message': ''}
                    try:
                        raw = path.read_bytes()
                        if len(raw) > 2_000_000:
                            raise ProgressError('计划超过 2 MB')
                        entry['raw_sha256'] = hashlib.sha256(raw).hexdigest()
                        entry.update(inspect(raw.decode('utf-8')))
                        if entry['status'] == 'legacy':
                            entry['message'] = '旧格式尚未明确迁移，保留原观察'
                        elif entry['status'] == 'pending':
                            entry['message'] = '计划已改变，进度尚未核对发布'
                    except (OSError, ValueError) as exc:
                        entry['message'] = f'来源未通过校验：{exc}'
                    entries.append(entry)
            except (OSError, ValueError) as exc:
                errors[pid] = str(exc)
        return entries, errors

    @staticmethod
    def project_goal(entry, previous=None):
        r, p, path = entry['record'], entry['project'], entry['path']
        gid = previous['id'] if previous else r.get('goal_id', 'plan-' + hashlib.sha256(r['plan_id'].encode()).hexdigest()[:20])
        if r.get('project_id', p['id']) != p['id'] or r.get('goal_id', gid) != gid:
            raise ProgressError('计划关联与登记身份不一致')
        if previous and previous['plan_id'] != r['plan_id']:
            raise ProgressError('原路径的计划身份发生变化，保留旧身份直到明确迁移')
        if previous and previous['project_id'] != p['id']:
            raise ProgressError('计划移动到不同项目；需要明确重新关联')
        milestones = [{'id': m['id'], 'title': m['title'], 'state': m['state'], 'depends_on': m['depends_on'],
                       'source_ref': path, 'note': '\n'.join(m['evidence'])} for m in r['milestones']]
        done = sum(m['state'] == 'done' for m in milestones)
        outcome = {'unassessed': '未验收', 'met': '来源报告达成', 'not_met': '未达成', 'insufficient_evidence': '证据不足'}[r['assessment']['outcome']]
        return {'id': gid, 'project_id': p['id'], 'title': r['title'], 'target_state': r['objective'],
                'plan_id': r['plan_id'], 'plan_ref': path, 'progress': f'{done}/{len(milestones)} 项来源里程碑完成；任务完成不自动代表目标达成。',
                'assessment': outcome + '：' + r['assessment']['summary'], 'gap': r.get('gap') or '以来源验收范围为限；不代表本地独立验收。',
                'next_action': r['next_action'], 'stage_status': r['execution_status'], 'milestones': milestones,
                'sources': [{'kind': 'plan', 'ref': path, 'title': r['title'], 'observed_at': r['updated_at'], 'sha256': entry['raw_sha256']}]}

    def refresh(self, tracking):
        if not self.registry.exists():
            return tracking, []
        checked = now()
        try:
            entries, errors = self.scan()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return tracking, [{'plan_id': 'registry', 'title': '来源登记', 'status': 'unavailable', 'message': str(exc), 'checked_at': checked, 'source_version': '', 'rendered_version': ''}]
        groups = {}
        by_path = {e['path']: e for e in entries}
        for entry in entries:
            if entry['record']:
                groups.setdefault(entry['record']['plan_id'], []).append(entry)
        candidate = copy.deepcopy(tracking)
        candidate['schema_version'] = 2
        # Upgrade only existing v1 defaults, without inventing progress.
        for goal in candidate['goals']:
            goal.setdefault('stage_status', 'unknown')
            goal.setdefault('milestones', [])
        known = {g['plan_id']: g for g in candidate['goals']}
        states = []
        for plan_id in sorted(set(known) | set(groups)):
            old, matches = known.get(plan_id), groups.get(plan_id, [])
            state = {'plan_id': plan_id, 'goal_id': old['id'] if old else '', 'title': old['title'] if old else plan_id,
                     'status': 'unavailable', 'message': '', 'checked_at': checked, 'source_version': '', 'rendered_version': ''}
            entry = matches[0] if len(matches) == 1 else None
            if len(matches) > 1:
                state.update(status='pending', message='重复计划 ID；需要消除身份冲突')
            elif entry is None:
                entry = by_path.get(old['plan_ref']) if old else None
                state['message'] = errors.get(old['project_id'], '登记目录中未找到来源；保留原观察') if old else '来源不可用'
            if entry and len(matches) <= 1:
                state.update(path=entry['path'], raw_sha256=entry['raw_sha256'], source_version=entry['raw_sha256'],
                             status='pending' if entry['status'] == 'legacy' else entry['status'], message=entry['message'])
                if entry['record']:
                    state['title'] = entry['record']['title']
                if entry['status'] == 'synced':
                    try:
                        projected = self.project_goal(entry, old)
                        trial = copy.deepcopy(candidate)
                        if old:
                            trial['goals'][trial['goals'].index(old)] = projected
                        else:
                            if any(g['id'] == projected['id'] for g in trial['goals']):
                                raise ProgressError('目标 ID 重复')
                            trial['goals'].append(projected)
                        if not any(p['id'] == entry['project']['id'] for p in trial['projects']):
                            trial['projects'].append({k: entry['project'][k] for k in ('id', 'title')})
                        validate_tracking(trial)
                        candidate = trial
                        state['goal_id'] = projected['id']
                    except (ValueError, KeyError) as exc:
                        state.update(status='pending', message=str(exc))
            states.append(state)
        # Invalid newly discovered records have no trustworthy identity; make them visible.
        for entry in entries:
            if entry['status'] == 'pending' and entry['record'] is None and not any(s.get('path') == entry['path'] for s in states):
                states.append({'plan_id': 'invalid:' + entry['path'], 'title': Path(entry['path']).name, 'status': 'pending', 'message': entry['message'], 'checked_at': checked, 'source_version': '', 'rendered_version': ''})
        if candidate['goals'] != tracking['goals'] or candidate['projects'] != tracking['projects']:
            candidate.update(observed_at=checked, sync_status='ok', sync_message='已读取通过发布校验的计划；逐来源状态见新鲜度面板')
            try:
                validate_tracking(candidate)
                candidate = save_tracking(self.tracking_path, tracking['revision'], snapshot=candidate)
            except (OSError, ValueError) as exc:
                for state in states:
                    if state['status'] == 'synced':
                        state.update(status='pending', message=f'缓存未保存，将重试：{exc}')
                return tracking, states
            return candidate, states
        return tracking, states

    @staticmethod
    def stable(states):
        for state in states:
            if state['status'] == 'synced':
                try:
                    if raw_hash(state['path']) != state['raw_sha256']:
                        return False
                except OSError:
                    return False
        return True
