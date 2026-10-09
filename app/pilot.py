"""Private conversational pilot: explicit facts in, derived overview/day out.

No chat fetching, model calls, automatic commitments, or website integration.
"""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from .graphs import ID, STATE_COLORS, STATE_LABELS, _alias, _render_svg, _wrapped
from .store import WorkspaceStore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "private/job-search.json"
SCHEMA = "life-project-pilot/v1"
STATES = {"proposed", "todo", "doing", "blocked", "done", "cancelled"}
PERIODS = {"morning": "上午", "afternoon": "下午", "evening": "晚上"}
PREF_KEYS = {"task_max_minutes", "preferred_period", "daily_minutes"}


class PilotError(ValueError):
    pass


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def fields(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= set(obj) or set(obj) - set(required) - set(optional):
        raise PilotError(f"字段无效；必填：{', '.join(required)}")


def text(value, label, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > 20000:
        raise PilotError(f"{label} 需要有效文字")


def integer(value, label, minimum=1):
    if type(value) is not int or not minimum <= value <= 1000000:
        raise PilotError(f"{label} 需要有效整数")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise PilotError("时间需要 ISO 日期及明确时区") from None


def day(value):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except (TypeError, ValueError):
        raise PilotError("日期需要 YYYY-MM-DD") from None


def strings(value, label):
    if not isinstance(value, list):
        raise PilotError(f"{label} 需要列表")
    for item in value:
        text(item, label)


def members(items, label):
    if not isinstance(items, list):
        raise PilotError(f"{label} 需要列表")
    result = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ID.fullmatch(item["id"]) or item["id"] in result:
            raise PilotError(f"{label} ID 无效或重复")
        result[item["id"]] = item
    return result


def dependencies(items):
    for item in items.values():
        refs = item["depends_on"]
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs) or len(set(refs)) != len(refs) or any(ref not in items or ref == item["id"] for ref in refs):
            raise PilotError("依赖引用无效")
    visiting, visited = set(), set()
    def visit(key):
        if key in visiting:
            raise PilotError("前置依赖不能成环；反馈回看请用 feedback 关系")
        if key in visited:
            return
        visiting.add(key)
        for ref in items[key]["depends_on"]:
            visit(ref)
        visiting.remove(key)
        visited.add(key)
    for key in items:
        visit(key)


def preference(key, value):
    if key not in PREF_KEYS:
        raise PilotError("未知偏好键")
    if key == "preferred_period":
        if value not in PERIODS:
            raise PilotError("偏好时段无效")
    else:
        integer(value, key)


def validate(data):
    fields(data, ("schema", "revision", "project", "goal", "initial_state", "current_state", "stages", "routes", "actions", "capacities", "schedules", "actuals", "settings", "feedback_source", "changes", "updated_at"))
    if data["schema"] != SCHEMA:
        raise PilotError("未知记录版本")
    integer(data["revision"], "revision")
    timestamp(data["updated_at"])
    fields(data["project"], ("id", "title"))
    members([data["project"]], "项目"); text(data["project"]["title"], "项目名")
    goal = data["goal"]
    fields(goal, ("id", "target_state", "criteria", "constraints", "unknowns", "version", "decision", "assessment", "history"))
    members([goal], "目标"); integer(goal["version"], "目标版本")
    text(goal["target_state"], "目标状态")
    for key in ("criteria", "constraints", "unknowns"):
        strings(goal[key], key)
    if goal["decision"] not in {"active", "paused", "closed"}:
        raise PilotError("目标推进决定无效")
    def assessment(value):
        fields(value, ("outcome", "summary", "evidence", "source_ref"))
        if value["outcome"] not in {"unassessed", "met", "not_met", "insufficient_evidence"}:
            raise PilotError("验收结论无效")
        text(value["summary"], "评价"); strings(value["evidence"], "证据")
        text(value["source_ref"], "评价来源", empty=value["outcome"] == "unassessed")
        if value["outcome"] == "met" and not value["evidence"]:
            raise PilotError("达成结论需要证据；行动完成不能替代目标验收")
    assessment(goal["assessment"])
    if not isinstance(goal["history"], list):
        raise PilotError("目标历史需要列表")
    for old in goal["history"]:
        fields(old, ("version", "target_state", "criteria", "constraints", "unknowns", "assessment", "reason", "source_ref", "revised_at"))
        integer(old["version"], "历史版本"); text(old["target_state"], "历史目标")
        for k in ("criteria", "constraints", "unknowns"):
            strings(old[k], k)
        assessment(old["assessment"])
        text(old["reason"], "修订理由"); text(old["source_ref"], "修订来源"); timestamp(old["revised_at"])
    if [old["version"] for old in goal["history"]] != list(range(1, goal["version"])):
        raise PilotError("目标定义历史版本不连续")
    for key in ("initial_state", "current_state"):
        fields(data[key], ("summary", "source_ref", "recorded_at"), ("stage_id",) if key == "current_state" else ())
        text(data[key]["summary"], key); text(data[key]["source_ref"], "状态来源"); timestamp(data[key]["recorded_at"])
    stages = members(data["stages"], "阶段")
    actions = members(data["actions"], "行动")
    if set(stages) & set(actions) or goal["id"] in stages or goal["id"] in actions:
        raise PilotError("阶段、行动与目标 ID 必须不同")
    for stage in stages.values():
        fields(stage, ("id", "title", "state", "origin", "external", "depends_on", "block_reason"))
        text(stage["title"], "阶段名"); text(stage["block_reason"], "阻碍", empty=True)
        if stage["state"] not in STATES or stage["origin"] not in {"proposed", "confirmed"} or type(stage["external"]) is not bool:
            raise PilotError("阶段状态或性质无效")
        if stage["state"] == "blocked" and not stage["block_reason"].strip():
            raise PilotError("受阻阶段需要原因")
    dependencies(stages)
    if data["current_state"].get("stage_id") is not None and data["current_state"]["stage_id"] not in stages:
        raise PilotError("当前位置阶段不存在")
    for action in actions.values():
        fields(action, ("id", "stage_id", "title", "completion_criteria", "state", "depends_on", "effort_minutes", "block_reason", "why", "actor", "source_ref", "priority"))
        for k in ("title", "completion_criteria", "why", "source_ref"):
            text(action[k], k)
        if action["stage_id"] not in stages or action["state"] not in STATES or action["actor"] not in {"user", "codex"}:
            raise PilotError("行动引用或状态无效")
        integer(action["priority"], "排序", 0)
        text(action["block_reason"], "阻碍", empty=True)
        if action["state"] == "blocked" and not action["block_reason"].strip():
            raise PilotError("受阻行动需要原因")
        if action["effort_minutes"] is not None:
            integer(action["effort_minutes"], "估时")
    dependencies(actions)
    if not isinstance(data["routes"], list):
        raise PilotError("路径需要列表")
    endpoints = {"start", goal["id"], *stages}
    seen_routes = set()
    for route in data["routes"]:
        fields(route, ("from", "to", "kind", "note"))
        if route["from"] not in endpoints or route["to"] not in endpoints or route["from"] == route["to"] or route["kind"] not in {"proposed_route", "feedback"}:
            raise PilotError("推进或回看关系无效；必要前置请用 depends_on")
        text(route["note"], "关系说明")
        edge = (route["from"], route["to"], route["kind"])
        if edge in seen_routes:
            raise PilotError("重复路径")
        seen_routes.add(edge)
    if not isinstance(data["capacities"], dict) or not isinstance(data["schedules"], dict):
        raise PilotError("容量与安排需要按日记录")
    for key, capacity in data["capacities"].items():
        day(key); fields(capacity, ("periods", "fixed", "source_ref")); text(capacity["source_ref"], "容量来源")
        if not isinstance(capacity["periods"], dict) or set(capacity["periods"]) - set(PERIODS):
            raise PilotError("容量时段无效")
        for minutes in capacity["periods"].values():
            integer(minutes, "净可用分钟", 0)
        if not isinstance(capacity["fixed"], list):
            raise PilotError("固定约定需要列表")
        fixed_times = []
        for fixed in capacity["fixed"]:
            fields(fixed, ("title", "start", "end")); text(fixed["title"], "约定")
            start, end = timestamp(fixed["start"]), timestamp(fixed["end"])
            if start >= end or start.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != key or end.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != key:
                raise PilotError("约定需在本日且起止正确")
            fixed_times.append((start, end))
        if any(a[1] > b[0] for a, b in zip(sorted(fixed_times), sorted(fixed_times)[1:])):
            raise PilotError("固定约定有冲突")
    for key, schedule in data["schedules"].items():
        day(key); fields(schedule, ("entries", "source_ref")); text(schedule["source_ref"], "安排来源")
        if key not in data["capacities"]:
            raise PilotError("缺少当天容量，不能保存日程")
        if not isinstance(schedule["entries"], list):
            raise PilotError("日程需要列表")
        seen, totals, roles = set(), {}, {"primary": 0, "optional": 0}
        for entry in schedule["entries"]:
            fields(entry, ("action_id", "period", "minutes", "role"))
            if entry["action_id"] not in actions or entry["action_id"] in seen or entry["period"] not in PERIODS or entry["role"] not in roles:
                raise PilotError("日程行动、时段或角色无效")
            seen.add(entry["action_id"]); integer(entry["minutes"], "安排分钟")
            totals[entry["period"]] = totals.get(entry["period"], 0) + entry["minutes"]
            roles[entry["role"]] += 1
        if roles["primary"] > 1 or roles["optional"] > 2 or any(total > data["capacities"][key]["periods"].get(period, 0) for period, total in totals.items()):
            raise PilotError("安排超出净容量，或超过一个主要行动/两个可选行动")
    actuals = members(data["actuals"], "反馈")
    sources = set()
    for actual in actuals.values():
        fields(actual, ("id", "source_id", "source_ref", "action_id", "result", "obstacle", "evidence", "actual_minutes", "action_state", "recorded_at"))
        for k in ("source_id", "source_ref", "result"):
            text(actual[k], k)
        text(actual["obstacle"], "实际阻碍", empty=True); strings(actual["evidence"], "实际证据")
        if actual["source_id"] in sources or actual["action_id"] not in actions or actual["action_state"] not in STATES:
            raise PilotError("反馈来源重复或行动无效")
        sources.add(actual["source_id"]); timestamp(actual["recorded_at"])
        if actual["actual_minutes"] is not None:
            integer(actual["actual_minutes"], "实际分钟", 0)
    for action in actions.values():
        if action["state"] == "done" and not any(a["action_id"] == action["id"] and a["action_state"] == "done" for a in actuals.values()):
            raise PilotError("完成行动需要实际结果反馈")
    settings = data["settings"]
    fields(settings, ("defaults", "confirmed", "project_overrides", "observations", "trials"))
    if settings["defaults"] != {"trigger": "on_demand", "schedule": "periods_duration", "learning": "trial_then_confirm"}:
        raise PilotError("默认工作方式变更超出本轮合同")
    for scope in ("confirmed", "project_overrides"):
        if not isinstance(settings[scope], dict):
            raise PilotError("偏好需要字典")
        for k, value in settings[scope].items():
            fields(value, ("value", "source_ref")); preference(k, value["value"]); text(value["source_ref"], "偏好来源")
    for item in members(settings["observations"], "观察").values():
        fields(item, ("id", "text", "source_ref", "recorded_at")); text(item["text"], "观察"); text(item["source_ref"], "观察来源"); timestamp(item["recorded_at"])
    for trial in members(settings["trials"], "试调").values():
        fields(trial, ("id", "key", "value", "rationale", "source_ref", "status"))
        preference(trial["key"], trial["value"]); text(trial["rationale"], "试调原因"); text(trial["source_ref"], "试调来源")
        if trial["status"] not in {"trial", "confirmed", "rejected"}:
            raise PilotError("试调状态无效")
    fields(data["feedback_source"], ("thread_id", "status", "checked_at", "detail"))
    source = data["feedback_source"]
    text(source["thread_id"], "反馈对话")
    if source["status"] not in {"not_checked", "checked", "unavailable"}:
        raise PilotError("来源核对状态无效")
    if source["checked_at"] is not None:
        timestamp(source["checked_at"])
    if source["status"] != "not_checked" and source["checked_at"] is None:
        raise PilotError("已核对/失败来源需要核对时间")
    text(source["detail"], "来源核对说明")
    if not isinstance(data["changes"], list):
        raise PilotError("变更需要列表")
    for change in data["changes"]:
        fields(change, ("revision", "reason", "source_ref", "at", "summary"))
        integer(change["revision"], "变更版本"); text(change["reason"], "变更理由"); text(change["source_ref"], "变更来源"); timestamp(change["at"]); strings(change["summary"], "变更摘要")
    return data


def initial_record():
    at = now()
    source = "synthetic-example: hypothetical job-search route; no actual user approval"
    stages = [
        ("explore", "探索并筛选方向", False), ("materials", "准备适配材料", False),
        ("outreach", "投递与交流", True), ("evaluate", "评估机会与底线", True),
    ]
    data = {
        "schema": SCHEMA, "revision": 1, "project": {"id": "job-search", "title": "寻找工作"},
        "goal": {"id": "accepted-suitable-offer", "target_state": "接受符合个人底线的 offer", "criteria": ["明确个人底线，并依据具体 offer 核对是否满足", "本人明确决定接受该 offer"], "constraints": [], "unknowns": ["可接受工作的具体底线尚未明确", "求职方向与候选范围尚未明确"], "version": 1, "decision": "active", "assessment": {"outcome": "unassessed", "summary": "未取得 offer 或真实验收证据", "evidence": [], "source_ref": ""}, "history": []},
        "initial_state": {"summary": "求职方向与可接受条件尚未明确", "source_ref": source, "recorded_at": at},
        "current_state": {"summary": "求职方向与可接受条件尚未明确；尚无真实执行反馈", "source_ref": source, "recorded_at": at, "stage_id": "explore"},
        "stages": [{"id": key, "title": title, "state": "proposed", "origin": "proposed", "external": external, "depends_on": [], "block_reason": ""} for key, title, external in stages],
        "routes": [{"from": first, "to": second, "kind": "proposed_route", "note": "拟议路径，非必要前置"} for first, second in zip(["start", "explore", "materials", "outreach", "evaluate"], ["explore", "materials", "outreach", "evaluate", "accepted-suitable-offer"])] + [{"from": "outreach", "to": "explore", "kind": "feedback", "note": "按招聘反馈回看方向"}],
        "actions": [{"id": "map-existing-experience", "stage_id": "explore", "title": "梳理可用于求职的经历与已有材料", "completion_criteria": "形成一份简短经历摘要，标出已有简历/作品和目前最需要澄清的问题", "state": "proposed", "depends_on": [], "effort_minutes": 30, "block_reason": "", "why": "为筛选真实岗位与明确底线提供已有事实；30 分钟是可修订估时", "actor": "user", "source_ref": source + "；Codex 提出的近期候选行动", "priority": 10}],
        "capacities": {}, "schedules": {}, "actuals": [],
        "settings": {"defaults": {"trigger": "on_demand", "schedule": "periods_duration", "learning": "trial_then_confirm"}, "confirmed": {}, "project_overrides": {}, "observations": [], "trials": []},
        "feedback_source": {"thread_id": "synthetic-feedback-source", "status": "not_checked", "checked_at": None, "detail": "尚未核对新的求职反馈；脚本不读取聊天，需 Codex 在当日启动时核对"},
        "changes": [], "updated_at": at,
    }
    return validate(data)


def read(path=DEFAULT_DATA):
    try:
        return validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except OSError as exc:
        raise PilotError(f"来源不可用；未创建空记录：{exc}") from exc


def find(items, key):
    for item in items:
        if item["id"] == key:
            return item
    raise PilotError(f"未找到对象：{key}")


def prepare(data, request):
    validate(data)
    fields(request, ("expected_revision", "reason", "source_ref", "operations"))
    integer(request["expected_revision"], "expected_revision")
    text(request["reason"], "原因"); text(request["source_ref"], "来源")
    if request["expected_revision"] != data["revision"]:
        raise PilotError(f"版本冲突：当前 revision {data['revision']}")
    if not isinstance(request["operations"], list) or not request["operations"]:
        raise PilotError("需要明确 operations")
    changed = copy.deepcopy(data); summary = []; at = now()
    for op in request["operations"]:
        if not isinstance(op, dict) or "type" not in op:
            raise PilotError("操作需要 type")
        kind = op["type"]
        if kind == "add_action":
            fields(op, ("type", "action")); changed["actions"].append(copy.deepcopy(op["action"]))
        elif kind == "update_action":
            fields(op, ("type", "action_id", "changes"))
            edits = op["changes"]
            allowed = {"title", "completion_criteria", "state", "depends_on", "effort_minutes", "block_reason", "why", "priority", "actor", "stage_id"}
            if not isinstance(edits, dict) or not edits or set(edits) - allowed or edits.get("state") == "done":
                raise PilotError("行动修改无效；完成请用 record_feedback 保存实际结果")
            find(changed["actions"], op["action_id"]).update(copy.deepcopy(edits))
        elif kind == "update_stage":
            fields(op, ("type", "stage_id", "changes"))
            if not isinstance(op["changes"], dict) or not op["changes"] or set(op["changes"]) - {"title", "state", "origin", "external", "depends_on", "block_reason"}:
                raise PilotError("阶段修改字段无效")
            find(changed["stages"], op["stage_id"]).update(copy.deepcopy(op["changes"]))
        elif kind == "set_routes":
            fields(op, ("type", "routes")); changed["routes"] = copy.deepcopy(op["routes"])
        elif kind == "set_current_state":
            fields(op, ("type", "summary", "stage_id"))
            changed["current_state"] = {"summary": op["summary"], "stage_id": op["stage_id"], "source_ref": request["source_ref"], "recorded_at": at}
        elif kind == "set_capacity":
            fields(op, ("type", "date", "periods", "fixed")); day(op["date"])
            changed["capacities"][op["date"]] = {"periods": copy.deepcopy(op["periods"]), "fixed": copy.deepcopy(op["fixed"]), "source_ref": request["source_ref"]}
        elif kind == "set_schedule":
            fields(op, ("type", "date", "entries")); day(op["date"])
            if changed["goal"]["decision"] != "active":
                raise PilotError("目标未在推进；先明确继续")
            if not isinstance(op["entries"], list):
                raise PilotError("日程 entries 需要列表")
            available = {a["id"] for a in changed["actions"] if a["state"] == "done"}
            last_period = -1
            for entry in op["entries"]:
                fields(entry, ("action_id", "period", "minutes", "role"))
                if entry["period"] not in PERIODS or list(PERIODS).index(entry["period"]) < last_period:
                    raise PilotError("日程应按上午、下午、晚上排序，前置不能安排在后续行动之后")
                last_period = list(PERIODS).index(entry["period"])
                action = find(changed["actions"], entry["action_id"])
                if action["state"] not in {"proposed", "todo", "doing"} or not set(action["depends_on"]) <= available:
                    raise PilotError("安排含受阻/结束行动或前置未完成；可按依赖顺序安排同日行动")
                available.add(action["id"])
            changed["schedules"][op["date"]] = {"entries": copy.deepcopy(op["entries"]), "source_ref": request["source_ref"]}
        elif kind == "record_feedback":
            fields(op, ("type", "source_id", "action_id", "result", "action_state"), ("obstacle", "evidence", "actual_minutes"))
            actual = {"id": "feedback-" + hashlib.sha256(str(op["source_id"]).encode()).hexdigest()[:20], "source_id": op["source_id"], "source_ref": request["source_ref"], "action_id": op["action_id"], "result": op["result"], "action_state": op["action_state"], "obstacle": op.get("obstacle", ""), "evidence": copy.deepcopy(op.get("evidence", [])), "actual_minutes": op.get("actual_minutes"), "recorded_at": at}
            previous = next((x for x in changed["actuals"] if x["source_id"] == op["source_id"]), None)
            if previous:
                if {k: v for k, v in previous.items() if k != "recorded_at"} != {k: v for k, v in actual.items() if k != "recorded_at"}:
                    raise PilotError("同一反馈来源内容不同；请核对并使用新的修订来源身份")
                continue
            action = find(changed["actions"], op["action_id"])
            action.update(state=op["action_state"], block_reason=actual["obstacle"] if op["action_state"] == "blocked" else "")
            changed["actuals"].append(actual)
        elif kind == "revise_goal":
            fields(op, ("type", "changes"))
            edits = op["changes"]
            if not isinstance(edits, dict) or not edits or set(edits) - {"target_state", "criteria", "constraints", "unknowns"}:
                raise PilotError("目标修改字段无效")
            goal = changed["goal"]
            if any(goal[k] != v for k, v in edits.items()):
                goal["history"].append({k: copy.deepcopy(goal[k]) for k in ("version", "target_state", "criteria", "constraints", "unknowns", "assessment") } | {"reason": request["reason"], "source_ref": request["source_ref"], "revised_at": at})
                goal.update(copy.deepcopy(edits)); goal["version"] += 1
                goal["assessment"] = {"outcome": "unassessed", "summary": "定义已修订，旧结论保留在历史中", "evidence": [], "source_ref": ""}
        elif kind == "assess_goal":
            fields(op, ("type", "outcome", "summary", "evidence"))
            changed["goal"]["assessment"] = {k: copy.deepcopy(op[k]) for k in ("outcome", "summary", "evidence")} | {"source_ref": request["source_ref"]}
        elif kind == "set_decision":
            fields(op, ("type", "decision")); changed["goal"]["decision"] = op["decision"]
        elif kind == "observe":
            fields(op, ("type", "id", "text"))
            changed["settings"]["observations"].append({"id": op["id"], "text": op["text"], "source_ref": request["source_ref"], "recorded_at": at})
        elif kind == "trial_preference":
            fields(op, ("type", "id", "key", "value", "rationale"))
            changed["settings"]["trials"].append({k: copy.deepcopy(op[k]) for k in ("id", "key", "value", "rationale")} | {"source_ref": request["source_ref"], "status": "trial"})
        elif kind == "confirm_preference":
            fields(op, ("type", "trial_id", "confirmation_ref"))
            text(op["confirmation_ref"], "本人确认来源")
            trial = find(changed["settings"]["trials"], op["trial_id"])
            if trial["status"] == "rejected":
                raise PilotError("已拒绝的试调请重新提出")
            trial["status"] = "confirmed"
            changed["settings"]["confirmed"][trial["key"]] = {"value": copy.deepcopy(trial["value"]), "source_ref": op["confirmation_ref"]}
        elif kind == "reject_trial":
            fields(op, ("type", "trial_id"))
            trial = find(changed["settings"]["trials"], op["trial_id"])
            if trial["status"] == "confirmed":
                raise PilotError("已确认偏好需用 set_preference 明确修订/清除")
            trial["status"] = "rejected"
        elif kind == "set_preference":
            fields(op, ("type", "key", "value", "scope", "confirmation_ref"))
            text(op["confirmation_ref"], "本人确认来源")
            if op["scope"] not in {"confirmed", "project_overrides"}:
                raise PilotError("偏好范围无效")
            if op["value"] is None:
                changed["settings"][op["scope"]].pop(op["key"], None)
            else:
                changed["settings"][op["scope"]][op["key"]] = {"value": copy.deepcopy(op["value"]), "source_ref": op["confirmation_ref"]}
        elif kind == "check_feedback_source":
            fields(op, ("type", "status", "detail"))
            if op["status"] not in {"checked", "unavailable"}:
                raise PilotError("来源检查需要 checked/unavailable")
            changed["feedback_source"].update(status=op["status"], detail=op["detail"], checked_at=at)
        else:
            raise PilotError(f"未知操作：{kind}")
        summary.append(kind)
    validate(changed)
    if changed == data:
        return changed, []
    changed["revision"] += 1; changed["updated_at"] = at
    changed["changes"].append({"revision": changed["revision"], "reason": request["reason"], "source_ref": request["source_ref"], "at": at, "summary": summary})
    return validate(changed), summary


def apply(path, request):
    path = Path(path).resolve()
    if not path.is_file():
        raise PilotError("来源不可用；未创建空记录")
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = path.read_bytes(); data = read(path)
        changed, summary = prepare(data, request)
        if not summary:
            return {"saved": False, "revision": data["revision"], "summary": []}
        if path.read_bytes() != raw:
            raise PilotError("来源在保存期间发生变化；请重读")
        WorkspaceStore._write_atomically(path.with_suffix(path.suffix + ".bak"), data)
        WorkspaceStore._write_atomically(path, changed)
        return {"saved": True, "revision": changed["revision"], "summary": summary}


def initialize(path=DEFAULT_DATA):
    path = Path(path)
    backup = path.with_suffix(path.suffix + ".bak")
    if path.exists() or path.is_symlink() or backup.exists() or backup.is_symlink():
        raise PilotError("记录或备份已存在；拒绝覆盖/重新初始化")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    WorkspaceStore._write_atomically(path, initial_record())


def recover(path):
    # Resolve aliases before deriving the lock, backup, and preservation paths.
    # Otherwise recovery through a symlink could replace the link itself or use
    # a different lock/backup from callers using the canonical path.
    path = Path(path).resolve()
    if not path.parent.is_dir():
        raise PilotError("来源目录不可用")
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            try:
                read(path)
            except (ValueError, TypeError, KeyError):
                pass
            else:
                raise PilotError("主记录有效；不以旧备份覆盖")
        backup = read(path.with_suffix(path.suffix + ".bak"))
        if path.exists():
            raw = path.read_bytes()
            preserved = path.with_name(path.name + ".pre-recover-" + hashlib.sha256(raw).hexdigest()[:16])
            if not preserved.exists():
                preserved.write_bytes(raw); preserved.chmod(0o600)
        WorkspaceStore._write_atomically(path, backup)
    return {"revision": backup["revision"], "warning": "显式恢复了上一有效备份，可能回退最近事实；损坏前像已保留"}


def ready(data):
    if data["goal"]["decision"] != "active":
        return []
    done = {a["id"] for a in data["actions"] if a["state"] == "done"}
    return sorted((a for a in data["actions"] if a["state"] in {"proposed", "todo", "doing"} and set(a["depends_on"]) <= done), key=lambda a: (-a["priority"], a["id"]))


def effective_preferences(data):
    values, notes = {}, []
    for trial in data["settings"]["trials"]:
        if trial["status"] == "trial":
            values[trial["key"]] = trial["value"]
            notes.append(f"试调（未成为长期偏好）：{trial['key']}={trial['value']}；{trial['rationale']}")
    for scope in ("confirmed", "project_overrides"):
        values.update({k: v["value"] for k, v in data["settings"][scope].items()})
    return values, notes


def cell(value):
    return str(value).replace("|", "／").replace("\n", " ")


def day_report(data, selected_day, since_revision=None):
    validate(data); day(selected_day)
    if since_revision is not None:
        integer(since_revision, "上次阅读版本", 0)
        if since_revision > data["revision"]:
            raise PilotError("上次阅读版本高于当前源，可能发生恢复；请先核对")
    goal = data["goal"]; source = data["feedback_source"]
    lines = [f"# {data['project']['title']} · {selected_day}", "", f"记录 r{data['revision']} · 目标定义 v{goal['version']} · 保存于 {data['updated_at']}", "", f"目标：{goal['target_state']}；验收：{goal['assessment']['summary']}", f"当前：{data['current_state']['summary']}", "", "## 全景", "", f"起点：{data['initial_state']['summary']}"]
    for stage in data["stages"]:
        lines.append(f"阶段：{stage['title']}〔{STATE_LABELS[stage['state']]} · {'拟议' if stage['origin'] == 'proposed' else '已确认'}〕" + (" ← 当前位置" if stage['id'] == data['current_state'].get('stage_id') else "") + ("；涉及外部回应" if stage['external'] else "") + ("；阻碍：" + stage['block_reason'] if stage['block_reason'] else ""))
    labels = {"start": "起点", goal["id"]: "目标终点", **{s["id"]: s["title"] for s in data["stages"]}}
    for route in data["routes"]:
        lines.append(f"{'拟议路径' if route['kind'] == 'proposed_route' else '反馈回看'}：{labels[route['from']]} → {labels[route['to']]}")
    lines.extend(["", f"反馈来源：{source['status']}；核对时间：{source['checked_at'] or '尚未核对'}；{source['detail']}"])
    if goal["unknowns"]:
        lines.extend(["", "待明确：" + "；".join(goal["unknowns"])])
    if since_revision is not None:
        changes = [c for c in data["changes"] if c["revision"] > since_revision]
        lines.extend(["", f"自上次所见 r{since_revision} 的已保存变化："])
        lines.extend([f"- r{c['revision']}：{c['reason']}" for c in changes] or ["无新保存变化。"])
    elif data["changes"]:
        lines.extend(["", f"最近保存变化 r{data['changes'][-1]['revision']}：{data['changes'][-1]['reason']}"])
    preferences, notes = effective_preferences(data)
    lines.extend(["", "## 当日安排", ""])
    schedule = data["schedules"].get(selected_day)
    if schedule:
        if goal["decision"] != "active":
            lines.append("目标已暂停/结束；以下为保留的原安排，执行前需重新决定。")
        lines.extend(["| 时段 | 行动 | 预计 | 完成条件 | 当前情况 |", "|---|---|---|---|---|"])
        for entry in schedule["entries"]:
            action = find(data["actions"], entry["action_id"])
            deps = [ref for ref in action["depends_on"] if find(data["actions"], ref)["state"] != "done"]
            status = STATE_LABELS[action["state"]] + ("；前置未完成，执行前核对" if deps else "")
            if action["block_reason"]:
                status += "；" + action["block_reason"]
            lines.append("| " + " | ".join(map(cell, [PERIODS[entry["period"]] + ("·主要" if entry["role"] == "primary" else "·可选"), action["title"], f"{entry['minutes']} 分钟", action["completion_criteria"], status])) + " |")
    else:
        lines.append("尚未保存日程。" + ("当天可用时段与固定约定未知；以下是行动建议，不是已排好的日程。" if selected_day not in data["capacities"] else "已知当天净容量，可在对话中选择行动并安排。"))
        candidates = ready(data)[:3]
        for index, action in enumerate(candidates):
            estimate = f"约 {action['effort_minutes']} 分钟（可修订估时）" if action["effort_minutes"] else "估时未知"
            lines.extend(["", f"{'主要建议' if index == 0 else '可选建议'}：{action['title']}〔{STATE_LABELS[action['state']]}〕", f"执行者：{'你' if action['actor'] == 'user' else 'Codex'}；{estimate}", f"目的：{action['why']}", f"停止条件：{action['completion_criteria']}"])
            if action["effort_minutes"] and preferences.get("task_max_minutes", action["effort_minutes"]) < action["effort_minutes"]:
                lines.append(f"粒度提示：当前设置建议每段不超过 {preferences['task_max_minutes']} 分钟；先讨论拆分，不自动缩小完成条件。")
        if not candidates:
            lines.append("当前没有可开始行动；检查推进决定、前置、阻碍或新增近期行动。")
    capacity = data["capacities"].get(selected_day)
    if capacity:
        lines.extend(["", "当天净可用容量：" + "、".join(f"{PERIODS[p]} {m} 分钟" for p, m in capacity["periods"].items())])
        for fixed in capacity["fixed"]:
            lines.append(f"固定约定：{fixed['title']} · {fixed['start']} — {fixed['end']}")
    if preferences:
        lines.extend(["", "当前建议设置：" + cell(json.dumps(preferences, ensure_ascii=False))])
    lines.extend(["", *notes])
    blocked = [a for a in data["actions"] if a["state"] == "blocked"]
    if blocked:
        lines.extend(["", "阻碍：" + "；".join(a["title"] + "：" + a["block_reason"] for a in blocked)])
    lines.extend(["", "结束时直接反馈做了什么、结果如何、哪里卡住；耗时不记得可以不填。未完成行动不会自动顺延。", ""])
    return "\n".join(lines)


def overview_puml(data):
    validate(data)
    goal = data["goal"]; current = data["current_state"].get("stage_id")
    lines = ["@startuml", "title 求职全景 · 阶段结果与近期行动", "top to bottom direction", 'skinparam defaultFontName "PingFang SC"', "skinparam defaultFontSize 16", "skinparam shadowing false", "skinparam roundcorner 12", "skinparam linetype ortho", "skinparam backgroundColor #FAFCFD", "skinparam nodesep 25", "skinparam ranksep 35"]
    lines.append(f'rectangle "起点\\n{_wrapped(data["initial_state"]["summary"], 22)}" as {_alias("start")} #F1F5F9')
    for stage in data["stages"]:
        detail = "当前位置 · " if stage["id"] == current else ""
        detail += STATE_LABELS[stage["state"]] + (" · 拟议" if stage["origin"] == "proposed" else " · 已确认")
        if stage["external"]:
            detail += "\\n包含外部回应"
        if stage["block_reason"]:
            detail += "\\n阻碍：" + _wrapped(stage["block_reason"], 22)
        lines.append(f'rectangle "{_wrapped(stage["title"], 22)}\\n{detail}" as {_alias(stage["id"])} {STATE_COLORS[stage["state"]]}')
    lines.append(f'rectangle "目标终点\\n{_wrapped(goal["target_state"], 22)}\\n{_wrapped(goal["assessment"]["summary"], 22)}" as {_alias(goal["id"])} #DCFCE7')
    for route in data["routes"]:
        arrow = ".[norank].>" if route["kind"] == "feedback" else ".down.>"
        label = "反馈回看" if route["kind"] == "feedback" else "拟议路径"
        lines.append(f'{_alias(route["from"])} {arrow} {_alias(route["to"])} : {label}')
    for stage in data["stages"]:
        for dep in stage["depends_on"]:
            lines.append(f'{_alias(dep)} --> {_alias(stage["id"])} : 必要前置')
    near = [a for a in data["actions"] if a["stage_id"] == current and a["state"] not in {"done", "cancelled"}]
    if near:
        detail = "\\n\\n".join(_wrapped(a["title"], 22) + f"〔{STATE_LABELS[a['state']]}〕\\n" + _wrapped(a["block_reason"] or a["completion_criteria"], 22) for a in sorted(near, key=lambda a: -a["priority"])[:3])
        lines.extend([f'note right of {_alias(current)}', "近期行动（最多显示三项）", detail.replace("\\n", "\n"), "end note"])
    detail = "当前：" + data["current_state"]["summary"] + "；待明确：" + "；".join(goal["unknowns"])
    lines.extend([f'note right of {_alias(goal["id"])}', _wrapped(detail, 22).replace("\\n", "\n"), "end note", "legend bottom", "虚线：拟议路径/反馈回看；实线：必要前置", "路径是待检验方案；行动全完成不自动代表目标达成", f"记录 r{data['revision']} · 目标 v{goal['version']} · {data['updated_at']}", "endlegend", "@enduml"])
    return "\n".join(lines) + "\n"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def render_png(source):
    config = json.loads((ROOT / "rendering.json").read_text())
    jar = ROOT / config["plantuml"]["path"]
    if digest(jar.read_bytes()) != config["plantuml"]["sha256"]:
        raise PilotError("绘图工具与固定校验和不一致")
    java = str(ROOT / config["java"]) if "/" in config["java"] else config["java"]
    result = subprocess.run([java, "-Djava.awt.headless=true", "-DPLANTUML_SECURITY_PROFILE=SECURE", "-jar", str(jar), "-pipe", "-tpng", "-charset", "UTF-8"], input=source.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PilotError("PlantUML PNG 渲染失败：" + result.stderr.decode("utf-8", "replace")[-500:])
    return result.stdout


def render(path=DEFAULT_DATA, output=None, selected_day=None, since_revision=None):
    path = Path(path).resolve(); output = Path(output or path.parent / (path.stem + "-views")).resolve()
    selected_day = selected_day or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if output == path.parent or output == path or output in path.parents:
        raise PilotError("输出必须位于独立派生目录")
    raw = path.read_bytes(); data = read(path)
    fingerprint = digest(raw)
    # Include generator and renderer inputs, never just the data revision.
    generator = digest(Path(__file__).read_bytes() + (ROOT / "app/graphs.py").read_bytes() + (ROOT / "rendering.json").read_bytes())
    report = day_report(data, selected_day, since_revision)
    key = digest((fingerprint + generator + selected_day + str(since_revision)).encode())[:24]
    destination = output / f"r{data['revision']}-{selected_day}-{key}"
    manifest_path = destination / "manifest.json"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / ".render.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = None
        if manifest_path.is_file():
            try:
                candidate = json.loads(manifest_path.read_text())
                if candidate["source_sha256"] == fingerprint and candidate["generator_sha256"] == generator and candidate["date"] == selected_day and candidate.get("since_revision") == since_revision and set(candidate["files"]) == {"overview.puml", "overview.svg", "overview.png", "today.md"} and all(digest((destination / name).read_bytes()) == hash_value for name, hash_value in candidate["files"].items()):
                    manifest = candidate
            except (OSError, ValueError, KeyError):
                pass
        if manifest is None:
            source = overview_puml(data)
            files = {"overview.puml": source.encode(), "overview.svg": _render_svg(source), "overview.png": render_png(source), "today.md": report.encode()}
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name, content in files.items():
                (destination / name).write_bytes(content); (destination / name).chmod(0o600)
            manifest = {"schema": "life-project-pilot-view/v1", "revision": data["revision"], "goal_version": data["goal"]["version"], "date": selected_day, "since_revision": since_revision, "source_sha256": fingerprint, "generator_sha256": generator, "generated_at": now(), "files": {name: digest(content) for name, content in files.items()}}
            WorkspaceStore._write_atomically(manifest_path, manifest)
        if path.read_bytes() != raw:
            raise PilotError("生成期间源记录改变；保留上次指针，请重新生成")
        pointer = {"directory": destination.name, "manifest_sha256": digest(manifest_path.read_bytes()), "revision": data["revision"], "source_sha256": fingerprint, "date": selected_day}
        latest = output / "latest.json"
        if not latest.is_file() or json.loads(latest.read_text()) != pointer:
            WorkspaceStore._write_atomically(latest, pointer)
    return {"directory": str(destination), "overview": str(destination / "overview.png"), "svg": str(destination / "overview.svg"), "day": str(destination / "today.md"), "revision": data["revision"]}


def main():
    parser = argparse.ArgumentParser(description="Codex 使用的私人求职试用命令；不读取聊天或调用 API")
    parser.add_argument("command", choices=("init", "check", "show", "apply", "render", "recover"))
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--date", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--since-revision", type=int, help="上次实际所见版本；未知时只列最近变化，不推断已阅读")
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.data); result = {"initialized": str(args.data)}
        elif args.command == "apply":
            if args.request is None:
                raise PilotError("apply 需要 --request")
            result = apply(args.data, json.loads(args.request.read_text()))
        elif args.command == "recover":
            result = recover(args.data)
        elif args.command == "render":
            result = render(args.data, args.output, args.date, args.since_revision)
        elif args.command == "show":
            print(day_report(read(args.data), args.date, args.since_revision)); return
        else:
            data = read(args.data); result = {"valid": True, "revision": data["revision"], "goal_version": data["goal"]["version"], "feedback_status": data["feedback_source"]["status"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired, RuntimeError) as exc:
        parser.exit(1, f"未完成：{exc}\n")


if __name__ == "__main__":
    main()
