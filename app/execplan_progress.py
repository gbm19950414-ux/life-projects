"""execplan-progress/v1: portable stdlib validator and explicit publisher.

This file is also vendored by consumers; no personal skill path is needed at runtime.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

START = '<!-- execplan-progress:start -->'
END = '<!-- execplan-progress:end -->'
SUMMARY_START = '<!-- execplan-summary:start -->'
SUMMARY_END = '<!-- execplan-summary:end -->'
SCHEMA = 'execplan-progress/v1'
STATES = {'todo', 'doing', 'blocked', 'done', 'cancelled', 'unknown'}
LABELS = dict(todo='待做', doing='进行中', blocked='阻塞', done='完成', cancelled='取消', unknown='未知')


class ProgressError(ValueError):
    pass


def _text(value, name, blank=False):
    if not isinstance(value, str) or len(value) > 10000 or (not blank and not value.strip()):
        raise ProgressError(f'{name}: expected text')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProgressError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def validate(record):
    required = {'schema', 'plan_id', 'title', 'objective', 'execution_status', 'assessment', 'milestones', 'next_action', 'updated_at', 'published_sha256'}
    if not isinstance(record, dict) or not required <= record.keys() or record.keys() - required - {'project_id', 'goal_id', 'gap', 'authority_ref'}:
        raise ProgressError('invalid progress fields')
    if record['schema'] != SCHEMA:
        raise ProgressError('unsupported progress schema')
    for key in ('plan_id', 'title', 'objective', 'next_action', 'updated_at'):
        _text(record[key], key)
    for key in ('project_id', 'goal_id', 'gap', 'authority_ref'):
        if key in record:
            _text(record[key], key, blank=key == 'gap')
    if record['execution_status'] not in ('planned', 'active', 'paused', 'blocked', 'closed', 'unknown'):
        raise ProgressError('invalid execution_status')
    try:
        if datetime.fromisoformat(record['updated_at'].replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError):
        raise ProgressError('updated_at must include a timezone') from None
    digest = record['published_sha256']
    if not isinstance(digest, str) or not re.fullmatch(r'(?:[0-9a-f]{64})?', digest):
        raise ProgressError('invalid published_sha256')
    assessment = record['assessment']
    if not isinstance(assessment, dict) or set(assessment) != {'outcome', 'summary', 'evidence'}:
        raise ProgressError('invalid assessment')
    if assessment['outcome'] not in ('unassessed', 'met', 'not_met', 'insufficient_evidence'):
        raise ProgressError('invalid assessment outcome')
    _text(assessment['summary'], 'assessment summary')
    def evidence(value):
        if not isinstance(value, list) or len(value) > 1000:
            raise ProgressError('evidence must be a bounded list')
        for entry in value:
            _text(entry, 'evidence')
    evidence(assessment['evidence'])
    if assessment['outcome'] == 'met' and not assessment['evidence']:
        raise ProgressError('met assessment needs evidence; publication is not acceptance')
    milestones = record['milestones']
    if not isinstance(milestones, list) or len(milestones) > 1000:
        raise ProgressError('milestones must be a bounded list')
    ids = set()
    for item in milestones:
        if not isinstance(item, dict) or set(item) != {'id', 'title', 'state', 'depends_on', 'evidence'}:
            raise ProgressError('invalid milestone fields')
        for key in ('id', 'title'):
            _text(item[key], key)
        if item['id'] in ids:
            raise ProgressError('duplicate milestone ID')
        ids.add(item['id'])
        if item['state'] not in tuple(STATES):
            raise ProgressError('invalid milestone state')
        evidence(item['evidence'])
        deps = item['depends_on']
        if not isinstance(deps, list) or any(not isinstance(x, str) for x in deps) or len(set(deps)) != len(deps):
            raise ProgressError('invalid dependencies')
    remaining = {m['id']: set(m['depends_on']) for m in milestones}
    if any(key in deps or not deps <= ids for key, deps in remaining.items()):
        raise ProgressError('unknown/self dependency')
    while remaining:
        ready = {key for key, deps in remaining.items() if not deps}
        if not ready:
            raise ProgressError('dependency cycle')
        remaining = {key: deps - ready for key, deps in remaining.items() if key not in ready}
    return record


def parse(text):
    if START not in text and END not in text:
        return None
    if text.count(START) != 1 or text.count(END) != 1:
        raise ProgressError('expected one progress block')
    section = re.search(r'^## Progress\s*\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    if not section or START not in section[1] or END not in section[1]:
        raise ProgressError('progress block must be inside Progress section')
    block = text.split(START, 1)[1].split(END, 1)[0]
    match = re.fullmatch(r'\s*```json\s*\n(.*?)\n```\s*', block, re.S)
    if not match:
        raise ProgressError('expected fenced JSON')
    try:
        return validate(json.loads(match[1], object_pairs_hook=_unique))
    except (ValueError, TypeError, KeyError) as exc:
        raise ProgressError(str(exc)) from exc


def fingerprint(text):
    """Hash exact UTF-8 document bytes, replacing only its digest value with ''."""
    start, end = text.index(START), text.index(END)
    block = text[start:end]
    pattern = r'("published_sha256"\s*:\s*")[0-9a-f]*(")'
    if len(re.findall(pattern, block)) != 1:
        raise ProgressError('expected exactly one fingerprint field')
    normalized = text[:start] + re.sub(pattern, r'\1\2', block) + text[end:]
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def summary(record):
    lines = [SUMMARY_START, '<!-- Generated from the JSON above; edit the JSON, not these checkboxes. -->']
    for item in record['milestones']:
        title = ' '.join(item['title'].splitlines())
        lines.append(f"- [{'x' if item['state'] == 'done' else ' '}] ({record['updated_at']}) {item['id']} · {title} [{LABELS[item['state']]}]")
    lines.append(SUMMARY_END)
    return '\n'.join(lines)


def block(record):
    return START + '\n```json\n' + json.dumps(record, ensure_ascii=False, indent=2) + '\n```\n' + END


def publication(text):
    record = parse(text)
    if record is None:
        raise ProgressError('legacy plan: migrate explicitly before publishing')
    record['published_sha256'] = ''
    result = text[:text.index(START)] + block(record) + text[text.index(END) + len(END):]
    if SUMMARY_START in result or SUMMARY_END in result:
        if result.count(SUMMARY_START) != 1 or result.count(SUMMARY_END) != 1 or result.index(SUMMARY_END) < result.index(SUMMARY_START):
            raise ProgressError('invalid generated summary markers')
        result = result[:result.index(SUMMARY_START)] + summary(record) + result[result.index(SUMMARY_END) + len(SUMMARY_END):]
    else:
        result = result.replace(END, END + '\n\n' + summary(record), 1)
    digest = fingerprint(result)
    return result.replace('"published_sha256": ""', f'"published_sha256": "{digest}"', 1)


def inspect(text):
    record = parse(text)
    if record is None:
        return {'status': 'legacy', 'record': None, 'source_version': hashlib.sha256(text.encode()).hexdigest()}
    current = fingerprint(text)
    valid = current == record['published_sha256'] and summary(record) in text
    return {'status': 'synced' if valid else 'pending', 'record': record, 'source_version': current}


def publish(path, expected_sha256=None):
    path = Path(path)
    if path.is_symlink():
        raise ProgressError('publish the real plan path, not a symlink')
    with path.with_suffix(path.suffix + '.publish.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = path.read_bytes()
        if expected_sha256 and hashlib.sha256(original).hexdigest() != expected_sha256:
            raise ProgressError('source changed; reread before publishing')
        updated = publication(original.decode('utf-8')).encode('utf-8')
        if updated == original:
            return inspect(updated.decode())
        fd, temp = tempfile.mkstemp(prefix='.execplan-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, path.stat().st_mode & 0o777)
            if path.read_bytes() != original:
                raise ProgressError('concurrent edit; publication aborted')
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return inspect(updated.decode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'publish'])
    parser.add_argument('plan', type=Path)
    parser.add_argument('--expected-sha256', help='Raw preimage SHA-256 for optimistic publication')
    args = parser.parse_args()
    try:
        result = publish(args.plan, args.expected_sha256) if args.action == 'publish' else inspect(args.plan.read_bytes().decode('utf-8'))
        print(json.dumps({k: v for k, v in result.items() if k != 'record'}, ensure_ascii=False))
        return 2 if result['status'] == 'pending' else 0
    except (OSError, ValueError) as exc:
        parser.exit(1, f'ExecPlan 未发布：{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
