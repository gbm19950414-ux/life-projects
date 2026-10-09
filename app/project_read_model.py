"""Bounded, read-only projections of explicitly registered business sources.

No source is initialized, synchronized, rendered or saved here. ``projections``
can be retained by the reader as a derived cache; it is never a business ledger.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from . import execplan_progress, pilot

MAX_SOURCE_BYTES = 2_000_000
MAX_CONFIG_BYTES = 64_000
MAX_SOURCES = 64
MAX_EXCERPT_CHARS = 20_000
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
STATE_LABELS = {"proposed": "候选", "todo": "待做", "doing": "进行中", "blocked": "受阻", "done": "完成", "cancelled": "取消", "unknown": "未知"}
DECISION_LABELS = {"active": "推进中", "planned": "计划中", "paused": "已暂停", "blocked": "受阻", "closed": "已结束", "unknown": "未知"}


class SourceReadError(ValueError):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bounded(path, limit):
    with Path(path).open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise SourceReadError(f"来源超过 {limit} bytes 读取上限")
    return raw


def registered_projects(config_path):
    """Read only the explicit catalog, even when a business source is offline."""
    path = Path(config_path)
    if not path.exists():
        return []
    config = _validate_config(json.loads(_bounded(path, MAX_CONFIG_BYTES)))
    projects = {}
    for spec in config["sources"]:
        projects[spec["project_id"]] = {"id": spec["project_id"], "title": spec["project_title"]}
    return list(projects.values())


def _read_source(spec):
    root = Path(spec["root"])
    relative = Path(spec["path"])
    if not root.is_absolute() or relative.is_absolute() or ".." in relative.parts:
        raise SourceReadError("来源必须是登记绝对根内的相对路径")
    root = root.resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise SourceReadError("来源路径或符号链接越出登记范围")
    expected = ".json" if spec["kind"] == "pilot" else ".md"
    if relative.suffix.lower() != expected:
        raise SourceReadError(f"此适配器只读取 {expected} 文件")
    return _bounded(path, MAX_SOURCE_BYTES), str(path)


def _validate_config(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "sources", "not_connected"} or value["schema_version"] != 1:
        raise SourceReadError("阅读来源登记格式无效")
    if not isinstance(value["sources"], list) or len(value["sources"]) > MAX_SOURCES:
        raise SourceReadError("来源登记必须为有界列表")
    ids, project_titles = set(), {}
    for item in value["sources"]:
        if not isinstance(item, dict) or set(item) != {"id", "kind", "project_id", "project_title", "title", "root", "path"}:
            raise SourceReadError("来源登记字段无效")
        if any(not isinstance(item[key], str) or not item[key].strip() for key in item):
            raise SourceReadError("来源登记字段需要非空文本")
        if not ID.fullmatch(item["id"]) or not ID.fullmatch(item["project_id"]) or item["id"] in ids:
            raise SourceReadError("来源或项目身份无效／重复")
        if item["kind"] not in {"pilot", "execplan", "markdown"}:
            raise SourceReadError("来源适配器不受支持")
        old = project_titles.setdefault(item["project_id"], item["project_title"])
        if old != item["project_title"]:
            raise SourceReadError("同一项目标题不一致")
        ids.add(item["id"])
    if not isinstance(value["not_connected"], list) or len(value["not_connected"]) > MAX_SOURCES:
        raise SourceReadError("未接入范围需要有界列表")
    for item in value["not_connected"]:
        if not isinstance(item, dict) or set(item) != {"id", "title", "reason"} or any(not isinstance(x, str) or not x.strip() for x in item.values()):
            raise SourceReadError("未接入范围字段无效")
        if not ID.fullmatch(item["id"]) or item["id"] in ids:
            raise SourceReadError("未接入范围身份无效／重复")
        ids.add(item["id"])
    return value


def _identity(spec):
    return _sha(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode())


def _base(spec, raw, path):
    return {"source_id": spec["id"], "source_identity": _identity(spec), "raw_sha256": _sha(raw),
            "source_path": path, "source_revision": None, "source_updated_at": None,
            "project_id": spec["project_id"], "project_title": spec["project_title"],
            "kind": spec["kind"], "views": [], "nodes": {}, "navigation": {},
            "summary": {"recent_change": "未记录", "next_step": "待明确", "blockers": "未记录", "source_label": spec["title"]}}


def _node(projection, view_id, node_id, kind, title, fields, **extra):
    projection["nodes"][node_id] = {"title": title, "fields": fields, "kind": kind,
                                      "source_id": projection["source_id"], **extra}
    projection["navigation"][node_id] = {"project_id": projection["project_id"], "view_id": view_id, "node_id": node_id, "kind": kind}
    return node_id


def _source_fields(projection):
    return [["来源", projection["source_path"]], ["来源 SHA-256", projection["raw_sha256"]],
            ["来源版本", str(projection["source_revision"]) if projection["source_revision"] is not None else "无结构化修订号；以内容 SHA-256 标识"],
            ["来源保存时间", projection["source_updated_at"] or "未提供结构化时间"]]


def _view(projection, view_id, node_id, title, target, status, next_step, stage_status, kind, details, task_count=0):
    view = {"id": view_id, "project_id": projection["project_id"], "project_title": projection["project_title"],
            "goal_id": node_id, "goal_node": node_id, "title": title, "target_state": target,
            "status": status, "next": next_step, "stage_status": stage_status, "is_current": False,
            "predecessor_ids": [], "task_count": task_count, "plan_id": projection["source_id"],
            "external": True, "flow_key": None, "full_key": None, "observed_at": projection["source_updated_at"] or "",
            "source_id": projection["source_id"], "record_kind": kind, "detail_node_ids": details,
            "freshness": "synced"}
    projection["views"].append(view)
    projection["navigation"][view_id] = {"project_id": projection["project_id"], "view_id": view_id, "node_id": node_id, "kind": kind}


def _pilot(spec, raw, path):
    data = pilot.validate(json.loads(raw.decode("utf-8")))
    result = _base(spec, raw, path)
    result.update(source_revision=data["revision"], source_updated_at=data["updated_at"])
    prefix = f"pilot:{spec['project_id']}"
    goal = data["goal"]
    view_id = f"{prefix}:goal:{goal['id']}"
    goal_id = view_id
    details = [_node(result, view_id, goal_id, "business_goal", goal["target_state"], [
        ["事实类别", "业务正式记录；目标达成独立于行动完成"], ["源内项目 ID", data["project"]["id"]],
        ["目标定义版本", str(goal["version"])], ["推进决定", DECISION_LABELS[goal["decision"]]],
        ["当前情况", data["current_state"]["summary"]], ["完成条件", "\n".join(goal["criteria"])],
        ["验收", goal["assessment"]["summary"]], ["验收证据", "\n".join(goal["assessment"]["evidence"]) or "未记录"],
        ["待明确", "\n".join(goal["unknowns"]) or "未记录"], ["容量与安排", "按具体日期读取；此总览不创建安排，不推断当天容量"],
        ["反馈来源核对", f"{data['feedback_source']['status']}：{data['feedback_source']['detail']}"], *_source_fields(result)
    ], state=goal["decision"], assessment_outcome=goal["assessment"]["outcome"],
        parent_id=spec["project_id"], source_project_id=data["project"]["id"], is_current=True)]
    for stage in data["stages"]:
        node_id = f"{prefix}:stage:{stage['id']}"
        details.append(_node(result, view_id, node_id, "business_stage", stage["title"], [
            ["阶段状态", STATE_LABELS[stage["state"]]], ["确认程度", "拟议" if stage["origin"] == "proposed" else "已确认"],
            ["当前位置", "是" if data["current_state"].get("stage_id") == stage["id"] else "否"],
            ["外部回应", "包含外部回应" if stage["external"] else "未标记外部回应"],
            ["必要前置", "、".join(stage["depends_on"]) or "未声明"], ["阻碍", stage["block_reason"] or "未记录"], *_source_fields(result)
        ], state=stage["state"], confirmation=stage["origin"], parent_id=goal_id,
            is_current=data["current_state"].get("stage_id") == stage["id"],
            depends_on=[f"{prefix}:stage:{ident}" for ident in stage["depends_on"]],
            block_reason=stage["block_reason"]))
    stages = {stage["id"]: stage["title"] for stage in data["stages"]}
    for action in data["actions"]:
        node_id = f"{prefix}:action:{action['id']}"
        actuals = [item for item in data["actuals"] if item["action_id"] == action["id"]]
        actual_text = "\n\n".join(f"{item['recorded_at']} · {item['result']}\n阻碍：{item['obstacle'] or '未记录'}\n来源：{item['source_ref']}\n证据：{'；'.join(item['evidence']) or '未记录'}" for item in actuals) or "尚无实际反馈"
        details.append(_node(result, view_id, node_id, "business_action", action["title"], [
            ["状态", STATE_LABELS[action["state"]]], ["所属阶段", stages[action["stage_id"]]], ["完成条件", action["completion_criteria"]],
            ["理由", action["why"]], ["执行者", "本人" if action["actor"] == "user" else "Codex"],
            ["估时", f"{action['effort_minutes']} 分钟（可修订）" if action["effort_minutes"] is not None else "未知"],
            ["必要前置", "、".join(action["depends_on"]) or "未声明"], ["阻碍", action["block_reason"] or "未记录"],
            ["行动来源", action["source_ref"]], ["实际反馈", actual_text], *_source_fields(result)
        ], state=action["state"], parent_id=f"{prefix}:stage:{action['stage_id']}",
            depends_on=[f"{prefix}:action:{ident}" for ident in action["depends_on"]],
            block_reason=action["block_reason"]))
    candidates = pilot.ready(data)
    next_step = candidates[0]["title"] if candidates else "正式记录未列出可开始行动"
    blockers = "；".join(x["block_reason"] for x in [*data["stages"], *data["actions"]] if x["block_reason"]) or "未记录明确阻碍；其他条件见目标待明确项"
    recent = data["changes"][-1]["reason"] if data["changes"] else data["current_state"]["summary"]
    result["summary"] = {"recent_change": f"正式记录 r{data['revision']}：{recent}", "next_step": f"正式记录候选：{next_step}（不是已保存日程；另核对相关材料）", "blockers": blockers,
                         "source_label": f"{spec['title']} · r{data['revision']}／目标 v{goal['version']}"}
    _view(result, view_id, goal_id, data["project"]["title"], goal["target_state"], f"{DECISION_LABELS[goal['decision']]} · {goal['assessment']['summary']}",
          result["summary"]["next_step"], goal["decision"], "pilot_goal", details, len(data["actions"]))
    result["views"][-1].update(is_current=True, current_stage_id=f"{prefix}:stage:{data['current_state']['stage_id']}" if data["current_state"].get("stage_id") else None,
                               source_project_id=data["project"]["id"])
    return result


def _execplan(spec, raw, path):
    checked = execplan_progress.inspect(raw.decode("utf-8"))
    if checked["status"] != "synced":
        raise SourceReadError("计划尚未有效发布；保留上一可靠版本" if checked["status"] == "pending" else "旧格式计划尚未明确迁移")
    data = checked["record"]
    if data.get("project_id", spec["project_id"]) != spec["project_id"]:
        raise SourceReadError("计划声明的项目身份与登记不一致")
    result = _base(spec, raw, path)
    result.update(source_revision=checked["source_version"], source_updated_at=data["updated_at"], business_plan_id=data["plan_id"])
    view_id = f"execplan:{data['plan_id']}"
    details = [_node(result, view_id, view_id, "implementation_goal", data["title"], [
        ["事实类别", "来源实施计划；技术完成、用途验收与业务行动分别判断"], ["期望结果", data["objective"]],
        ["阶段状态", DECISION_LABELS[data["execution_status"]]], ["来源验收", data["assessment"]["summary"]],
        ["来源验收证据", "\n".join(data["assessment"]["evidence"]) or "未记录"], ["下一步", data["next_action"]],
        ["计划身份", data["plan_id"]], *_source_fields(result)
    ], state=data["execution_status"], assessment_outcome=data["assessment"]["outcome"], parent_id=spec["project_id"])]
    for milestone in data["milestones"]:
        node_id = f"{view_id}:milestone:{milestone['id']}"
        details.append(_node(result, view_id, node_id, "implementation_milestone", milestone["title"], [
            ["来源报告状态", STATE_LABELS[milestone["state"]]], ["必要前置", "、".join(milestone["depends_on"]) or "未声明"],
            ["来源证据", "\n".join(milestone["evidence"]) or "未记录"], ["验收边界", "本里程碑状态不替代业务行动或本人用途验收"], *_source_fields(result)
        ], state=milestone["state"], parent_id=view_id,
            depends_on=[f"{view_id}:milestone:{ident}" for ident in milestone["depends_on"]]))
    result["summary"] = {"recent_change": data["assessment"]["summary"], "next_step": data["next_action"],
                         "blockers": data.get("gap") or "以来源验收范围为限；未作独立用途验收", "source_label": f"{spec['title']} · {data['plan_id']}"}
    _view(result, view_id, view_id, data["title"], data["objective"], f"来源报告：{DECISION_LABELS[data['execution_status']]} · {data['assessment']['summary']}",
          data["next_action"], data["execution_status"], "implementation_plan", details, len(data["milestones"]))
    return result


def _markdown(spec, raw, path):
    text = raw.decode("utf-8")
    result = _base(spec, raw, path)
    lines = text.splitlines()
    title = next((line.lstrip("# ").strip() for line in lines if line.startswith("# ")), spec["title"])
    excerpt, used = [], 0
    for number, line in enumerate(lines, 1):
        rendered = f"{number}: {line}"
        if used + len(rendered) + 1 > MAX_EXCERPT_CHARS:
            break
        excerpt.append(rendered)
        used += len(rendered) + 1
    if not excerpt and lines:
        excerpt = ["1: " + lines[0][:MAX_EXCERPT_CHARS - 3]]
    truncated = len(excerpt) < len(lines) or (lines and len(lines[0]) > MAX_EXCERPT_CHARS - 3)
    view_id = f"material:{spec['project_id']}:{spec['id']}"
    fields = [["事实类别", "已保存材料原文；不是新确认的行动状态"], ["对齐状态", "尚未核对材料各项内容与正式行动的对应；不据此自动标记完成、批准或长期偏好"],
              ["摘录范围", f"第 1–{len(excerpt)} 行／共 {len(lines)} 行" + ("；超出展示上限，余文未展示" if truncated else "；已展示全文")],
              ["材料原文（按行）", "\n".join(excerpt)], *_source_fields(result)]
    _node(result, view_id, view_id, "source_material", title, fields, reconciliation="not_evaluated", excerpt_truncated=bool(truncated))
    result["summary"] = {"recent_change": f"相关材料：{title} · {result['raw_sha256'][:12]}（展开阅读原文）", "next_step": "核对材料与正式行动的关系，不自动改行动状态", "blockers": "材料中的建议、待确认与历史成品按原文区分", "source_label": spec["title"]}
    _view(result, view_id, view_id, title, "相关保存材料", "材料已保存 · 与行动状态对应尚未核对", "展开原文及其版本边界", "unknown", "source_material", [view_id])
    return result


ADAPTERS = {"pilot": _pilot, "execplan": _execplan, "markdown": _markdown}


def _assemble(projections, states, not_connected, fingerprints):
    result = {"projects": [], "views": [], "nodes": {}, "navigation": {}, "source_states": states,
              "coverage": [], "fingerprint_inputs": fingerprints, "source_versions": {}, "projections": projections}
    by_project = {}
    for source_id, projection in projections.items():
        state = next(s for s in states if s["plan_id"] == source_id)
        stale = state["status"] != "synced"
        project = by_project.setdefault(projection["project_id"], {"id": projection["project_id"], "title": projection["project_title"],
            "node": f"read-project:{projection['project_id']}", "external": True, "read_only": True, "unassigned_task_nodes": [],
            "summary": {}, "material_updates": [], "source_ids": []})
        project["source_ids"].append(source_id)
        if projection["kind"] == "markdown":
            project["material_updates"].append(projection["summary"]["recent_change"] + ("〔上次可靠内容〕" if stale else ""))
        else:
            project["summary"] = copy.deepcopy(projection["summary"])
            if stale:
                project["summary"]["source_label"] += " · 上次可靠版本，本次未核对成功"
        for original in projection["views"]:
            view = copy.deepcopy(original)
            view["freshness"] = state["status"]
            if stale:
                view["status"] = "上次可靠内容 · " + view["status"]
            result["views"].append(view)
        for node_id, original in projection["nodes"].items():
            node = copy.deepcopy(original)
            node["fields"] = [["本次来源核对", "当前读取通过" if not stale else "未通过；以下为上次可靠内容：" + state["message"]], *node["fields"]]
            result["nodes"][node_id] = node
        result["navigation"].update(copy.deepcopy(projection["navigation"]))
        result["source_versions"][source_id] = projection["raw_sha256"]
    for state in states:
        if state["project_id"]:
            project = by_project.setdefault(state["project_id"], {"id": state["project_id"], "title": state.get("project_title", state["project_id"]),
                "node": f"read-project:{state['project_id']}", "external": True, "read_only": True, "unassigned_task_nodes": [],
                "summary": {"recent_change": "来源不可用；无可靠内容", "next_step": "恢复来源后重读", "blockers": state["message"], "source_label": state["title"]}, "material_updates": [], "source_ids": []})
            if state["plan_id"] not in project["source_ids"]:
                project["source_ids"].append(state["plan_id"])
        result["coverage"].append({"id": state["plan_id"], "title": state["title"], "project_id": state["project_id"],
                                   "status": "connected" if state["status"] == "synced" else "partial", "message": state["message"] or "已核对本次登记来源"})
    for project in by_project.values():
        if not project["summary"]:
            project["summary"] = {"recent_change": "已读取相关材料；无结构化业务状态", "next_step": "展开材料核对", "blockers": "材料不替代正式行动记录", "source_label": "相关材料"}
        project["freshness"] = "synced" if all(s["status"] == "synced" for s in states if s["project_id"] == project["id"]) else "partial"
        result["projects"].append(project)
        project_node = project["node"]
        result["nodes"][project_node] = {"title": project["title"], "kind": "project", "fields": [
            ["读取范围", "仅显式登记的来源；不代表本机全部项目或全部业务"],
            ["来源状态", "本轮登记来源均已核对" if project["freshness"] == "synced" else "部分来源未核对成功；见逐来源提示"],
            ["正式来源摘要", project["summary"]["recent_change"]],
            ["下一步", project["summary"]["next_step"]], ["阻碍", project["summary"]["blockers"]],
            ["相关材料", "\n".join(project["material_updates"]) or "此入口未登记相关材料"],
            ["已登记来源", "、".join(project["source_ids"])]]}
        result["navigation"][project_node] = {"project_id": project["id"], "view_id": None, "node_id": project_node, "kind": "project"}
    result["coverage"].extend({**item, "status": "not_connected", "message": item["reason"]} for item in not_connected)
    return result


def read_sources(config_path, *, previous=None):
    """Read only the configured files, keeping per-source reliable projections.

    ``previous`` is this function's last result, never an alternate business source.
    No paths from it are opened. Changing a registered identity invalidates fallback.
    """
    checked = _now()
    old = (previous or {}).get("projections", {})
    projections, states, fingerprints = {}, [], {}
    try:
        config_raw = _bounded(config_path, MAX_CONFIG_BYTES)
        config = _validate_config(json.loads(config_raw.decode("utf-8")))
        fingerprints["registry"] = _sha(config_raw)
    except (OSError, ValueError, TypeError) as exc:
        state = {"plan_id": "reader-registry", "title": "补充阅读来源登记", "status": "unavailable", "message": str(exc),
                 "raw_sha256": "", "rendered_version": "", "checked_at": checked, "project_id": ""}
        retained = copy.deepcopy(old)
        states = [state]
        fingerprints = {"registry_error": str(exc)}
        prior_states = {item["plan_id"]: item for item in (previous or {}).get("source_states", [])}
        for source_id, projection in retained.items():
            prior = prior_states.get(source_id, {})
            states.append({"plan_id": source_id, "title": prior.get("title", source_id), "status": "unavailable",
                           "message": "来源登记不可用；显示上次可靠内容，未重读业务源", "raw_sha256": "",
                           "rendered_version": projection["raw_sha256"], "checked_at": checked,
                           "project_id": projection["project_id"], "project_title": projection["project_title"]})
            fingerprints[source_id] = {"status": "unavailable", "displayed_sha256": projection["raw_sha256"]}
        omitted = [{"id": item["id"], "title": item["title"], "reason": item.get("reason", item["message"])}
                   for item in (previous or {}).get("coverage", []) if item["status"] == "not_connected"]
        return _assemble(retained, states, omitted, fingerprints)
    for spec in config["sources"]:
        source_id = spec["id"]
        state = {"plan_id": source_id, "title": spec["title"], "status": "unavailable", "message": "", "raw_sha256": "",
                 "rendered_version": "", "checked_at": checked, "project_id": spec["project_id"], "project_title": spec["project_title"]}
        current = None
        try:
            raw, path = _read_source(spec)
            state["raw_sha256"] = _sha(raw)
            current = ADAPTERS[spec["kind"]](spec, raw, path)
            after, _ = _read_source(spec)
            if after != raw:
                state.update(status="checking", message="读取期间来源发生变化；保留上一可靠版本")
            else:
                projections[source_id] = current
                state.update(status="synced", source_revision=current["source_revision"], source_updated_at=current["source_updated_at"])
        except OSError as exc:
            state["message"] = f"来源不可用：{exc}"
        except (ValueError, KeyError, TypeError) as exc:
            state.update(status="pending", message=f"来源未通过校验：{exc}")
        if source_id not in projections:
            cached = old.get(source_id)
            if isinstance(cached, dict) and cached.get("source_identity") == _identity(spec):
                projections[source_id] = copy.deepcopy(cached)
                state["message"] += "；显示上次可靠版本"
        if source_id in projections:
            state["rendered_version"] = projections[source_id]["raw_sha256"]
        fingerprints[source_id] = {"config": _identity(spec), "raw_sha256": state["raw_sha256"], "displayed_sha256": state["rendered_version"], "status": state["status"], "message": state["message"]}
        states.append(state)
    # Check the complete explicit source set after all projections were read.
    # A later material read can overlap a change to an earlier business record;
    # two adjacent reads per file alone do not detect that cross-source race.
    by_id = {state["plan_id"]: state for state in states}
    for spec in config["sources"]:
        source_id = spec["id"]
        state = by_id[source_id]
        if state["status"] != "synced":
            continue
        try:
            final_raw, _ = _read_source(spec)
            final_sha = _sha(final_raw)
            if final_sha == state["raw_sha256"]:
                continue
            state.update(status="checking", raw_sha256=final_sha,
                         message="整轮读取结束核对发现来源已变化；保留上一可靠版本")
        except OSError as exc:
            state.update(status="unavailable", message=f"整轮读取结束时来源不可用：{exc}")
        except (ValueError, KeyError, TypeError) as exc:
            state.update(status="pending", message=f"整轮读取结束时来源未通过校验：{exc}")
        # Never promote the earlier bytes to a reliable cache when the complete
        # read window already showed them to have changed. No retries or writes.
        projections.pop(source_id, None)
        cached = old.get(source_id)
        if isinstance(cached, dict) and cached.get("source_identity") == _identity(spec):
            projections[source_id] = copy.deepcopy(cached)
        state["rendered_version"] = projections.get(source_id, {}).get("raw_sha256", "")
        state.pop("source_revision", None)
        state.pop("source_updated_at", None)
        fingerprints[source_id].update(raw_sha256=state["raw_sha256"], displayed_sha256=state["rendered_version"],
                                       status=state["status"], message=state["message"])
    # A changing registry cannot silently redirect a half-read set of sources.
    try:
        stable = _bounded(config_path, MAX_CONFIG_BYTES) == config_raw
    except (OSError, ValueError):
        stable = False
    if not stable:
        for state in states:
            if state["status"] == "synced":
                state.update(status="checking", message="读取期间来源登记改变；下次重读")
        projections = {spec["id"]: copy.deepcopy(old[spec["id"]]) for spec in config["sources"] if spec["id"] in old and old[spec["id"]].get("source_identity") == _identity(spec)}
        for state in states:
            state["rendered_version"] = projections.get(state["plan_id"], {}).get("raw_sha256", "")
            fingerprints[state["plan_id"]].update(status=state["status"], message=state["message"], displayed_sha256=state["rendered_version"])
    return _assemble(projections, states, config["not_connected"], fingerprints)
