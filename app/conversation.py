"""Small, explicit operations for Codex to apply through the existing local API."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .graphs import generate
from .tracking import load_tracking


EDITABLE = ("projects", "goals", "tasks", "time_blocks", "work_logs")


def _item(items, item_id, kind):
    found = next((item for item in items if item["id"] == item_id), None)
    if found is None:
        raise ValueError(f"未找到{kind} {item_id}")
    return found


def _current_goal(body, active=False):
    goal = _item(body["goals"], body["projects"][0]["current_goal_id"], "当前目标")
    if active and goal["decision"] != "active":
        raise ValueError("当前目标未在推进；请先明确继续或开始新阶段")
    return goal


def _operation_fields(operation, required, optional=""):
    if not set(required.split()).issubset(operation) or set(operation) - {"type", *required.split(), *optional.split()}:
        raise ValueError("目标操作字段无效")


def prepare(workspace, operations):
    """Prepare a change without modifying the snapshot returned by the server."""
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations 必须是非空列表")
    body = {name: copy.deepcopy(workspace[name]) for name in EDITABLE}
    summary = []
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("type"), str):
            raise ValueError("操作需要 type")
        kind = operation["type"]
        if kind == "add_candidate":
            if set(operation) != {"type", "title"} or not isinstance(operation["title"], str) or not operation["title"].strip():
                raise ValueError("候选操作需要非空标题")
            body["tasks"].append({"title": operation["title"].strip(), "sort_order": max((t.get("sort_order", 0) for t in body["tasks"]), default=0) + 1})
            summary.append("记录候选：" + operation["title"].strip())
        elif kind == "add_task":
            if not {"type", "title", "completion_criteria"}.issubset(operation) or set(operation) - {"type", "title", "completion_criteria", "depends_on", "source_ref"}:
                raise ValueError("新任务需要标题和完成条件")
            if not isinstance(operation["title"], str) or not operation["title"].strip() or not isinstance(operation["completion_criteria"], str) or not operation["completion_criteria"].strip():
                raise ValueError("新任务标题和完成条件不能为空")
            body["tasks"].append({"title": operation["title"].strip(), "completion_criteria": operation["completion_criteria"].strip(),
                                  "state": "todo", "depends_on": operation.get("depends_on", []),
                                  "goal_id": _current_goal(body, active=True)["id"],
                                  "sort_order": max((t.get("sort_order", 0) for t in body["tasks"]), default=0) + 1,
                                  "source_ref": operation.get("source_ref", "当前 ExecPlan")})
            summary.append("选择任务：" + operation["title"].strip())
        elif kind == "choose_candidate":
            if not {"type", "task_id", "completion_criteria"}.issubset(operation) or set(operation) - {"type", "task_id", "completion_criteria", "depends_on"}:
                raise ValueError("选择候选需要 task_id 和完成条件")
            task = _item(body["tasks"], operation["task_id"], "任务")
            if task["state"] != "proposed" or not isinstance(operation["completion_criteria"], str) or not operation["completion_criteria"].strip():
                raise ValueError("只能选择候选任务，且需要完成条件")
            task["state"] = "todo"
            task["goal_id"] = _current_goal(body, active=True)["id"]
            task["completion_criteria"] = operation["completion_criteria"].strip()
            if "depends_on" in operation:
                task["depends_on"] = operation["depends_on"]
            summary.append("确认执行：" + task["title"])
        elif kind == "set_dependency":
            if set(operation) != {"type", "task_id", "depends_on"} or not isinstance(operation["depends_on"], list):
                raise ValueError("依赖操作需要 task_id 与 depends_on 列表")
            task = _item(body["tasks"], operation["task_id"], "任务")
            task["depends_on"] = operation["depends_on"]
            summary.append("调整前置依赖：" + task["title"])
        elif kind == "set_state":
            if not {"type", "task_id", "state"}.issubset(operation) or set(operation) - {"type", "task_id", "state", "result", "obstacle"}:
                raise ValueError("状态操作需要 task_id 与 state")
            task = _item(body["tasks"], operation["task_id"], "任务")
            target = operation["state"]
            if target not in {"todo", "doing", "blocked", "done", "cancelled"}:
                raise ValueError("不支持的目标状态")
            task["state"] = target
            if target == "blocked":
                obstacle = operation.get("obstacle")
                if not isinstance(obstacle, str) or not obstacle.strip():
                    raise ValueError("受阻必须说明具体阻碍")
                task["block_reason"] = obstacle.strip()
            elif target == "done":
                result = operation.get("result")
                if not isinstance(result, str) or not result.strip():
                    raise ValueError("完成必须写明核对后的实际结果")
                body["work_logs"].append({"task_id": task["id"], "result": result.strip(), "criteria_checked": True})
                task["block_reason"] = ""
            else:
                task["block_reason"] = ""
            summary.append(f"状态改为 {target}：{task['title']}")
        elif kind == "set_blocker":
            if set(operation) != {"type", "task_id", "obstacle"} or not isinstance(operation["obstacle"], str) or not operation["obstacle"].strip():
                raise ValueError("阻碍操作需要 task_id 与具体 obstacle")
            task = _item(body["tasks"], operation["task_id"], "任务")
            task["state"] = "blocked"
            task["block_reason"] = operation["obstacle"].strip()
            summary.append("记录阻碍：" + task["title"])
        elif kind == "clear_blocker":
            if set(operation) != {"type", "task_id"}:
                raise ValueError("解除阻碍操作需要 task_id")
            task = _item(body["tasks"], operation["task_id"], "任务")
            if task["state"] != "blocked":
                raise ValueError("该任务当前没有阻碍状态")
            task["state"] = "todo"
            task["block_reason"] = ""
            summary.append("解除阻碍：" + task["title"])
        elif kind in {"schedule", "reschedule"}:
            required = {"type", "planned_start", "planned_end", "task_id" if kind == "schedule" else "block_id"}
            if set(operation) != required:
                raise ValueError("安排操作需要目标及计划起止时间")
            values = {"planned_start": operation["planned_start"], "planned_end": operation["planned_end"]}
            if kind == "schedule":
                task = _item(body["tasks"], operation["task_id"], "任务")
                body["time_blocks"].append({"task_id": task["id"], **values})
                summary.append("安排：" + task["title"])
            else:
                block = _item(body["time_blocks"], operation["block_id"], "时间块")
                block.update(values)
                summary.append("改期：" + operation["block_id"])
        elif kind == "set_goal":
            if set(operation) != {"type", "goal"} or not isinstance(operation["goal"], str) or not operation["goal"].strip():
                raise ValueError("目标操作需要非空 goal")
            _current_goal(body)["target_state"] = operation["goal"].strip()
            summary.append("修订当前目标")
        elif kind == "start_goal":
            _operation_fields(operation, "title target_state acceptance_criteria execplan_ref", "previous_decision")
            previous = _current_goal(body)
            if "previous_decision" in operation:
                if operation["previous_decision"] not in {"paused", "closed"}:
                    raise ValueError("开始新阶段时前目标只能明确暂停或结束")
                previous["decision"] = operation["previous_decision"]
            elif previous["decision"] == "active":
                raise ValueError("前目标仍在推进，需要 previous_decision 明确暂停或结束")
            goal = {key: operation[key] for key in ("title", "target_state", "acceptance_criteria", "execplan_ref")}
            goal.update(id=str(uuid4()), project_id=body["projects"][0]["id"],
                        predecessor_id=previous["id"], definition_version=1, decision="active", assessments=[])
            body["goals"].append(goal)
            body["projects"][0]["current_goal_id"] = goal["id"]
            summary.append("开始新阶段：" + goal["title"])
        elif kind == "revise_goal":
            _operation_fields(operation, "goal_id", "target_state acceptance_criteria")
            if not {"target_state", "acceptance_criteria"} & set(operation):
                raise ValueError("修订需要目标状态或完成条件")
            goal = _item(body["goals"], operation["goal_id"], "目标")
            for key in ("target_state", "acceptance_criteria"):
                if key in operation:
                    goal[key] = operation[key]
            summary.append("细化目标定义：" + goal["title"])
        elif kind == "assess_goal":
            _operation_fields(operation, "goal_id outcome summary evidence basis")
            goal = _item(body["goals"], operation["goal_id"], "目标")
            goal["assessments"].append({key: operation[key] for key in ("outcome", "summary", "evidence", "basis")})
            summary.append("记录目标评价：" + goal["title"])
        elif kind == "set_goal_decision":
            _operation_fields(operation, "goal_id decision")
            if operation["decision"] not in {"active", "paused", "closed"}:
                raise ValueError("推进决定必须为 active/paused/closed")
            goal = _item(body["goals"], operation["goal_id"], "目标")
            goal["decision"] = operation["decision"]
            summary.append("调整推进决定：" + goal["title"])
        elif kind == "assign_task_goal":
            _operation_fields(operation, "task_id goal_id")
            task = _item(body["tasks"], operation["task_id"], "任务")
            if operation["goal_id"] is not None:
                _item(body["goals"], operation["goal_id"], "目标")
            task["goal_id"] = operation["goal_id"]
            summary.append("调整目标关联：" + task["title"])
        else:
            raise ValueError(f"不支持的操作：{kind}")
    return body, summary


def _api_json(url, method="GET", payload=None):
    origin = urlsplit(url)
    if origin.scheme != "http" or origin.hostname != "127.0.0.1" or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment:
        raise ValueError("接口只允许 http://127.0.0.1:端口")
    base = url.rstrip("/")
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.update({"Content-Type": "application/json", "Origin": base})
    request = Request(base + "/api/workspace", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc).get("error", str(exc))
        except (ValueError, OSError):
            detail = str(exc)
        raise RuntimeError(f"保存未完成（HTTP {exc.code}）：{detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接本地服务：{exc.reason}") from exc


def apply(url, operations, reason, context_path, output, plantuml):
    current = _api_json(url)["workspace"]
    body, summary = prepare(current, operations)
    saved = _api_json(url, "PUT", {"expected_revision": current["revision"], "workspace": body, "reason": reason})
    workspace = saved["workspace"]
    try:
        context = json.loads(Path(context_path).read_text(encoding="utf-8"))
        tracking = load_tracking(Path(context_path).parent / "tracking.json")
        page = generate(workspace, context, output, Path(plantuml) if plantuml else None, tracking=tracking)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"已保存 revision {workspace['revision']}，但图未更新：{exc}") from exc
    return {"revision": workspace["revision"], "summary": summary, "page": str(page.resolve())}


def main():
    parser = argparse.ArgumentParser(description="Apply explicit conversational changes through the local API")
    parser.add_argument("--request", required=True, help="JSON file containing reason and operations")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--context", default="data/context.json")
    parser.add_argument("--output", default="views/current")
    parser.add_argument("--plantuml", default=None)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    if not isinstance(request, dict) or set(request) != {"reason", "operations"} or not isinstance(request["reason"], str):
        parser.error("请求必须包含 reason 和 operations")
    result = apply(args.url, request["operations"], request["reason"], args.context, args.output, args.plantuml)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
