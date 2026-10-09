"""Workspace shape, task rules, and derived views for the first local project."""
from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from uuid import UUID, uuid4


class ValidationError(ValueError):
    pass


STATES = {"proposed", "todo", "doing", "blocked", "done", "cancelled"}
TRANSITIONS = {
    "proposed": {"todo", "cancelled"},
    "todo": {"doing", "blocked", "cancelled"},
    "doing": {"todo", "blocked", "done", "cancelled"},
    "blocked": {"todo", "cancelled"},
    "done": {"todo"},
    "cancelled": {"todo"},
}
ROOT_FIELDS_V1 = {"schema_version", "revision", "projects", "tasks", "time_blocks", "work_logs", "changes"}
ROOT_FIELDS = ROOT_FIELDS_V1 | {"goals"}
EDITABLE_FIELDS = {"projects", "goals", "tasks", "time_blocks", "work_logs"}
PROJECT_FIELDS_V1 = {"id", "title", "goal", "reason", "acceptance_criteria", "source_ref", "execplan_ref"}
PROJECT_FIELDS = {"id", "title", "reason", "source_ref", "current_goal_id"}
GOAL_FIELDS = {"id", "project_id", "title", "target_state", "acceptance_criteria", "definition_version", "execplan_ref", "predecessor_id", "decision", "assessments"}
ASSESSMENT_FIELDS = {"id", "definition_version", "outcome", "summary", "evidence", "basis", "at"}
TASK_FIELDS_V1 = {"id", "project_id", "title", "completion_criteria", "state", "depends_on", "source_ref", "block_reason", "stage", "effort_minutes", "due_date", "sort_order"}
TASK_FIELDS = TASK_FIELDS_V1 | {"goal_id"}
GOAL_DECISIONS = {"active", "paused", "closed", "unknown"}
ASSESSMENT_OUTCOMES = {"met", "not_met", "insufficient_evidence"}
ASSESSMENT_BASES = {"user_feedback", "technical_check", "observation"}
BLOCK_FIELDS = {"id", "task_id", "planned_start", "planned_end"}
LOG_FIELDS = {"id", "task_id", "block_id", "actual_start", "actual_end", "result", "obstacle", "evidence", "note", "criteria_checked"}


def _text(value, name, *, required=False, limit=4000):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValidationError(f"{name} 需要文本" + ("且不能为空" if required else ""))
    return value.strip()


def _datetime(value, name, *, optional=False):
    if value in (None, "") and optional:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} 需要带时区的日期时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} 日期时间无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{name} 必须带时区偏移")
    return parsed


def _list(value, name):
    if not isinstance(value, list) or len(value) > 2000:
        raise ValidationError(f"{name} 必须是列表，且不能超过 2000 项")
    return value


def _object(value, name, fields):
    if not isinstance(value, dict) or set(value) - fields:
        raise ValidationError(f"{name} 含有未知字段或不是对象")
    return value


def _items(value, name, fields):
    seen = set()
    result = {}
    for item in _list(value, name):
        _object(item, name, fields)
        key = item.get("id")
        if not isinstance(key, str) or not key or key in seen:
            raise ValidationError(f"{name} 的 ID 缺失或重复")
        seen.add(key)
        result[key] = item
    return result


def initial_workspace():
    goal_id = str(uuid4())
    workspace = {
        "schema_version": 2,
        "revision": 0,
        "projects": [{
            "id": "life-projects",
            "title": "life-projects",
            "reason": "减少反复阅读资料和重新判断下一步的负担，并从真实使用修订长期方向。",
            "source_ref": "README.md；docs/architecture/tobe.md",
            "current_goal_id": goal_id,
        }],
        "goals": [{
            "id": goal_id,
            "project_id": "life-projects",
            "title": "验证本地开发管理闭环",
            "target_state": "用本地面板管理本项目开发，看清目标、下一步、时间安排和执行反馈。",
            "acceptance_criteria": "完成一次真实开发任务的安排、执行、调整与回顾；记录减负是否有证据。",
            "definition_version": 1,
            "execplan_ref": "docs/exec-plans/active/2026-09-25-self-management-mvp.md",
            "predecessor_id": None,
            "decision": "active",
            "assessments": [],
        }],
        "tasks": [
            {"id": str(uuid4()), "project_id": "life-projects", "goal_id": goal_id, "title": title,
             "completion_criteria": criterion, "state": "todo", "depends_on": [],
             "source_ref": "核心 ExecPlan", "block_reason": "", "stage": stage,
             "effort_minutes": None, "due_date": None, "sort_order": index}
            for index, (title, criterion, stage) in enumerate([
                ("M1：实现本地服务、面板和可靠保存", "可操作页面能更新项目与任务，重启后保留；冲突和失败不丢数据。", "M1"),
                ("M2：实现依赖、周安排和实际记录", "依赖、周视图与实际记录可操作，相关行为测试通过。", "M2"),
                ("M3：用面板推进真实开发任务并回顾", "在面板完成一项真实开发任务的安排、执行与调整；如实记录减负结论。", "M3"),
            ], start=1)
        ],
        "time_blocks": [],
        "work_logs": [],
        "changes": [],
    }
    workspace["tasks"][1]["depends_on"] = [workspace["tasks"][0]["id"]]
    workspace["tasks"][2]["depends_on"] = [workspace["tasks"][1]["id"]]
    return workspace


def validate_workspace(workspace):
    return _validate_workspace(workspace, version=2)


def validate_workspace_v1(workspace):
    """Strictly validate original ledgers before any migration or recovery."""
    return _validate_workspace(workspace, version=1)


def _validate_workspace(workspace, *, version):
    root_fields = ROOT_FIELDS if version == 2 else ROOT_FIELDS_V1
    project_fields = PROJECT_FIELDS if version == 2 else PROJECT_FIELDS_V1
    task_fields = TASK_FIELDS if version == 2 else TASK_FIELDS_V1
    if not isinstance(workspace, dict) or set(workspace) != root_fields:
        raise ValidationError("账本根字段不受支持或缺失")
    if type(workspace["schema_version"]) is not int or workspace["schema_version"] != version or type(workspace["revision"]) is not int or workspace["revision"] < 0:
        raise ValidationError("账本版本或修订号无效")
    projects = _items(workspace["projects"], "projects", project_fields)
    if set(projects) != {"life-projects"}:
        raise ValidationError("首版只能管理 life-projects 一个项目")
    project = projects["life-projects"]
    if set(project) != project_fields:
        raise ValidationError("项目字段缺失")
    for field in project_fields - {"id", "current_goal_id"}:
        _text(project[field], f"项目 {field}", required=field in {"title", "goal"})

    goals = {}
    if version == 2:
        goals = _items(workspace["goals"], "goals", GOAL_FIELDS)
        for goal in goals.values():
            if set(goal) != GOAL_FIELDS or goal["project_id"] != project["id"]:
                raise ValidationError("目标字段缺失或项目引用无效")
            for field in ("title", "target_state", "acceptance_criteria", "execplan_ref"):
                _text(goal[field], f"目标 {field}", required=field in {"title", "target_state"} or
                      (field == "acceptance_criteria" and goal["id"] != "legacy-life-projects"))
            if type(goal["definition_version"]) is not int or goal["definition_version"] < 1:
                raise ValidationError("目标定义版本无效")
            if not isinstance(goal["decision"], str) or goal["decision"] not in GOAL_DECISIONS or (goal["decision"] == "unknown" and goal["id"] != "legacy-life-projects"):
                raise ValidationError("目标推进决定无效")
            predecessor = goal["predecessor_id"]
            if predecessor is not None and (not isinstance(predecessor, str) or predecessor not in goals or predecessor == goal["id"]):
                raise ValidationError("目标接续引用无效")
            seen_assessments = set()
            for item in _list(goal["assessments"], "assessments"):
                if not isinstance(item, dict) or set(item) != ASSESSMENT_FIELDS:
                    raise ValidationError("评价字段无效")
                if not isinstance(item["id"], str) or not item["id"] or item["id"] in seen_assessments:
                    raise ValidationError("评价 ID 缺失或重复")
                seen_assessments.add(item["id"])
                if type(item["definition_version"]) is not int or not 1 <= item["definition_version"] <= goal["definition_version"]:
                    raise ValidationError("评价定义版本无效")
                if (not isinstance(item["outcome"], str) or item["outcome"] not in ASSESSMENT_OUTCOMES or
                        not isinstance(item["basis"], str) or item["basis"] not in ASSESSMENT_BASES):
                    raise ValidationError("评价结论或依据类别无效")
                _text(item["summary"], "评价摘要", required=True)
                _text(item["evidence"], "评价证据", required=True)
                _datetime(item["at"], "评价时间")
        current_goal_id = project["current_goal_id"]
        if not isinstance(current_goal_id, str) or current_goal_id not in goals:
            raise ValidationError("当前目标引用无效")
        active = [goal["id"] for goal in goals.values() if goal["decision"] == "active"]
        if len(active) > 1 or (active and active[0] != current_goal_id):
            raise ValidationError("同一项目只允许一个当前 active 目标")
        visiting_goals, visited_goals = set(), set()
        def visit_goal(goal_id):
            if goal_id in visiting_goals:
                raise ValidationError("目标接续形成了循环")
            if goal_id in visited_goals:
                return
            visiting_goals.add(goal_id)
            predecessor = goals[goal_id]["predecessor_id"]
            if predecessor is not None:
                visit_goal(predecessor)
            visiting_goals.remove(goal_id)
            visited_goals.add(goal_id)
        for goal_id in goals:
            visit_goal(goal_id)

    tasks = _items(workspace["tasks"], "tasks", task_fields)
    if version == 2 and (set(goals) & (set(tasks) | set(projects))):
        raise ValidationError("目标 ID 与项目或任务 ID 重复")
    for task in tasks.values():
        if set(task) != task_fields or task["project_id"] != "life-projects":
            raise ValidationError("任务字段缺失或项目引用无效")
        if version == 2 and task["goal_id"] is not None and (not isinstance(task["goal_id"], str) or task["goal_id"] not in goals):
            raise ValidationError("任务目标引用无效")
        _text(task["title"], "任务标题", required=True, limit=300)
        if not isinstance(task["state"], str) or task["state"] not in STATES:
            raise ValidationError("任务状态无效")
        _text(task["completion_criteria"], "完成条件", required=task["state"] not in {"proposed", "cancelled"})
        for field in ("source_ref", "block_reason", "stage"):
            _text(task[field], f"任务 {field}")
        if task["state"] == "blocked" and not task["block_reason"].strip():
            raise ValidationError("受阻任务需要写明阻碍")
        if not isinstance(task["depends_on"], list) or any(not isinstance(dep, str) for dep in task["depends_on"]):
            raise ValidationError("任务依赖无效")
        if len(set(task["depends_on"])) != len(task["depends_on"]):
            raise ValidationError("任务依赖重复")
        if any(not isinstance(dep, str) or dep not in tasks or dep == task["id"] for dep in task["depends_on"]):
            raise ValidationError("任务依赖不存在或指向自身")
        effort = task["effort_minutes"]
        if effort is not None and (type(effort) is not int or effort < 0 or effort > 100000):
            raise ValidationError("任务估时必须是非负整数分钟")
        if task["due_date"] is not None:
            if not isinstance(task["due_date"], str):
                raise ValidationError("任务期限日期无效")
            try:
                date.fromisoformat(task["due_date"])
            except ValueError as exc:
                raise ValidationError("任务期限日期无效") from exc
        if type(task["sort_order"]) is not int:
            raise ValidationError("任务顺序必须是整数")
    visiting, visited = set(), set()
    def visit(task_id):
        if task_id in visiting:
            raise ValidationError("任务依赖形成了循环")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep in tasks[task_id]["depends_on"]:
            visit(dep)
        visiting.remove(task_id)
        visited.add(task_id)
    for task_id in tasks:
        visit(task_id)

    blocks = _items(workspace["time_blocks"], "time_blocks", BLOCK_FIELDS)
    for block in blocks.values():
        if set(block) != BLOCK_FIELDS or not isinstance(block["task_id"], str) or block["task_id"] not in tasks:
            raise ValidationError("时间块字段或任务引用无效")
        start = _datetime(block["planned_start"], "计划开始")
        end = _datetime(block["planned_end"], "计划结束")
        if end <= start:
            raise ValidationError("计划结束必须晚于开始")
    logs = _items(workspace["work_logs"], "work_logs", LOG_FIELDS)
    for log in logs.values():
        if set(log) != LOG_FIELDS or not isinstance(log["task_id"], str) or log["task_id"] not in tasks:
            raise ValidationError("执行记录字段或任务引用无效")
        if log["block_id"] is not None and (not isinstance(log["block_id"], str) or log["block_id"] not in blocks or blocks[log["block_id"]]["task_id"] != log["task_id"]):
            raise ValidationError("执行记录关联了其他任务的时间块")
        start = _datetime(log["actual_start"], "实际开始", optional=True)
        end = _datetime(log["actual_end"], "实际结束", optional=True)
        if start and end and end < start:
            raise ValidationError("实际结束不能早于开始")
        for field in ("result", "obstacle", "evidence", "note"):
            _text(log[field], f"执行记录 {field}")
        if not any(log[field].strip() for field in ("result", "obstacle", "note")):
            raise ValidationError("执行记录至少需要结果、阻碍或备注")
        if type(log["criteria_checked"]) is not bool:
            raise ValidationError("完成条件核对值无效")
    if not isinstance(workspace["changes"], list) or len(workspace["changes"]) > 20000:
        raise ValidationError("变更历史无效")
    for change in workspace["changes"]:
        if not isinstance(change, dict) or set(change) != {"entity", "id", "old", "new", "at", "reason"}:
            raise ValidationError("变更历史格式无效")
        _datetime(change["at"], "变更时间")
        _text(change["reason"], "变更理由", required=True)
    return tasks, blocks, logs


def _new_id(item, current_ids, kind):
    if "id" not in item or item["id"] in (None, ""):
        item["id"] = str(uuid4())
    elif not isinstance(item["id"], str) or item["id"] not in current_ids:
        if kind == "goals":
            try:
                parsed = UUID(item["id"])
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValidationError("新目标 ID 需要 UUIDv4") from exc
            if parsed.version == 4 and str(parsed) == item["id"]:
                return
            raise ValidationError("新目标 ID 需要 UUIDv4")
        raise ValidationError(f"新增{kind}的 ID 必须由服务生成")


def migrate_workspace_v1(workspace):
    """Preserve a validated v1 ledger while introducing unknown historic goal links."""
    validate_workspace_v1(workspace)
    migrated = copy.deepcopy(workspace)
    old_project = migrated["projects"][0]
    migrated["projects"] = [{
        "id": old_project["id"],
        "title": old_project["title"],
        "reason": old_project["reason"],
        "source_ref": old_project["source_ref"],
        "current_goal_id": "legacy-life-projects",
    }]
    migrated["goals"] = [{
        "id": "legacy-life-projects",
        "project_id": old_project["id"],
        "title": "迁移前的项目目标",
        "target_state": old_project["goal"],
        "acceptance_criteria": old_project["acceptance_criteria"],
        "definition_version": 1,
        "execplan_ref": old_project["execplan_ref"],
        "predecessor_id": None,
        "decision": "unknown",
        "assessments": [],
    }]
    for task in migrated["tasks"]:
        task["goal_id"] = None
    migrated["schema_version"] = 2
    migrated["revision"] += 1
    validate_workspace(migrated)
    return migrated


def normalize_and_validate(current, submitted, reason):
    if not isinstance(submitted, dict) or set(submitted) != EDITABLE_FIELDS:
        raise ValidationError("提交内容只能包含 projects、goals、tasks、time_blocks、work_logs")
    if not isinstance(reason, str) or len(reason) > 2000:
        raise ValidationError("变更说明无效")
    body = copy.deepcopy(submitted)
    for name, fields in (("goals", GOAL_FIELDS), ("tasks", TASK_FIELDS), ("time_blocks", BLOCK_FIELDS), ("work_logs", LOG_FIELDS)):
        current_ids = {item["id"] for item in current[name]}
        for item in _list(body[name], name):
            _object(item, name, fields)
            _new_id(item, current_ids, name)
    old_goals = {item["id"]: item for item in current["goals"]}
    for goal in body["goals"]:
        old = old_goals.get(goal["id"])
        if old is None:
            goal.setdefault("project_id", "life-projects")
            goal.setdefault("definition_version", 1)
            if goal["definition_version"] != 1 or type(goal["definition_version"]) is not int:
                raise ValidationError("新目标定义版本必须为 1")
            goal.setdefault("execplan_ref", "")
            goal.setdefault("predecessor_id", None)
            goal.setdefault("decision", "active")
            goal.setdefault("assessments", [])
            if goal["assessments"]:
                raise ValidationError("新目标需要先保存，评价须单独追加")
        else:
            definition_changed = any(goal.get(key) != old[key] for key in ("target_state", "acceptance_criteria"))
            if definition_changed:
                if not reason.strip():
                    raise ValidationError("修改目标定义需简短说明")
                goal["definition_version"] = old["definition_version"] + 1
            elif goal.get("definition_version") != old["definition_version"]:
                raise ValidationError("定义版本由服务维护")
            if goal.get("decision") != old["decision"] and not reason.strip():
                raise ValidationError("修改目标推进决定需简短说明")
            old_assessments = old["assessments"]
            supplied = goal.get("assessments")
            if not isinstance(supplied, list) or supplied[:len(old_assessments)] != old_assessments:
                raise ValidationError("已有评价只能追加，不能修改或删除")
            if definition_changed and len(supplied) > len(old_assessments):
                raise ValidationError("目标定义修订与评价必须分开保存")
            for assessment in supplied[len(old_assessments):]:
                _object(assessment, "assessment", ASSESSMENT_FIELDS)
                if "id" in assessment or "at" in assessment or "definition_version" in assessment:
                    raise ValidationError("评价 ID、时间和定义版本由服务生成")
                assessment["id"] = str(uuid4())
                assessment["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                assessment["definition_version"] = goal["definition_version"]
    for task in body["tasks"]:
        task.setdefault("project_id", "life-projects")
        task.setdefault("completion_criteria", "")
        task.setdefault("state", "proposed")
        task.setdefault("depends_on", [])
        task.setdefault("source_ref", "手工输入")
        task.setdefault("block_reason", "")
        task.setdefault("stage", "")
        task.setdefault("effort_minutes", None)
        task.setdefault("due_date", None)
        task.setdefault("sort_order", len(body["tasks"]))
        task.setdefault("goal_id", None)
    for log in body["work_logs"]:
        for field, default in (("block_id", None), ("actual_start", None), ("actual_end", None),
                               ("result", ""), ("obstacle", ""), ("evidence", ""), ("note", ""),
                               ("criteria_checked", False)):
            log.setdefault(field, default)
    candidate = {**body, "schema_version": 2, "revision": current["revision"] + 1, "changes": list(current["changes"])}
    old_tasks = {item["id"]: item for item in current["tasks"]}
    old_logs = {item["id"]: item for item in current["work_logs"]}
    new_tasks, _, new_logs = validate_workspace(candidate)
    new_goals = {item["id"]: item for item in candidate["goals"]}
    if not set(old_goals).issubset(new_goals):
        raise ValidationError("已有目标不能删除")
    if not set(old_tasks).issubset(new_tasks) or not set(old_logs).issubset(new_logs):
        raise ValidationError("已有任务和执行记录不能删除")
    for task_id, task in new_tasks.items():
        old = old_tasks.get(task_id)
        if not old or task["state"] == old["state"]:
            continue
        if task["state"] not in TRANSITIONS[old["state"]]:
            raise ValidationError(f"{task['title']}：不允许 {old['state']} → {task['state']}")
        if task["state"] == "doing" and any(new_tasks[dep]["state"] != "done" for dep in task["depends_on"]):
            raise ValidationError(f"{task['title']}：前置任务尚未完成")
        if task["state"] == "done" and not any(
            log["task_id"] == task_id and log["id"] not in old_logs and
            log["criteria_checked"] and log["result"].strip()
            for log in new_logs.values()
        ):
            raise ValidationError(f"{task['title']}：完成前需核对条件并记录实际结果")
        if task["state"] in {"cancelled"} or old["state"] in {"done", "cancelled", "blocked"}:
            if not reason.strip():
                raise ValidationError(f"{task['title']}：取消、重开或解除阻碍需说明原因")
    previous_project = current["projects"][0]
    next_project = candidate["projects"][0] if candidate["projects"] else {}
    if any(previous_project.get(key) != next_project.get(key) for key in ("reason", "current_goal_id")) and not reason.strip():
        raise ValidationError("修改项目理由或当前目标需简短说明")
    changes = []
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for name, label in (("projects", "project"), ("goals", "goal"), ("tasks", "task"), ("time_blocks", "time_block"), ("work_logs", "work_log")):
        before = {item["id"]: item for item in current[name]}
        after = {item["id"]: item for item in candidate[name]}
        for key in sorted(set(before) | set(after)):
            old, new = before.get(key), after.get(key)
            if old != new:
                action = "新增" if old is None else "移除安排" if new is None else "更新"
                changes.append({"entity": label, "id": key, "old": old, "new": new,
                                "at": at, "reason": reason.strip() or f"{action}{label}"})
    if not changes:
        raise ValidationError("没有需要保存的更改")
    candidate["changes"] += changes
    validate_workspace(candidate)
    return candidate


def derived_view(workspace):
    tasks = {task["id"]: task for task in workspace["tasks"]}
    current_goal_id = workspace["projects"][0]["current_goal_id"]
    goals = {goal["id"]: goal for goal in workspace["goals"]}
    active_goal = goals.get(current_goal_id)
    ready = [task["id"] for task in workspace["tasks"]
             if task["state"] == "todo" and task["goal_id"] == current_goal_id
             and active_goal is not None and active_goal["decision"] == "active"
             and all(tasks[dep]["state"] == "done" for dep in task["depends_on"])]
    warnings = []
    for task in workspace["tasks"]:
        if task["state"] in {"doing", "done"}:
            unmet = [dep for dep in task["depends_on"] if tasks[dep]["state"] != "done"]
            if unmet:
                warnings.append({"task_id": task["id"], "kind": "upstream_reopened", "depends_on": unmet})
    blocks = workspace["time_blocks"]
    for i, first in enumerate(blocks):
        a, b = _datetime(first["planned_start"], "计划开始"), _datetime(first["planned_end"], "计划结束")
        for second in blocks[i + 1:]:
            c, d = _datetime(second["planned_start"], "计划开始"), _datetime(second["planned_end"], "计划结束")
            if a < d and c < b:
                warnings.append({"kind": "time_overlap", "blocks": [first["id"], second["id"]]})
    assessments = {goal["id"]: next((item["outcome"] for item in reversed(goal["assessments"])
                                      if item["definition_version"] == goal["definition_version"]), "unassessed")
                   for goal in goals.values()}
    return {"ready_task_ids": ready, "warnings": warnings,
            "current_goal_id": current_goal_id, "goal_assessment_status": assessments}
