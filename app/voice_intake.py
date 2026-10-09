"""Source-verified voice delivery into the demand ledger; never executes projects."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from . import demands
from .store import WorkspaceStore

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / 'private' / 'demand-lifecycle'
ID = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    value = copy.deepcopy(value)
    for source in value.get('sources', []):
        source.pop('audio_path', None)
    return value


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def validate_item(item):
    if item.get('project_id') != 'life-projects' or item.get('operation') != 'demand':
        raise ValueError('unsupported_life_capability')
    event_id = item.get('event_id', '')
    if not isinstance(event_id, str) or not ID.fullmatch(event_id):
        raise ValueError('invalid_event_id')
    if item.get('nature') in {'test', 'unintelligible'}:
        raise ValueError('non_actionable_source')
    if not isinstance(item.get('quote'), str) or not item['quote'].strip():
        raise ValueError('missing_literal_quote')
    if any(key in item for key in ('human_confirmed', 'status', 'confirmation_evidence')):
        raise ValueError('intake_is_not_owner_approval')
    sources = item.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('missing_source')
    for source in sources:
        path = Path(source['path'])
        if not path.is_absolute() or sha(path) != source.get('sha256'):
            raise ValueError('immutable_source_hash_mismatch')
        raw = load(path)
        text = raw.get('response', {}).get('text')
        if (raw.get('status') != 'success' or source.get('locator') != '/response/text'
                or raw.get('source_sha256') != source.get('audio_sha256')
                or text != source.get('raw_text') or item['quote'] not in text):
            raise ValueError('quote_or_audio_not_bound_to_source')
    proposal = item.get('demand')
    if not isinstance(proposal, dict):
        raise ValueError('missing_curated_demand')
    for key in ('title', 'outcome', 'kind'):
        if not isinstance(proposal.get(key), str) or not proposal[key].strip():
            raise ValueError('missing_demand_' + key)
    if proposal['kind'] not in {'action', 'learning', 'project_upgrade', 'question'}:
        raise ValueError('unsupported_demand_kind')
    if not isinstance(proposal.get('constraints', []), list):
        raise ValueError('invalid_constraints')
    return event_id, proposal


def source_refs(item):
    digest = hashlib.sha256(json.dumps(canonical(item), ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    return [{'kind': 'voice', 'ref': s['path'], 'sha256': s['sha256'],
             'event_id': item['event_id'], 'audio_sha256': s['audio_sha256'],
             'recording_id': s.get('recording_id'), 'quote': item['quote'],
             'delivery_sha256': digest}
            for s in item['sources']]


def immutable_json(path, value):
    if path.exists():
        if load(path) != value:
            raise ValueError('immutable_receipt_conflict: ' + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    WorkspaceStore._write_atomically(path, value)


def correction(root, item):
    old_id = item['supersedes_event_id']
    if not isinstance(old_id, str) or not ID.fullmatch(old_id):
        raise ValueError('invalid_superseded_event')
    prior_path = root / 'intake' / (old_id + '.json')
    prior = load(prior_path)
    manifest = load(root / 'receipts' / (old_id + '.json'))
    state = demands.read(root / 'demands.json')
    old_refs = [s for s in state['demands'].get(prior['demand_id'], {}).get('sources', [])
                if s.get('event_id') == old_id]
    if (prior.get('event_id') != old_id or prior['item'].get('event_id') != old_id
            or manifest.get('event_id') != old_id or manifest.get('demand_id') != prior['demand_id']
            or manifest.get('artifact') != {'path': str(prior_path.relative_to(root)), 'sha256': sha(prior_path)}
            or old_refs != source_refs(prior['item'])):
        raise ValueError('superseded_receipt_or_ledger_mismatch')
    evidence = item.get('correction_evidence')
    if not isinstance(evidence, dict) or sha(evidence['path']) != evidence.get('sha256'):
        raise ValueError('missing_or_changed_correction_evidence')
    owner = load(evidence['path'])
    if (owner.get('kind') != 'user_message' or not owner.get('text')
            or not owner.get('message_ref') or not owner.get('known_at')
            or owner.get('supersedes_event_id', owner.get('event_id')) != old_id):
        raise ValueError('correction_not_bound_to_old_event')
    if owner.get('demand_confirmation') != item.get('demand'):
        raise ValueError('correction_not_bound_to_exact_content')
    return prior['demand_id']


def receive_one(root, item):
    event_id, proposal = validate_item(item)
    path = root / 'intake' / (event_id + '.json')
    published = root / 'receipts' / (event_id + '.json')
    duplicate = path.exists() and published.exists()
    frozen = load(path) if path.exists() else None
    if frozen is not None and canonical(frozen['item']) != canonical(item):
        raise ValueError('same_event_changed_content')
    demand_id = ('demand-' + hashlib.sha256(event_id.encode()).hexdigest()[:24])
    if item.get('supersedes_event_id'):
        demand_id = correction(root, item)
    refs = source_refs(item)
    value = {k: proposal[k] for k in ('title', 'outcome', 'kind')}
    value.update(target_project=proposal.get('target_project'), constraints=proposal.get('constraints', []))
    ledger_path = root / 'demands.json'
    state = demands.read(ledger_path)
    if item.get('supersedes_event_id'):
        evidence = item['correction_evidence']
        operations = [dict(op='revise', id=demand_id, changes=value, sources=refs,
                           supersedes_event_id=item['supersedes_event_id'],
                           correction_evidence={'kind': 'user_message', 'ref': evidence['path'],
                                                'sha256': evidence['sha256']})]
    else:
        operations = [dict(op='propose', id=demand_id, sources=refs,
                           provenance='machine_interpreted', **value)]
    # A retry repairs a ledger commit followed by an interrupted publication.
    # Existing published items are not reinterpreted using later demand state.
    seen = state['demands'].get(demand_id, {})
    prior_refs = [s for s in seen.get('sources', []) if s.get('event_id') == event_id]
    already_recorded = bool(prior_refs)
    if already_recorded and prior_refs != refs:
        raise ValueError('same_event_changed_content_after_ledger_commit')
    if not already_recorded:
        demands.apply(ledger_path, {'expected_revision': state['revision'],
                      'reason': '接收来源已核验的语音需求；不代表采用或实施批准',
                      'source_ref': refs[0]['ref'], 'operations': operations})
    artifact = frozen or {'schema_version': 'life-voice-intake/v1', 'event_id': event_id,
                          'demand_id': demand_id, 'item': item}
    immutable_json(path, artifact)
    artifact_ref = {'path': str(path.relative_to(root)), 'sha256': sha(path)}
    manifest = {'schema_version': 'voice-delivery-manifest/v2', 'operation': 'voice_intake',
                'event_id': event_id, 'demand_id': demand_id, 'artifact': artifact_ref}
    immutable_json(published, manifest)
    return {'status': 'duplicate' if duplicate else 'accepted', 'archive_status': 'archived',
            'event_id': event_id, 'demand_id': demand_id,
            'receipt': {'schema_version': 'voice-delivery-receipt/v2', 'event_id': event_id,
                        'artifact': artifact_ref,
                        'manifest': {'path': str(published.relative_to(root)), 'sha256': sha(published)}}}


def receive(root, items):
    root = Path(root).resolve()
    demands.read(root / 'demands.json')  # Missing/offline must never initialize an empty ledger.
    if not isinstance(items, list) or not items:
        raise ValueError('expected_nonempty_item_list')
    results = []
    with (root / '.voice-intake.lock').open('a+b') as lock:
        os.chmod(root / '.voice-intake.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        for item in items:
            try:
                results.append(receive_one(root, item))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                results.append({'status': 'rejected', 'event_id': item.get('event_id') if isinstance(item, dict) else None,
                                'reason': str(exc)})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--json', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)
    receive_parser = sub.add_parser('receive')
    receive_parser.add_argument('--input', type=Path, required=True)
    show = sub.add_parser('show')
    show.add_argument('--issue')
    args = parser.parse_args(argv)
    try:
        if args.command == 'receive':
            result = receive(args.data_root, load(args.input))
        else:
            state = demands.read(args.data_root / 'demands.json')
            result = {'schema_version': 'life-demand-view/v1', 'revision': state['revision'],
                      'demands': {key: val for key, val in state['demands'].items()
                                  if not args.issue or args.issue == key}}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
