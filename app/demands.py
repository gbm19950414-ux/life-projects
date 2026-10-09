"""Private, revisioned demand ledger. No action here dispatches an agent or task.

``apply`` takes {expected_revision, reason, source_ref, operations}. Each operation
has ``op`` and ``id``. Supported operations and their additional fields:

* propose: title, outcome, kind, target_project ({id,path} or null),
  constraints, sources; optional allowlist (relative paths), provenance and
  implementation ({change,acceptance,stop_conditions,plan_path} or null).
* append_sources: sources.
* assess: analysis ({capability,gap,options,recommendation,disposition,evidence}),
  where disposition is use_existing, needs_upgrade, needs_clarification,
  deferred, or not_pursued.
* revise_proposal: changes (proposal fields except sources).
* revise: changes, sources, supersedes_event_id, correction_evidence
  (same local user_message evidence shape as decide). This is for verified
  user corrections to a delivered voice event.
* decide: decision (approved|rejected|deferred), proposal_sha256, evidence
  ({kind:"user_message",ref:<absolute local file>,sha256:<raw bytes hash>}).
* link_execution: handoff_id, thread_id, goal_id (nullable), goal_status
  (linked|not_created|unknown), plan_path, plan_id.
* feedback: dimension (technical|usage), outcome (met|not_met|
  insufficient_evidence), summary, evidence (source references).
* close: resolution (completed). Technical and usage results must both be met.

An agent must independently authenticate the human authorship of approval
evidence. A JSON request or a matching file hash cannot do that.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from . import execplan_progress


SCHEMA = 'demand-lifecycle/v1'
DEFAULT_DATA = Path(__file__).resolve().parents[1] / 'private/demand-lifecycle/demands.json'
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
SHA = re.compile(r'^[0-9a-f]{64}$')
MAX_ITEMS = 1000


class DemandError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DemandError(f'duplicate JSON key: {key}')
        value[key] = item
    return value


def _text(value, name, max_len=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise DemandError(f'{name}: expected nonempty bounded text')
    return value


def _id(value, name='id'):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise DemandError(f'{name}: invalid identifier')
    return value


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise DemandError(f'fields must be {sorted(required)} with optional {sorted(optional)}')


def _strings(value, name):
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise DemandError(f'{name}: expected bounded list')
    for item in value:
        _text(item, name)
    return value


def _relative_paths(value):
    _strings(value, 'allowlist')
    for item in value:
        p = Path(item)
        if p.is_absolute() or not p.parts or any(part in ('.', '..') for part in p.parts):
            raise DemandError('allowlist must contain relative paths inside target project')
    if len(set(value)) != len(value):
        raise DemandError('duplicate allowlist path')
    return value


def _source(value):
    _fields(value, {'kind', 'ref'}, {'sha256', 'event_id', 'delivery_sha256', 'audio_sha256', 'recording_id', 'quote', 'supersedes_event_id'})
    _text(value['kind'], 'source kind', 100)
    _text(value['ref'], 'source ref', 2000)
    for key in ('sha256', 'delivery_sha256', 'audio_sha256'):
        if key in value and (not isinstance(value[key], str) or not SHA.fullmatch(value[key])):
            raise DemandError(f'invalid source {key}')
    for key in ('event_id', 'supersedes_event_id'):
        if key in value:
            _id(value[key], key)
    if 'recording_id' in value and value['recording_id'] is not None:
        _text(value['recording_id'], 'recording_id', 2000)
    if 'quote' in value:
        _text(value['quote'], 'quote', 2000)
    return value


def _sources(values):
    if not isinstance(values, list) or len(values) > MAX_ITEMS:
        raise DemandError('sources must be a bounded list')
    seen = set()
    for source in values:
        _source(source)
        digest = _digest(source)
        if digest in seen:
            raise DemandError('duplicate source entry')
        seen.add(digest)
    return values


def _proposal(value):
    _fields(value, {'title', 'outcome', 'kind', 'target_project', 'constraints', 'allowlist', 'provenance', 'implementation'})
    for key in ('title', 'outcome', 'kind'):
        _text(value[key], key)
    if value['target_project'] is not None:
        _fields(value['target_project'], {'id', 'path'})
        _id(value['target_project']['id'], 'target project ID')
        _text(value['target_project']['path'], 'target project path', 2000)
    _strings(value['constraints'], 'constraints')
    _relative_paths(value['allowlist'])
    if value['provenance'] not in ('machine_interpreted', 'user_confirmed'):
        raise DemandError('provenance must be machine_interpreted or user_confirmed')
    implementation = value['implementation']
    if implementation is not None:
        _fields(implementation, {'change', 'acceptance', 'stop_conditions', 'plan_path'})
        _text(implementation['change'], 'implementation change')
        for name in ('acceptance', 'stop_conditions'):
            _strings(implementation[name], name)
            if not implementation[name]:
                raise DemandError(f'implementation {name} must be nonempty')
        if not Path(_text(implementation['plan_path'], 'implementation plan_path', 2000)).is_absolute():
            raise DemandError('implementation plan_path must be absolute')
    return value


def _analysis(value):
    _fields(value, {'capability', 'gap', 'options', 'recommendation', 'disposition', 'evidence'})
    for key in ('capability', 'gap', 'recommendation'):
        _text(value[key], key)
    _strings(value['options'], 'options')
    if value['disposition'] not in ('use_existing', 'needs_upgrade', 'needs_clarification', 'deferred', 'not_pursued'):
        raise DemandError('invalid analysis disposition')
    _sources(value['evidence'])
    return value


def _local_evidence(value):
    _fields(value, {'kind', 'ref', 'sha256'})
    if value['kind'] != 'user_message':
        raise DemandError('approval needs user_message evidence')
    ref = Path(_text(value['ref'], 'evidence ref', 2000))
    if not ref.is_absolute() or not ref.is_file():
        raise DemandError('approval evidence must be an existing absolute local file')
    if not isinstance(value['sha256'], str) or not SHA.fullmatch(value['sha256']):
        raise DemandError('invalid evidence sha256')
    try:
        actual = hashlib.sha256(ref.read_bytes()).hexdigest()
    except OSError as exc:
        raise DemandError(f'approval evidence unavailable: {exc}') from exc
    if actual != value['sha256']:
        raise DemandError('approval evidence hash mismatch')
    return value


def _validate(data):
    _fields(data, {'schema', 'revision', 'demands', 'changes', 'updated_at'})
    if data['schema'] != SCHEMA or type(data['revision']) is not int or data['revision'] < 1:
        raise DemandError('invalid ledger schema or revision')
    _text(data['updated_at'], 'updated_at')
    if not isinstance(data['demands'], dict) or len(data['demands']) > MAX_ITEMS:
        raise DemandError('invalid demand map')
    if not isinstance(data['changes'], list) or len(data['changes']) > 100000:
        raise DemandError('invalid change log')
    events = {}
    for key, demand in data['demands'].items():
        _id(key)
        _fields(demand, {'id', 'proposal', 'proposal_sha256', 'sources', 'analysis', 'decision', 'handoff', 'execution', 'authorization_stale', 'feedback', 'closure', 'history', 'created_at', 'updated_at'})
        if key != demand['id']:
            raise DemandError('demand key/id mismatch')
        _proposal(demand['proposal'])
        if demand['proposal_sha256'] != _digest(demand['proposal']):
            raise DemandError('proposal digest mismatch')
        _sources(demand['sources'])
        for source in demand['sources']:
            event = source.get('event_id')
            if event:
                if event in events and events[event] != key:
                    raise DemandError('event ID collision across demands')
                events[event] = key
        if demand['analysis'] is not None:
            _analysis(demand['analysis'])
        if demand['decision'] is not None:
            decision = demand['decision']
            _fields(decision, {'decision', 'proposal_sha256', 'evidence', 'at'})
            if decision['decision'] not in ('approved', 'rejected', 'deferred'):
                raise DemandError('invalid decision')
            if not isinstance(decision['proposal_sha256'], str) or not SHA.fullmatch(decision['proposal_sha256']):
                raise DemandError('invalid decision proposal digest')
            if decision['proposal_sha256'] != demand['proposal_sha256']:
                raise DemandError('current decision is bound to an old proposal')
            _fields(decision['evidence'], {'kind', 'ref', 'sha256'})
            _text(decision['at'], 'decision time')
        if demand['handoff'] is not None:
            _fields(demand['handoff'], {'id', 'path', 'sha256', 'proposal_sha256', 'prepared_at'})
            if not demand['authorization_stale'] and demand['handoff']['proposal_sha256'] != demand['proposal_sha256']:
                raise DemandError('handoff is bound to an old proposal')
        if type(demand['authorization_stale']) is not bool:
            raise DemandError('authorization_stale must be boolean')
        if demand['execution'] is not None:
            _fields(demand['execution'], {'handoff_id', 'thread_id', 'goal_id', 'goal_status', 'plan_path', 'plan_id', 'observation', 'linked_at'})
            if demand['handoff'] is None or demand['execution']['handoff_id'] != demand['handoff']['id']:
                raise DemandError('execution is not linked to current handoff')
        if demand['authorization_stale'] and (demand['execution'] is None or demand['handoff'] is None):
            raise DemandError('stale authorization requires preserved old execution and handoff')
        if not isinstance(demand['feedback'], dict) or set(demand['feedback']) != {'technical', 'usage'}:
            raise DemandError('invalid feedback dimensions')
        for result in demand['feedback'].values():
            if result is not None:
                _fields(result, {'outcome', 'summary', 'evidence', 'at'})
                if result['outcome'] not in ('met', 'not_met', 'insufficient_evidence'):
                    raise DemandError('invalid feedback outcome')
                _text(result['summary'], 'feedback summary')
                _sources(result['evidence'])
                if result['outcome'] == 'met' and not result['evidence']:
                    raise DemandError('met feedback without evidence')
        if demand['closure'] is not None:
            _fields(demand['closure'], {'resolution', 'at'})
            upgraded = (demand['analysis'] is not None and demand['analysis']['disposition'] == 'needs_upgrade'
                        and demand['decision'] is not None and demand['decision']['decision'] == 'approved'
                        and demand['decision']['proposal_sha256'] == demand['proposal_sha256'])
            existing = (demand['analysis'] is not None and demand['analysis']['disposition'] == 'use_existing'
                        and demand['proposal']['implementation'] is None and demand['handoff'] is None
                        and demand['execution'] is None)
            if demand['authorization_stale'] or demand['closure']['resolution'] != 'completed' or not (upgraded or existing) or any(not result or result['outcome'] != 'met' for result in demand['feedback'].values()):
                raise DemandError('invalid completed closure')
        if not isinstance(demand['history'], list):
            raise DemandError('invalid history')
    return data


def _path(path):
    return Path(path).expanduser().resolve()


def _atomic(path, value):
    raw = _encoded(value)
    name = None
    try:
        with tempfile.NamedTemporaryFile(prefix=path.name + '.tmp-', dir=path.parent, delete=False) as handle:
            name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        name = None
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')


def _load(path):
    try:
        raw = path.read_bytes()
        data = json.loads(raw, object_pairs_hook=_unique)
        return _validate(data), raw
    except FileNotFoundError as exc:
        raise DemandError('demand ledger unavailable; run explicit init or recover') from exc
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise DemandError(f'demand ledger unreadable; recover explicitly: {exc}') from exc


def read(path=DEFAULT_DATA):
    """Read and validate an existing ledger; never initializes on missing data."""
    return _load(_path(path))[0]


def _lock_path(path):
    return path.with_name(path.name + '.lock')


def _backup_path(path):
    return path.with_name(path.name + '.bak')


def initialize(path=DEFAULT_DATA):
    """Create an empty ledger only when explicitly requested, without overwriting history."""
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, path.parent.stat().st_mode & 0o700)
    with _lock_path(path).open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = path.with_name(path.name + '.initialized')
        if any(x.exists() or x.is_symlink() for x in (path, _backup_path(path), marker)):
            raise DemandError('ledger, backup, or initialization marker exists; refusing init')
        initial = {'schema': SCHEMA, 'revision': 1, 'demands': {}, 'changes': [], 'updated_at': _now()}
        _atomic(path, initial)
        _atomic(marker, {'schema': SCHEMA, 'initialized': True})
        return initial


def _get(data, ident):
    _id(ident)
    try:
        return data['demands'][ident]
    except KeyError as exc:
        raise DemandError(f'unknown demand: {ident}') from exc


def _merge_sources(demand, incoming, data):
    _sources(incoming)
    existing = {}
    globally = {}
    for other_id, item in data['demands'].items():
        for source in item['sources']:
            event = source.get('event_id')
            if event:
                globally.setdefault(event, other_id)
                if other_id == demand['id']:
                    existing.setdefault(event, []).append(source)
    grouped = {}
    for source in incoming:
        if source.get('event_id'):
            grouped.setdefault(source['event_id'], []).append(source)
    for event, sources in grouped.items():
        if event in globally and globally[event] != demand['id']:
            raise DemandError('source event already belongs to another demand')
        if event in existing and not set(map(_digest, sources)) <= set(map(_digest, existing[event])):
            raise DemandError('same event ID has different source content')
    changed = False
    for source in incoming:
        if source not in demand['sources']:
            demand['sources'].append(copy.deepcopy(source))
            changed = True
    return changed


def _execution_target(demand):
    """Use the original approved target while an old execution is still linked."""
    if demand['authorization_stale']:
        handoff_id = demand['execution']['handoff_id']
        for item in reversed(demand['history']):
            if item.get('handoff') and item['handoff']['id'] == handoff_id:
                return item['proposal']['target_project']
        raise DemandError('old execution target is missing from revision history')
    return demand['proposal']['target_project']


def _archived_plan(execution, target):
    """Resolve only a missing approved active/archive plan by stable identity."""
    original = Path(execution['plan_path'])
    if not target or not isinstance(target.get('path'), str):
        raise DemandError('approved target project is unavailable')
    root = Path(target['path']).resolve()
    parent = original.parent
    if (original.suffix != '.md' or parent.name not in ('active', 'archive')
            or parent.parent.name != 'exec-plans' or original.is_symlink()
            or not original.resolve().is_relative_to(root)):
        raise DemandError('approved path is outside target active/archive ExecPlan directories')
    matches = []
    scanned = 0
    for name in ('active', 'archive'):
        directory = parent.parent / name
        if not directory.is_dir() or not directory.resolve().is_relative_to(root):
            continue
        for candidate in directory.rglob('*.md'):
            scanned += 1
            if scanned > 1000:
                raise DemandError('active/archive plan scan exceeds 1000 Markdown files')
            if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve().is_relative_to(directory.resolve()):
                continue
            try:
                raw = candidate.read_bytes()
                if len(raw) > 2_000_000:
                    continue
                inspected = execplan_progress.inspect(raw.decode('utf-8'))
            except (OSError, UnicodeError, ValueError):
                continue
            record = inspected['record']
            if record and record['plan_id'] == execution['plan_id']:
                matches.append((candidate, raw, inspected))
    if len(matches) != 1:
        raise DemandError(f'expected one archived plan for {execution["plan_id"]}; found {len(matches)}')
    path, raw, inspected = matches[0]
    record = inspected['record']
    if inspected['status'] != 'synced' or record.get('project_id') != target['id']:
        raise DemandError('archived plan is unpublished or belongs to another project')
    return path, raw, inspected


def _plan_observation(execution, target):
    path = Path(execution['plan_path'])
    old = execution['observation']
    try:
        try:
            raw = path.read_bytes()
            inspected = execplan_progress.inspect(raw.decode('utf-8'))
        except FileNotFoundError:
            path, raw, inspected = _archived_plan(execution, target)
        record = inspected['record']
        if (inspected['status'] != 'synced' or not record or record['plan_id'] != execution['plan_id']
                or not target or record.get('project_id') != target['id']):
            raise DemandError('linked ExecPlan is unpublished or has different plan/project identity')
        return {'freshness': 'fresh', 'status': inspected['status'], 'source_version': inspected['source_version'],
                'source_path': str(path), 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                'execution_status': record['execution_status'],
                'assessment': copy.deepcopy(record['assessment']), 'milestones': copy.deepcopy(record['milestones']),
                'observed_at': _now(), 'error': None}
    except (OSError, UnicodeError, ValueError) as exc:
        if old is None:
            return {'freshness': 'stale', 'status': 'unavailable', 'source_version': None, 'raw_sha256': None,
                    'source_path': None, 'execution_status': 'unknown', 'assessment': None, 'milestones': [],
                    'observed_at': _now(), 'error': str(exc)[:500]}
        result = copy.deepcopy(old)
        result['freshness'] = 'stale'
        result['error'] = str(exc)[:500]
        return result


def _operation(data, op, at):
    if not isinstance(op, dict) or 'op' not in op:
        raise DemandError('operation requires op')
    kind = op['op']
    if kind == 'propose':
        _fields(op, {'op', 'id', 'title', 'outcome', 'kind', 'target_project', 'constraints', 'sources'}, {'allowlist', 'provenance', 'implementation'})
        ident = _id(op['id'])
        proposal = {key: copy.deepcopy(op[key]) for key in ('title', 'outcome', 'kind', 'target_project', 'constraints')}
        proposal['allowlist'] = copy.deepcopy(op.get('allowlist', []))
        proposal['provenance'] = op.get('provenance', 'machine_interpreted')
        proposal['implementation'] = copy.deepcopy(op.get('implementation'))
        _proposal(proposal)
        _sources(op['sources'])
        if ident in data['demands']:
            demand = data['demands'][ident]
            known_proposals = [demand['proposal']] + [item['proposal'] for item in demand['history'] if 'proposal' in item]
            if proposal not in known_proposals or not set(map(_digest, op['sources'])) <= set(map(_digest, demand['sources'])):
                raise DemandError('same demand ID has different proposal/source content; revise or append explicitly')
            return False
        for source in op['sources']:
            event = source.get('event_id')
            if event and any(event == item.get('event_id') for d in data['demands'].values() for item in d['sources']):
                raise DemandError('source event already belongs to another demand')
        data['demands'][ident] = {'id': ident, 'proposal': proposal, 'proposal_sha256': _digest(proposal),
                                  'sources': copy.deepcopy(op['sources']), 'analysis': None, 'decision': None,
                                  'handoff': None, 'execution': None, 'authorization_stale': False,
                                  'feedback': {'technical': None, 'usage': None},
                                  'closure': None, 'history': [], 'created_at': at, 'updated_at': at}
        return True
    ident = _id(op.get('id'))
    demand = _get(data, ident)
    if demand['closure'] is not None and kind not in ('append_sources', 'close'):
        raise DemandError('closed demand cannot be changed without an explicit new demand')
    if kind == 'append_sources':
        _fields(op, {'op', 'id', 'sources'})
        return _merge_sources(demand, op['sources'], data)
    if kind == 'assess':
        _fields(op, {'op', 'id', 'analysis'})
        _analysis(op['analysis'])
        if demand['analysis'] == op['analysis']:
            return False
        if (demand['decision'] is not None or demand['handoff'] is not None) and not demand['authorization_stale']:
            old_disposition = demand['analysis']['disposition'] if demand['analysis'] is not None else None
            if old_disposition != op['analysis']['disposition']:
                raise DemandError('disposition cannot change after decision or handoff; revise scope explicitly')
        if demand['analysis'] is not None:
            demand['history'].append({'type': 'analysis_replaced', 'analysis': copy.deepcopy(demand['analysis']),
                                      'proposal_sha256': demand['proposal_sha256'], 'at': at})
        demand['analysis'] = copy.deepcopy(op['analysis'])
        return True
    if kind in ('revise_proposal', 'revise'):
        if kind == 'revise':
            _fields(op, {'op', 'id', 'changes', 'sources', 'supersedes_event_id', 'correction_evidence'})
            _id(op['supersedes_event_id'], 'supersedes_event_id')
            if not any(source.get('event_id') == op['supersedes_event_id'] for source in demand['sources']):
                raise DemandError('superseded event is not linked to this demand')
            _local_evidence(op['correction_evidence'])
            _sources(op['sources'])
            old_ids = {source.get('event_id') for source in demand['sources']}
            if not any(source.get('event_id') and source['event_id'] not in old_ids for source in op['sources']):
                raise DemandError('correction requires a new source event ID')
        else:
            _fields(op, {'op', 'id', 'changes'})
        edits = op['changes']
        if not isinstance(edits, dict) or not edits or set(edits) - {'title', 'outcome', 'kind', 'target_project', 'constraints', 'allowlist', 'provenance', 'implementation'}:
            raise DemandError('invalid proposal changes')
        revised = copy.deepcopy(demand['proposal'])
        revised.update(copy.deepcopy(edits))
        _proposal(revised)
        if revised == demand['proposal']:
            if kind == 'revise':
                return _merge_sources(demand, op['sources'], data)
            return False
        if kind == 'revise':
            _merge_sources(demand, op['sources'], data)
        demand['history'].append({'type': 'proposal_revised', 'proposal': copy.deepcopy(demand['proposal']),
                                  'proposal_sha256': demand['proposal_sha256'],
                                  'analysis': copy.deepcopy(demand['analysis']),
                                  'decision': copy.deepcopy(demand['decision']), 'handoff': copy.deepcopy(demand['handoff']),
                                  'execution': copy.deepcopy(demand['execution']),
                                  'feedback': copy.deepcopy(demand['feedback']), 'closure': copy.deepcopy(demand['closure']),
                                  'at': at, 'correction': {'supersedes_event_id': op['supersedes_event_id'],
                                                          'evidence': copy.deepcopy(op['correction_evidence'])} if kind == 'revise' else None})
        demand['proposal'] = revised
        demand['proposal_sha256'] = _digest(revised)
        demand['analysis'] = None
        demand['decision'] = None
        if demand['execution'] is None:
            demand['handoff'] = None
        else:
            demand['authorization_stale'] = True
        demand['feedback'] = {'technical': None, 'usage': None}
        demand['closure'] = None
        return True
    if kind == 'decide':
        _fields(op, {'op', 'id', 'decision', 'proposal_sha256', 'evidence'})
        if op['decision'] not in ('approved', 'rejected', 'deferred'):
            raise DemandError('invalid decision')
        if op['decision'] == 'approved' and (demand['analysis'] is None or demand['analysis']['disposition'] != 'needs_upgrade'):
            raise DemandError('approval applies only to assessed needs_upgrade proposals')
        if op['proposal_sha256'] != demand['proposal_sha256']:
            raise DemandError('decision applies to an old or different proposal')
        _local_evidence(op['evidence'])
        previous = demand['decision']
        new_core = {'decision': op['decision'], 'proposal_sha256': op['proposal_sha256'], 'evidence': copy.deepcopy(op['evidence'])}
        if previous and {k: previous[k] for k in new_core} == new_core:
            return False
        if previous and previous['decision'] == 'approved' and demand['handoff'] is not None:
            raise DemandError('prepared handoff must be reconciled before changing decision')
        if previous is not None:
            demand['history'].append({'type': 'decision_replaced', 'decision': copy.deepcopy(previous),
                                      'proposal_sha256': demand['proposal_sha256'], 'at': at})
        demand['decision'] = new_core | {'at': at}
        return True
    if kind == 'link_execution':
        _fields(op, {'op', 'id', 'handoff_id', 'thread_id', 'goal_id', 'goal_status', 'plan_path', 'plan_id'})
        if demand['authorization_stale']:
            raise DemandError('old execution is still linked to stale authorization; sync and close it before new link')
        handoff = demand['handoff']
        if handoff is None or op['handoff_id'] != handoff['id']:
            raise DemandError('execution requires the exact prepared handoff')
        _text(op['thread_id'], 'real thread_id', 200)
        if op['goal_status'] not in ('linked', 'not_created', 'unknown'):
            raise DemandError('goal_status must be linked, not_created, or unknown')
        if op['goal_status'] == 'linked':
            _text(op['goal_id'], 'real goal_id', 200)
        elif op['goal_id'] is not None:
            raise DemandError('goal_id must be null without a linked goal')
        plan_path = Path(_text(op['plan_path'], 'plan_path', 2000))
        if not plan_path.is_absolute():
            raise DemandError('plan_path must be absolute')
        if demand['proposal']['implementation'] is None or str(plan_path) != demand['proposal']['implementation']['plan_path']:
            raise DemandError('execution plan_path differs from approved implementation')
        _id(op['plan_id'], 'plan_id')
        core = {k: copy.deepcopy(op[k]) for k in ('handoff_id', 'thread_id', 'goal_id', 'goal_status', 'plan_path', 'plan_id')}
        previous = demand['execution']
        if previous is not None:
            if {k: previous[k] for k in core} != core:
                raise DemandError('execution link already exists; no silent replacement')
            return False
        demand['execution'] = core | {'observation': None, 'linked_at': at}
        return True
    if kind == 'feedback':
        _fields(op, {'op', 'id', 'dimension', 'outcome', 'summary', 'evidence'})
        if demand['authorization_stale']:
            raise DemandError('old execution is still linked; reconcile before new-scope feedback')
        if op['dimension'] not in ('technical', 'usage') or op['outcome'] not in ('met', 'not_met', 'insufficient_evidence'):
            raise DemandError('invalid feedback dimension/outcome')
        _text(op['summary'], 'feedback summary')
        _sources(op['evidence'])
        if op['outcome'] == 'met' and not op['evidence']:
            raise DemandError('met feedback needs evidence')
        core = {k: copy.deepcopy(op[k]) for k in ('outcome', 'summary', 'evidence')}
        previous = demand['feedback'][op['dimension']]
        if previous and {k: previous[k] for k in core} == core:
            return False
        if previous is not None:
            demand['history'].append({'type': 'feedback_replaced', 'dimension': op['dimension'],
                                      'feedback': copy.deepcopy(previous),
                                      'proposal_sha256': demand['proposal_sha256'], 'at': at})
        demand['feedback'][op['dimension']] = core | {'at': at}
        return True
    if kind == 'close':
        _fields(op, {'op', 'id', 'resolution'})
        if op['resolution'] != 'completed':
            raise DemandError('only completed closure is supported')
        if demand['closure'] is not None:
            return False
        if demand['authorization_stale']:
            raise DemandError('stale authorization cannot close a revised demand')
        disposition = demand['analysis']['disposition'] if demand['analysis'] is not None else None
        upgraded = disposition == 'needs_upgrade' and demand['decision'] and demand['decision']['decision'] == 'approved' and demand['decision']['proposal_sha256'] == demand['proposal_sha256']
        existing = disposition == 'use_existing' and demand['proposal']['implementation'] is None and demand['handoff'] is None and demand['execution'] is None
        if not (upgraded or existing):
            raise DemandError('closure requires current approved upgrade or assessed use_existing without new scope')
        if any(not demand['feedback'][dimension] or demand['feedback'][dimension]['outcome'] != 'met' for dimension in ('technical', 'usage')):
            raise DemandError('closure requires independent met technical and usage evidence')
        demand['closure'] = {'resolution': 'completed', 'at': at}
        return True
    raise DemandError(f'unknown operation: {kind}')


def apply(path, request):
    """CAS apply under an advisory file lock; preserve last good ledger as .bak."""
    path = _path(path)
    if not path.is_file():
        raise DemandError('demand ledger unavailable; no implicit initialization')
    _fields(request, {'expected_revision', 'reason', 'source_ref', 'operations'})
    if type(request['expected_revision']) is not int or request['expected_revision'] < 1:
        raise DemandError('expected_revision must be a positive integer')
    _text(request['reason'], 'reason')
    _text(request['source_ref'], 'source_ref')
    if not isinstance(request['operations'], list) or not request['operations'] or len(request['operations']) > MAX_ITEMS:
        raise DemandError('operations must be a nonempty bounded list')
    with _lock_path(path).open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current, raw = _load(path)
        if request['expected_revision'] != current['revision']:
            raise DemandError(f'revision conflict: current revision {current["revision"]}')
        changed = copy.deepcopy(current)
        at = _now()
        summary = []
        for op in request['operations']:
            if _operation(changed, op, at):
                changed['demands'][op['id']]['updated_at'] = at
                summary.append(op['op'])
        if not summary:
            return {'saved': False, 'revision': current['revision'], 'summary': []}
        changed['revision'] += 1
        changed['updated_at'] = at
        changed['changes'].append({'revision': changed['revision'], 'reason': request['reason'], 'source_ref': request['source_ref'], 'operations': summary, 'at': at})
        _validate(changed)
        if path.read_bytes() != raw:
            raise DemandError('ledger changed outside lock; refusing overwrite')
        _atomic(_backup_path(path), current)
        _atomic(path, changed)
        return {'saved': True, 'revision': changed['revision'], 'summary': summary}


def _handoff_content(demand):
    proposal = demand['proposal']
    if demand['decision'] is None or demand['decision']['decision'] != 'approved' or demand['decision']['proposal_sha256'] != demand['proposal_sha256']:
        raise DemandError('handoff requires approval for exact current proposal')
    _local_evidence(demand['decision']['evidence'])
    if proposal['target_project'] is None or not Path(proposal['target_project']['path']).is_absolute() or not proposal['allowlist']:
        raise DemandError('handoff needs absolute target project and nonempty relative allowlist')
    if proposal['implementation'] is None:
        raise DemandError('handoff needs an approved implementation with change, acceptance, stop conditions, and exact plan path')
    handoff_id = 'handoff-' + hashlib.sha256((demand['id'] + ':' + demand['proposal_sha256']).encode()).hexdigest()[:24]
    return {'schema': 'demand-handoff/v1', 'id': handoff_id, 'demand_id': demand['id'],
            'proposal_sha256': demand['proposal_sha256'], 'target_project': copy.deepcopy(proposal['target_project']),
            'allowlist': copy.deepcopy(proposal['allowlist']), 'outcome': proposal['outcome'],
            'constraints': copy.deepcopy(proposal['constraints']), 'implementation': copy.deepcopy(proposal['implementation']),
            'sources': copy.deepcopy(demand['sources']),
            'approval': copy.deepcopy(demand['decision'])}


def prepare_handoff(path, ident):
    """Persist one stable approved handoff intent; never create a platform task."""
    path = _path(path)
    with _lock_path(path).open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data, raw = _load(path)
        demand = _get(data, ident)
        content = _handoff_content(demand)
        out = path.parent / 'handoffs' / (content['id'] + '.json')
        digest = hashlib.sha256(_encoded(content)).hexdigest()
        if demand['authorization_stale']:
            old = demand['execution']
            previous = old['observation']
            current = _plan_observation(old, _execution_target(demand))
            if not previous or previous['freshness'] != 'fresh' or previous['execution_status'] != 'closed' or current['freshness'] != 'fresh' or current['execution_status'] != 'closed' or current['raw_sha256'] != previous['raw_sha256']:
                raise DemandError('old execution is not observed closed at the same published plan version; sync and reconcile before new handoff')
        elif demand['handoff'] is not None:
            if demand['handoff']['id'] != content['id'] or demand['handoff']['sha256'] != digest:
                raise DemandError('existing handoff conflicts with current proposal')
            if not out.is_file() or hashlib.sha256(out.read_bytes()).hexdigest() != digest:
                raise DemandError('handoff artifact missing or changed; reconcile before retry')
            return {'saved': False, 'handoff_id': content['id'], 'path': str(out), 'sha256': digest, 'revision': data['revision']}
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if out.exists() and hashlib.sha256(out.read_bytes()).hexdigest() != digest:
            raise DemandError('stable handoff path contains different content')
        if not out.exists():
            _atomic(out, content)
        changed = copy.deepcopy(data)
        if demand['authorization_stale']:
            # Preserve the final synced observation, which may have changed
            # after the proposal-revision snapshot was taken.
            changed['demands'][ident]['history'].append({
                'type': 'execution_reconciled', 'handoff': copy.deepcopy(demand['handoff']),
                'execution': copy.deepcopy(demand['execution']), 'at': _now()})
            changed['demands'][ident]['execution'] = None
            changed['demands'][ident]['handoff'] = None
            changed['demands'][ident]['authorization_stale'] = False
        changed['demands'][ident]['handoff'] = {'id': content['id'], 'path': str(out), 'sha256': digest,
                                                  'proposal_sha256': demand['proposal_sha256'], 'prepared_at': _now()}
        changed['revision'] += 1
        changed['updated_at'] = _now()
        changed['changes'].append({'revision': changed['revision'], 'reason': 'prepared approved handoff',
                                   'source_ref': content['id'], 'operations': ['prepare_handoff'], 'at': changed['updated_at']})
        _validate(changed)
        if path.read_bytes() != raw:
            raise DemandError('ledger changed outside lock')
        _atomic(_backup_path(path), data)
        _atomic(path, changed)
        return {'saved': True, 'handoff_id': content['id'], 'path': str(out), 'sha256': digest, 'revision': changed['revision']}


def _load_json(path):
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=_unique)
    except (OSError, ValueError) as exc:
        raise DemandError(f'artifact unreadable: {exc}') from exc


def sync(path=DEFAULT_DATA):
    """Observe linked published plans, retaining last reliable state when offline."""
    path = _path(path)
    with _lock_path(path).open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data, raw = _load(path)
        changed = copy.deepcopy(data)
        count = 0
        for demand in changed['demands'].values():
            execution = demand['execution']
            if execution is None:
                continue
            observed = _plan_observation(execution, _execution_target(demand))
            if execution['observation'] == observed:
                continue
            # A fresh poll of the same source version does not create a new revision.
            previous = execution['observation']
            if previous and observed['freshness'] == previous['freshness'] == 'fresh' and observed['raw_sha256'] == previous['raw_sha256'] and observed['source_path'] == previous.get('source_path'):
                continue
            if previous and observed['freshness'] == previous['freshness'] == 'stale' and observed['error'] == previous['error']:
                continue
            execution['observation'] = observed
            demand['updated_at'] = _now()
            count += 1
        if not count:
            return {'saved': False, 'revision': data['revision'], 'observed': 0}
        changed['revision'] += 1
        changed['updated_at'] = _now()
        changed['changes'].append({'revision': changed['revision'], 'reason': 'observed linked ExecPlan',
                                   'source_ref': 'sync', 'operations': ['sync'], 'at': changed['updated_at']})
        _validate(changed)
        if path.read_bytes() != raw:
            raise DemandError('ledger changed outside lock')
        _atomic(_backup_path(path), data)
        _atomic(path, changed)
        return {'saved': True, 'revision': changed['revision'], 'observed': count}


def recover(path=DEFAULT_DATA):
    """Explicitly restore the last valid backup, preserving corrupt primary bytes."""
    path = _path(path)
    if not path.parent.is_dir():
        raise DemandError('ledger directory unavailable')
    with _lock_path(path).open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            try:
                _load(path)
            except DemandError:
                pass
            else:
                raise DemandError('primary ledger is valid; refusing recovery over newer facts')
        backup, _ = _load(_backup_path(path))
        if path.exists():
            raw = path.read_bytes()
            preserved = path.with_name(path.name + '.pre-recover-' + hashlib.sha256(raw).hexdigest()[:16])
            if preserved.exists() and preserved.read_bytes() != raw:
                raise DemandError('pre-recover preservation collision')
            if not preserved.exists():
                _atomic_bytes(preserved, raw)
        _atomic(path, backup)
        return {'revision': backup['revision'], 'warning': 'Restored last valid backup; most recent changes may need replay'}


def _atomic_bytes(path, raw):
    name = None
    try:
        with tempfile.NamedTemporaryFile(prefix=path.name + '.tmp-', dir=path.parent, delete=False) as handle:
            name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        name = None
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name is not None:
            os.unlink(name)


def _phone(data):
    items = list(data['demands'].values())
    fresh = [x for x in items if x['decision'] is None]
    pending = [x for x in items if x['decision'] and x['decision']['decision'] == 'deferred']
    doing = [x for x in items if x['execution'] is not None and x['closure'] is None]
    technical = [x for x in items if x['feedback']['technical'] is not None]
    unfinished = [x for x in items if x['decision'] and x['decision']['decision'] == 'approved' and x['closure'] is None]
    def line(label, values):
        return f'{label}（{len(values)}）: ' + ('、'.join(x['id'] for x in values[:8]) or '无')
    return '\n'.join([f'需求账本 revision {data["revision"]}', line('新增', fresh), line('待决', pending),
                      line('正在执行', doing), line('技术结果', technical), line('未完验收', unfinished)])


def check(path=DEFAULT_DATA):
    """Check current approvals and prepared artifact bytes as well as ledger shape."""
    data = read(path)
    for demand in data['demands'].values():
        if demand['decision'] is not None:
            _local_evidence(demand['decision']['evidence'])
        handoff = demand['handoff']
        if handoff is not None:
            artifact = Path(handoff['path'])
            if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != handoff['sha256']:
                raise DemandError(f'handoff artifact unavailable or changed: {handoff["id"]}')
    return {'valid': True, 'revision': data['revision'], 'demands': len(data['demands'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--json', action='store_true', help='Machine-readable output (place before subcommand)')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'show', 'check', 'sync', 'recover'):
        sub.add_parser(name)
    apply_parser = sub.add_parser('apply')
    apply_parser.add_argument('--request', type=Path, required=True)
    handoff_parser = sub.add_parser('prepare-handoff')
    handoff_parser.add_argument('--id', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            value = initialize(args.data)
        elif args.command == 'show':
            value = read(args.data)
        elif args.command == 'check':
            value = check(args.data)
        elif args.command == 'apply':
            value = apply(args.data, _load_json(args.request))
        elif args.command == 'prepare-handoff':
            value = prepare_handoff(args.data, args.id)
        elif args.command == 'sync':
            value = sync(args.data)
        else:
            value = recover(args.data)
        if args.json or args.command not in ('show',):
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(_phone(value))
        return 0
    except (DemandError, OSError) as exc:
        parser.exit(1, f'需求账本错误：{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
