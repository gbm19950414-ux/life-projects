"""Derived local observations; explicit imports and registered read-only synchronization."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile


class TrackingError(ValueError):
    pass


def empty_tracking():
    return {"schema_version": 1, "revision": 0, "observed_at": "", "sync_status": "empty",
            "sync_message": "尚未导入外部计划", "source_thread": None,
            "projects": [], "goals": [], "relations": []}


def _fields(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise TrackingError(f"{name}字段无效")


def _text(value, name, blank=False):
    if not isinstance(value, str) or len(value) > 10000 or (not blank and not value.strip()):
        raise TrackingError(f"{name}需要文本")


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise TrackingError("观察时间需要带时区的日期时间") from None


def validate_tracking(value):
    _fields(value, "schema_version revision observed_at sync_status sync_message source_thread projects goals relations", "追踪")
    if type(value["schema_version"]) is not int or value["schema_version"] not in {1, 2} or type(value["revision"]) is not int or value["revision"] < 0:
        raise TrackingError("追踪版本无效")
    if not isinstance(value["sync_status"], str) or value["sync_status"] not in {"empty", "ok", "failed"}:
        raise TrackingError("同步状态无效")
    _text(value["sync_message"], "同步说明", blank=True)
    for key in ("projects", "goals", "relations"):
        if not isinstance(value[key], list) or len(value[key]) > 1000:
            raise TrackingError(f"{key}需要有限列表")
    if value["source_thread"] is None:
        if value["projects"] or value["goals"] or value["relations"] or value["observed_at"] or value["sync_status"] == "ok":
            raise TrackingError("有追踪数据必须声明来源对话")
        return value
    thread = value["source_thread"]
    _fields(thread, "id title url status", "来源对话")
    for key in thread:
        _text(thread[key], key)
    if thread["url"] != "codex://threads/" + thread["id"]:
        raise TrackingError("来源对话 URL 与 ID 不一致")
    _time(value["observed_at"])
    projects, goals, plans = set(), set(), set()
    for project in value["projects"]:
        _fields(project, "id title", "外部项目")
        for key in project:
            _text(project[key], key)
        if project["id"] in projects:
            raise TrackingError("外部项目 ID 重复")
        projects.add(project["id"])
    for goal in value["goals"]:
        fields = "id project_id title target_state plan_id plan_ref progress assessment gap next_action sources"
        if value["schema_version"] == 2:
            fields += " stage_status milestones"
        _fields(goal, fields, "外部目标")
        for key in set(goal) - {"sources", "milestones"}:
            if key == "stage_status":
                continue
            _text(goal[key], key)
        if goal["id"] in goals or goal["plan_id"] in plans or goal["project_id"] not in projects:
            raise TrackingError("外部目标身份重复或项目引用无效")
        goals.add(goal["id"])
        plans.add(goal["plan_id"])
        if not isinstance(goal["sources"], list) or not goal["sources"]:
            raise TrackingError("外部目标需要来源")
        source_refs = set()
        for source in goal["sources"]:
            _fields(source, "kind ref title observed_at sha256", "来源")
            if not isinstance(source["kind"], str) or source["kind"] not in {"plan", "thread", "report"}:
                raise TrackingError("来源类型无效")
            for key in ("ref", "title"):
                _text(source[key], key)
            if value["schema_version"] == 2 and source["ref"] in source_refs:
                raise TrackingError("外部目标来源引用重复")
            source_refs.add(source["ref"])
            _time(source["observed_at"])
            if not isinstance(source["sha256"], str) or not (re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) or source["kind"] == "thread" and not source["sha256"]):
                raise TrackingError("文件来源需要 SHA-256 指纹")
        if value["schema_version"] == 2:
            if not isinstance(goal["stage_status"], str) or goal["stage_status"] not in {"planned", "active", "paused", "blocked", "closed", "unknown"}:
                raise TrackingError("外部阶段状态无效")
            milestones = goal["milestones"]
            if not isinstance(milestones, list) or len(milestones) > 1000:
                raise TrackingError("外部里程碑需要有限列表")
            milestone_ids = set()
            for milestone in milestones:
                _fields(milestone, "id title state depends_on source_ref note", "外部里程碑")
                for key in ("id", "title", "source_ref"):
                    _text(milestone[key], key)
                _text(milestone["note"], "里程碑说明", blank=True)
                if milestone["id"] in milestone_ids:
                    raise TrackingError("外部里程碑 ID 重复")
                milestone_ids.add(milestone["id"])
                if not isinstance(milestone["state"], str) or milestone["state"] not in {"todo", "doing", "blocked", "done", "cancelled", "unknown"}:
                    raise TrackingError("外部里程碑状态无效")
                if milestone["source_ref"] not in source_refs:
                    raise TrackingError("外部里程碑来源引用无效")
                if not isinstance(milestone["depends_on"], list) or len(milestone["depends_on"]) > 1000 or any(not isinstance(dep, str) for dep in milestone["depends_on"]) or len(set(milestone["depends_on"])) != len(milestone["depends_on"]):
                    raise TrackingError("外部里程碑前置无效")
            dependencies = {item["id"]: item["depends_on"] for item in milestones}
            for milestone_id, predecessors in dependencies.items():
                if milestone_id in predecessors or any(dep not in milestone_ids for dep in predecessors):
                    raise TrackingError("外部里程碑前置引用无效")
            remaining = {key: set(value) for key, value in dependencies.items()}
            resolved = {key for key, value in remaining.items() if not value}
            while resolved:
                for key in resolved:
                    del remaining[key]
                for predecessors in remaining.values():
                    predecessors.difference_update(resolved)
                resolved = {key for key, value in remaining.items() if not value}
            if remaining:
                raise TrackingError("外部里程碑前置成环")
    seen = set()
    for relation in value["relations"]:
        _fields(relation, "from to kind status note", "外部关系")
        if not isinstance(relation["from"], str) or not isinstance(relation["to"], str) or relation["from"] not in goals or relation["to"] not in goals or relation["from"] == relation["to"]:
            raise TrackingError("外部关系引用无效")
        if not isinstance(relation["kind"], str) or not isinstance(relation["status"], str) or relation["kind"] not in {"follows", "depends_on"} or relation["status"] not in {"confirmed", "proposed"}:
            raise TrackingError("外部关系语义无效")
        _text(relation["note"], "关系说明")
        edge = (relation["from"], relation["to"], relation["kind"])
        if edge in seen:
            raise TrackingError("外部关系重复")
        seen.add(edge)
    return value


def load_tracking(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_tracking()
    except (OSError, ValueError) as exc:
        raise TrackingError(f"追踪快照读取失败：{exc}") from exc
    return validate_tracking(value)


def save_tracking(path, expected_revision, snapshot=None, failure=None):
    """Compare-and-swap local snapshot. Failure preserves the last successful facts."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if type(expected_revision) is not int or expected_revision < 0 or (snapshot is None) == (failure is None):
        raise TrackingError("需要修订号以及快照或失败说明")
    with path.with_suffix(path.suffix + ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_tracking(path)
        if expected_revision != current["revision"]:
            raise TrackingError(f"追踪修订冲突：当前 {current['revision']}")
        if failure is not None:
            _text(failure, "失败说明")
            candidate = copy.deepcopy(current)
            candidate.update(sync_status="failed", sync_message=failure)
        else:
            candidate = copy.deepcopy(validate_tracking(snapshot))
            if candidate["sync_status"] != "ok":
                raise TrackingError("成功导入必须标为 ok")
            # A plan's stable identity survives path moves and renaming.
            previous = {g["plan_id"]: g["id"] for g in current["goals"]}
            for goal in candidate["goals"]:
                if goal["plan_id"] in previous and previous[goal["plan_id"]] != goal["id"]:
                    raise TrackingError("相同计划必须保留目标 ID")
        candidate["revision"] = current["revision"] + 1
        validate_tracking(candidate)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tracking-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(candidate, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return candidate


def main():
    parser = argparse.ArgumentParser(description="Import an explicit read-only plan observation")
    parser.add_argument("--data", default="data/tracking.json")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--import", dest="source")
    action.add_argument("--failure", help="Preserve successful facts and record why sync failed")
    parser.add_argument("--expected-revision", required=True, type=int)
    args = parser.parse_args()
    try:
        snapshot = json.loads(Path(args.source).read_text()) if args.source else None
        result = save_tracking(args.data, args.expected_revision, snapshot, args.failure)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"追踪未更新：{exc}\n")
    print(json.dumps({"revision": result["revision"], "observed_at": result["observed_at"], "sync_status": result["sync_status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
