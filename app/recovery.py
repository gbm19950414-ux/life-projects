"""Explicitly recover a corrupt or missing ledger from the last valid backup."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from uuid import uuid4

from .model import validate_workspace, validate_workspace_v1
from .store import StorageError, WorkspaceStore


def _validate_any(workspace):
    if not isinstance(workspace, dict):
        raise ValueError("备份不是账本对象")
    if workspace.get("schema_version") == 1:
        validate_workspace_v1(workspace)
    else:
        validate_workspace(workspace)


def restore_backup(path):
    target = Path(path).expanduser().resolve()
    backup = target.with_name(target.name + ".bak")
    lock = target.with_name(target.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StorageError("服务仍在使用账本；请先停止服务") from exc
        if not backup.exists():
            raise StorageError("没有可恢复的备份")
        try:
            restored = json.loads(backup.read_text(encoding="utf-8"))
            _validate_any(restored)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise StorageError(f"备份无法校验：{exc}") from exc
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                _validate_any(existing)
            except (OSError, ValueError, TypeError, KeyError):
                pass
            else:
                raise StorageError("当前账本仍有效；拒绝用较旧备份覆盖")
            old = target.read_bytes()
            name = target.with_name(target.name + ".corrupt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
            with os.fdopen(os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
                stream.write(old)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            name = None
        WorkspaceStore._write_atomically(target, restored)
        return restored["revision"], name
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description="restore a corrupt or missing life-projects ledger")
    parser.add_argument("--data", default="data/workspace.json")
    args = parser.parse_args()
    try:
        revision, preserved = restore_backup(args.data)
    except (StorageError, OSError) as exc:
        parser.exit(1, f"恢复失败：{exc}\n")
    print(f"已恢复备份 revision {revision}。原始损坏文件：{preserved or '原文件缺失'}")


if __name__ == "__main__":
    main()
