"""Single-writer, revisioned JSON storage with explicit backup recovery."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from .model import (ValidationError, derived_view, initial_workspace,
                    migrate_workspace_v1, normalize_and_validate,
                    validate_workspace, validate_workspace_v1)


class ConflictError(Exception):
    def __init__(self, revision):
        self.revision = revision
        super().__init__(f"账本已更新到 revision {revision}")


class StorageError(Exception):
    pass


class WorkspaceStore:
    def __init__(self, path, create=True):
        self.path = Path(path).expanduser().resolve()
        if not create and not self.path.is_file():
            raise StorageError("现有账本不可用；未创建目录或空账本")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup = self.path.with_name(self.path.name + ".bak")
        self.pre_v2 = self.path.with_name(self.path.name + ".pre-v2")
        self.marker = self.path.with_name(self.path.name + ".initialized")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._mutex = threading.RLock()
        self._lock_fd = os.open(self.lock_path, (os.O_CREAT | os.O_RDWR) if create else os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not self.path.exists():
                if not create:
                    raise StorageError("账本已离线；拒绝初始化")
                if self.marker.exists() or self.backup.exists() or self.pre_v2.exists() or list(self.path.parent.glob(self.path.name + ".tmp-*")):
                    raise StorageError("账本缺失但存在初始化记录、备份或临时文件；请检查并显式恢复")
                self._write_atomically(self.path, initial_workspace())
            self._migrate_legacy_if_needed()
            self.read()
            if not self.marker.exists():
                self._write_atomically(self.marker, {"schema_version": 2, "initialized": True})
        except Exception:
            self.close()
            raise

    def close(self):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _write_atomically(target, value):
        data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(prefix=target.name + ".tmp-", dir=target.parent, delete=False) as stream:
                temp_name = stream.name
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
            temp_name = None
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def _migrate_legacy_if_needed(self):
        """Called only while the process owns the ledger's exclusive flock."""
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("schema_version") == 2:
                validate_workspace(data)
                return
            validate_workspace_v1(data)
            upgraded = migrate_workspace_v1(data)
            self._preserve_pre_v2(raw)
            self._write_atomically(self.path, upgraded)
        except StorageError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise StorageError(f"账本迁移或校验失败：{exc}") from exc

    @staticmethod
    def _sync_directory(parent):
        dir_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _preserve_pre_v2(self, raw):
        target = self.pre_v2
        if target.exists():
            if target.read_bytes() == raw:
                return
            # A later explicit restore may select an older v1 backup. Keep both
            # source images instead of replacing the first migration's evidence.
            suffix = hashlib.sha256(raw).hexdigest()[:16]
            target = self.pre_v2.with_name(self.pre_v2.name + "-" + suffix)
            if target.exists():
                if target.read_bytes() != raw:
                    raise StorageError("迁移前像指纹冲突；请检查后显式恢复")
                return
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(prefix=target.name + ".tmp-", dir=target.parent, delete=False) as stream:
                temp_name = stream.name
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp_name, target)
            self._sync_directory(target.parent)
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def read(self):
        with self._mutex:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                validate_workspace(data)
            except (OSError, json.JSONDecodeError, ValidationError, TypeError, KeyError, RecursionError) as exc:
                raise StorageError(f"账本无法读取或校验失败：{exc}") from exc
            return data

    def view(self):
        data = self.read()
        return {"workspace": data, "derived": derived_view(data)}

    def save(self, expected_revision, submitted, reason=""):
        with self._mutex:
            current = self.read()
            if type(expected_revision) is not int or expected_revision != current["revision"]:
                raise ConflictError(current["revision"])
            candidate = normalize_and_validate(current, submitted, reason)
            try:
                self._write_atomically(self.backup, current)
                self._write_atomically(self.path, candidate)
            except OSError as exc:
                raise StorageError(f"保存失败；请检查文件状态：{exc}") from exc
            return {"workspace": candidate, "derived": derived_view(candidate)}
