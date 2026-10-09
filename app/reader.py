"""Read-only view bundles; publish both perspectives from one validated snapshot."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

from .graphs import (STATE_LABELS, DECISION_LABELS, ASSESSMENT_LABELS, _alias, _render_svg,
                     STAGE_LABELS, _short_datetime, context_puml, flow_puml, external_flow_puml,
                     milestone_alias, current_goal, latest_assessment, goal_status, tracking_alias, validate_context)
from .live_sync import SourceSync
from .tracking import empty_tracking, load_tracking, validate_tracking
from .model import derived_view
from .project_read_model import read_sources, registered_projects as catalog_projects

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
ASSETS = ("reader.html", "reader.css", "reader.js")


def fingerprint(workspace, context, tracking=None, supplement=None):
    digest = hashlib.sha256(json.dumps([workspace, context, tracking if tracking is not None else empty_tracking(),
                                       (supplement or {}).get("fingerprint_inputs", {})], sort_keys=True, ensure_ascii=False).encode())
    for path in [ROOT / "rendering.json", Path(__file__), ROOT / "app" / "project_read_model.py", ROOT / "app" / "pilot.py", ROOT / "app" / "graphs.py", ROOT / "app" / "tracking.py", ROOT / "app" / "live_sync.py", ROOT / "app" / "execplan_progress.py", *(STATIC / name for name in ASSETS)]:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def safe_svg(raw):
    """Keep local renderer shapes, text and IDs; remove active/external content."""
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    root = ET.fromstring(raw)
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise ValueError("渲染结果不是 SVG")
    forbidden = {"script", "foreignObject", "image", "use", "a", "style", "animate", "animateTransform", "set"}
    for parent in root.iter():
        for child in list(parent):
            if child.tag.split("}")[-1] in forbidden:
                parent.remove(child)
        for key in list(parent.attrib):
            value = parent.attrib[key]
            if key.split("}")[-1].lower().startswith("on") or key.split("}")[-1] in {"href", "src"} or "url(" in value.lower():
                del parent.attrib[key]
    return ET.tostring(root, encoding="unicode")


def build_bundle(workspace, context, jar=None, tracking=None, previous=None, supplement=None, registered_projects=None):
    tracking = validate_tracking(tracking if tracking is not None else empty_tracking())
    goal = current_goal(workspace)
    project = workspace["projects"][0]
    registered_projects = registered_projects if registered_projects is not None else (supplement or {}).get("projects", [])
    validate_context(context, project["id"], tracking, registered_projects)
    sources = {"context": context_puml(workspace, context, tracking, registered_projects)}
    views = []
    ready = set(derived_view(workspace)["ready_task_ids"])
    ordered_goals = sorted(workspace["goals"], key=lambda item: item["id"] != goal["id"])
    for item in ordered_goals:
        is_current = item["id"] == goal["id"]
        key = "local-goal-" + hashlib.sha256(item["id"].encode()).hexdigest()[:20]
        flow_key, full_key = ("flow", "full") if is_current else (key + "-flow", key + "-full")
        sources[flow_key] = flow_puml(workspace, focus=True, goal_id=item["id"])
        sources[full_key] = flow_puml(workspace, goal_id=item["id"])
        tasks = [task for task in workspace["tasks"] if task["goal_id"] == item["id"]]
        if item["decision"] != "active":
            next_action = f'{DECISION_LABELS[item["decision"]]}；保留原记录，不推荐下一步'
        else:
            next_action = "、".join(task["title"] for task in tasks if task["id"] in ready) or ("当前工作进行中" if any(task["state"] == "doing" for task in tasks) else "尚无可开始的事项")
        views.append({"id": f'local:goal:{item["id"]}', "project_id": project["id"], "project_title": project["title"],
                      "goal_id": item["id"], "goal_node": _alias(item["id"]), "title": item["title"],
                      "target_state": item["target_state"], "status": f'结果：{goal_status(item)} · {DECISION_LABELS[item["decision"]]}',
                      "next": next_action, "stage_status": item["decision"], "is_current": is_current,
                      "predecessor_ids": [f'local:goal:{item["predecessor_id"]}'] if item["predecessor_id"] else [],
                      "task_count": len(tasks), "plan_id": None,
                      "external": False, "flow_key": flow_key, "full_key": full_key, "observed_at": ""})
    for external in tracking["goals"]:
        project_name = next(item["title"] for item in tracking["projects"] if item["id"] == external["project_id"])
        key = "tracking-goal-" + hashlib.sha256(external["id"].encode()).hexdigest()[:20]
        flow_key, full_key = key + "-flow", key + "-full"
        sources[flow_key] = external_flow_puml(tracking, external, focus=True)
        sources[full_key] = external_flow_puml(tracking, external)
        stage = external.get("stage_status", "unknown")
        next_action = external["next_action"]
        if stage == "planned":
            next_action = "尚在计划中；不建议立即执行。" + next_action
        views.append({"id": f'tracking:goal:{external["id"]}', "project_id": f'tracking:project:{external["project_id"]}',
                      "project_title": project_name, "goal_id": external["id"], "goal_node": tracking_alias("goal", external["id"]),
                      "title": external["title"], "target_state": external["target_state"],
                      "status": f'来源报告：{STAGE_LABELS[stage]} · {external["assessment"]}', "next": next_action,
                      "stage_status": stage, "block_reason": external["gap"] if stage == "blocked" else "", "is_current": False, "plan_id": external["plan_id"],
                      "task_count": len(external.get("milestones", [])),
                      "predecessor_ids": [f'tracking:goal:{r["to"]}' for r in tracking["relations"] if r["from"] == external["id"] and r["kind"] == "follows"],
                      "external": True, "flow_key": flow_key, "full_key": full_key, "observed_at": next((s["observed_at"] for s in external["sources"] if s["kind"] == "plan"), tracking["observed_at"])})
    renderer_version = hashlib.sha256(b"".join(p.read_bytes() for p in (ROOT / 'rendering.json', ROOT / 'app/graphs.py', Path(__file__)))).hexdigest()
    reusable = previous if previous and previous.get('renderer_version') == renderer_version else {}
    svgs = {key: reusable['svgs'][key] for key, source in sources.items() if reusable.get('sources', {}).get(key) == source and key in reusable.get('svgs', {})}
    pending = [(key, source) for key, source in sources.items() if key not in svgs]
    def render_one(item):
        return item[0], safe_svg(_render_svg(item[1], jar))
    if pending:
        # Bound JVM concurrency while shortening a cold start's eleven independent renders.
        with ThreadPoolExecutor(max_workers=2) as pool:
            svgs.update(pool.map(render_one, pending))
    titles = {task["id"]: task["title"] for task in workspace["tasks"]}
    def context_fields(project_id):
        relations = [relation for relation in context["relations"] if relation["from"] == project_id]
        areas = {item["id"]: item for item in context["areas"]}
        directions = {item["id"]: item for item in context["directions"]}
        def describe(relation, nodes):
            node = nodes[relation["to"]]
            return f'{node["title"]}（关系{("拟议" if relation["status"] == "proposed" else "已确认")}；节点{("拟议" if node["status"] == "proposed" else "已确认")}）'
        primary = [describe(r, areas) for r in relations if r["kind"] == "belongs_to"]
        support = [describe(r, areas) for r in relations if r["kind"] == "supports"]
        direct = [describe(r, directions) for r in relations if r["kind"] == "serves"]
        via_area = [describe(r, directions) for relation in relations if relation["kind"] == "belongs_to" for r in context["relations"] if r["from"] == relation["to"] and r["kind"] == "serves"]
        return [["主要领域", "\n".join(primary) or "主要领域待明确"], ["跨领域支持", "\n".join(support) or "未声明"],
                ["直接服务方向", "\n".join(direct) or "上层方向待明确"], ["经主要领域服务方向", "\n".join(via_area) or "未声明"]]
    project_detail = {"title": project["title"], "fields": [["当前目标", goal["target_state"]], ["完成条件", goal["acceptance_criteria"]], ["项目 ID", project["id"]]] + context_fields(project["id"])}
    nodes = {"PROJECT": project_detail, _alias(project["id"]): project_detail}
    for item in workspace["goals"]:
        assessment = latest_assessment(item)
        fields = [["期望结果", item["target_state"]], ["完成条件", item["acceptance_criteria"]],
                  ["结果评价", goal_status(item)], ["推进决定", DECISION_LABELS[item["decision"]]],
                  ["证据与缺口", assessment["summary"] if assessment else "尚无此版目标的评价；任务完成不会自动判定达成"],
                  ["当前证据", assessment["evidence"] if assessment else "待记录"],
                  ["评价依据", {"user_feedback": "本人反馈", "technical_check": "技术检查", "observation": "观察记录"}.get(assessment["basis"], "") if assessment else "未记录"],
                  ["ExecPlan · 实施依据", item["execplan_ref"] or "未关联"],
                  ["定义版本", str(item["definition_version"])], ["目标 ID", item["id"]]]
        if item["assessments"]:
            fields.append(["评价记录（保留旧版本）", "\n\n".join(f'v{a["definition_version"]} · {ASSESSMENT_LABELS[a["outcome"]]} · {_short_datetime(a["at"])}\n{a["summary"]}\n{a["evidence"]}' for a in item["assessments"])])
        nodes[_alias(item["id"])] = {"title": item["title"], "fields": fields, "kind": "local_goal",
                                    "state": item["decision"], "parent_id": project["id"], "is_current": item["id"] == goal["id"]}
    for task in workspace["tasks"]:
        blocks = [f'{_short_datetime(b["planned_start"])} → {_short_datetime(b["planned_end"])}' for b in workspace["time_blocks"] if b["task_id"] == task["id"]]
        logs = [f'{_short_datetime(log["actual_start"])} → {_short_datetime(log["actual_end"])}\n{log["result"]}' for log in workspace["work_logs"] if log["task_id"] == task["id"]]
        nodes[_alias(task["id"])] = {"title": task["title"], "kind": "task", "state": task["state"],
            "parent_id": f'local:goal:{task["goal_id"]}' if task["goal_id"] else project["id"],
            "depends_on": [f'local:task:{dep}' for dep in task["depends_on"]], "block_reason": task["block_reason"], "fields": [
            ["状态", STATE_LABELS[task["state"]]], ["阶段目标", next((g["title"] for g in workspace["goals"] if g["id"] == task["goal_id"]), "未关联阶段（历史或候选）")], ["完成条件", task["completion_criteria"]],
            ["前置", "\n".join(titles[dep] for dep in task["depends_on"]) or "无"],
            ["阻碍", task["block_reason"] or "未记录"], ["期限", task["due_date"] or "未设定"],
            ["估时", f'{task["effort_minutes"]} 分钟' if task["effort_minutes"] is not None else "未估计"],
            ["计划 · 上海时间", "\n".join(blocks) or "未安排"], ["实际记录 · 上海时间", "\n\n".join(logs) or "尚无记录"], ["事项 ID", task["id"]]]}
    for key, label in (("directions", "方向"), ("areas", "领域")):
        for node in context[key]:
            nodes[_alias(node["id"])] = {"title": node["title"], "fields": [["类型", label], ["确认程度", "拟议，待本人确认" if node["status"] == "proposed" else "已确认"]]}
    for external in tracking["projects"]:
        nodes[tracking_alias("project", external["id"])] = {"title": external["title"], "fields": [["管理方式", "外部只读观察；在原项目中推进"]] + context_fields(f'tracking:project:{external["id"]}') + [["来源对话", tracking["source_thread"]["title"]], ["对话入口", tracking["source_thread"]["url"]]]}
    for external in tracking["goals"]:
        nodes[tracking_alias("goal", external["id"])] = {"title": external["title"], "kind": "implementation_goal",
            "state": external.get("stage_status", "unknown"), "parent_id": f'tracking:project:{external["project_id"]}', "fields": [
            ["期望结果", external["target_state"]], ["来源报告的阶段状态", STAGE_LABELS[external.get("stage_status", "unknown")]], ["来源报告的进度", external["progress"]],
            ["来源报告的验收", external["assessment"]], ["缺口", external["gap"]], ["下一步", external["next_action"]],
            ["来源进度核对时间 · 上海", _short_datetime(next((s["observed_at"] for s in external["sources"] if s["kind"] == "plan"), tracking["observed_at"]))],
            ["同步方式", "仅展示来源报告；同步状态见页面，本地未重新验收"],
            ["计划身份", external["plan_id"]], ["当前计划路径", external["plan_ref"]],
            ["来源", "\n\n".join(f'{source["title"]}\n{source["ref"]}\n观察于 {_short_datetime(source["observed_at"])}\n' + (f'SHA-256: {source["sha256"]}' if source["sha256"] else "对话观察") for source in external["sources"])]]}
        for milestone in external.get("milestones", []):
            source = next(item for item in external["sources"] if item["ref"] == milestone["source_ref"])
            nodes[milestone_alias(external["id"], milestone["id"])] = {"title": milestone["title"], "kind": "implementation_milestone",
                "state": milestone["state"], "parent_id": f'tracking:goal:{external["id"]}',
                "depends_on": [f'tracking:milestone:{quote(external["plan_id"], safe="")}:{quote(dep, safe="")}' for dep in milestone["depends_on"]], "fields": [
                ["所属阶段", external["title"]], ["来源报告的状态", STATE_LABELS.get(milestone["state"], "未记录")],
                ["明确前置", "\n".join(milestone["depends_on"]) or "未声明"], ["说明", milestone["note"] or "未记录"],
                ["来源", f'{source["title"]}\n{source["ref"]}\n观察于 {_short_datetime(source["observed_at"])}\n' + (f'SHA-256: {source["sha256"]}' if source["sha256"] else "对话观察")],
                ["来源进度核对时间 · 上海", _short_datetime(source["observed_at"])], ["里程碑 ID", milestone["id"]]]}
    projects = [{"id": project["id"], "title": project["title"], "node": _alias(project["id"]), "external": False,
                 "unassigned_task_nodes": [_alias(task["id"]) for task in workspace["tasks"] if task["goal_id"] is None]}]
    projects += [{"id": f'tracking:project:{item["id"]}', "title": item["title"],
                  "node": tracking_alias("project", item["id"]), "external": True, "unassigned_task_nodes": []}
                 for item in tracking["projects"]]
    navigation = {}
    def locate(item_id, project_id, view_id, node_id, kind):
        navigation[item_id] = {"project_id": project_id, "view_id": view_id, "node_id": node_id, "kind": kind}
    locate(f'local:project:{project["id"]}', project["id"], None, _alias(project["id"]), "project")
    for item in workspace["goals"]:
        view_id = f'local:goal:{item["id"]}'
        locate(view_id, project["id"], view_id, _alias(item["id"]), "goal")
    for task in workspace["tasks"]:
        view_id = f'local:goal:{task["goal_id"]}' if task["goal_id"] else None
        locate(f'local:task:{task["id"]}', project["id"], view_id, _alias(task["id"]), "task")
    for item in tracking["projects"]:
        project_id = f'tracking:project:{item["id"]}'
        locate(project_id, project_id, None, tracking_alias("project", item["id"]), "project")
    for item in tracking["goals"]:
        project_id = f'tracking:project:{item["project_id"]}'
        view_id = f'tracking:goal:{item["id"]}'
        locate(view_id, project_id, view_id, tracking_alias("goal", item["id"]), "implementation_plan")
        for milestone in item.get("milestones", []):
            locate(f'tracking:milestone:{quote(item["plan_id"], safe="")}:{quote(milestone["id"], safe="")}', project_id, view_id,
                   milestone_alias(item["id"], milestone["id"]), "implementation_milestone")
    for view in views:
        view["record_kind"] = "implementation_plan" if view["external"] else "local_goal"
    for item in projects:
        active = [view for view in views if view["project_id"] == item["id"] and view["stage_status"] not in {"met", "ended", "closed", "cancelled"}]
        blocked = [view["title"] for view in active if view["stage_status"] == "blocked"]
        if item["external"]:
            observed = max((view["observed_at"] for view in active if view["observed_at"]), default=tracking["observed_at"])
            recent = f'最近来源发布：{_short_datetime(observed)}' if observed else "来源更新时间未记录"
            source_label = "已登记外部计划；来源报告"
        else:
            recent = f'本地记录 r{workspace["revision"]}；更新于 {_short_datetime(workspace.get("updated_at", ""))}'
            source_label = "Life Projects 本地记录"
            blocked += [task["title"] + "：" + task["block_reason"] for task in workspace["tasks"] if task.get("block_reason")]
        item["summary"] = {"recent_change": recent,
                           "next_step": "\n".join(f'{view["title"]}：{view["next"]}' for view in active) or "未登记下一步",
                           "blockers": "\n".join(blocked) or "未记录阶段阻碍；不表示所有事项均可开始",
                           "source_label": source_label}
    bundle = {"schema_version": 1, "presentation_version": 3, "projects": projects,
            "context": {key: context[key] for key in ("directions", "areas", "relations")}, "renderer_version": renderer_version, "source_versions": {g["plan_id"]: next((s["sha256"] for s in g["sources"] if s["kind"] == "plan"), "") for g in tracking["goals"]}, "fingerprint": fingerprint(workspace, context, tracking), "revision": workspace["revision"],
            "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"),
            "project": {"id": project["id"], "title": project["title"], "goal": goal["target_state"], "goal_node": _alias(goal["id"]), "status": views[0]["status"], "next": views[0]["next"]},
            "tracking": {"revision": tracking["revision"], "observed_at": tracking["observed_at"], "sync_status": tracking["sync_status"], "sync_message": tracking["sync_message"]},
            "views": views, "svgs": svgs, "nodes": nodes, "sources": sources, "navigation": navigation,
            "coverage": [{"id": "registered-projects", "title": "当前读取范围", "status": "partial",
                          "message": "仅包含本地项目与明确登记的来源。外部旧格式或未登记计划不在当前结构化阅读范围；不是全部人生项目。"}]}
    if supplement is not None:
        # This is a derived read projection, never a second editable ledger.
        by_id = {item["id"]: item for item in bundle["projects"]}
        for incoming in supplement.get("projects", []):
            if incoming["id"] not in by_id:
                item = dict(incoming)
                item.setdefault("external", True)
                item.setdefault("read_only", True)
                item.setdefault("unassigned_task_nodes", [])
                bundle["projects"].append(item)
                by_id[item["id"]] = item
            else:
                existing = by_id[incoming["id"]]
                # Supplementary plans belong to the same project without replacing its local Goal.
                for field, text in incoming.get("summary", {}).items():
                    current = existing.setdefault("summary", {}).get(field, "")
                    if text and text != current:
                        existing["summary"][field] = current + ("\n" if current else "") + text
        bundle["views"].extend(supplement.get("views", []))
        for key in ("nodes", "navigation", "source_versions"):
            bundle[key].update(supplement.get(key, {}))
        bundle["coverage"].extend(supplement.get("coverage", []))
        bundle["supplement"] = supplement
        bundle["fingerprint"] = fingerprint(workspace, context, tracking, supplement)
    # The primary outline is derived AFTER all explicitly registered sources merge.
    # The legacy context SVG remains a compatibility artifact, not the directory.
    bundle["reading_model"] = reading_model(bundle)
    bundle["presentation_version"] = 4
    return bundle


def reading_model(bundle):
    """One canonical, typed ownership projection for all reading modes.

    IDs and parents come from source adapters/navigation, never display prose.
    Relations such as serves/follows stay references; they do not own children.
    """
    objects = {}
    views = {view["id"]: view for view in bundle["views"]}
    projects = {project["id"]: project for project in bundle["projects"]}
    for project in projects.values():
        objects[project["id"]] = {"id": project["id"], "project_id": project["id"], "parent_id": None,
                                 "kind": "project", "title": project["title"], "node_id": project.get("node"), "children": []}
    for ident, ref in bundle["navigation"].items():
        if ref.get("kind") == "project":
            continue
        node = bundle["nodes"].get(ref["node_id"])
        if node is None or ref["project_id"] not in projects:
            continue
        view = views.get(ref.get("view_id"), {})
        root = ident in views
        parent = node.get("parent_id") or (ref["project_id"] if root else ref.get("view_id") or ref["project_id"])
        objects[ident] = {"id": ident, "project_id": ref["project_id"], "view_id": ref.get("view_id"),
                          "node_id": ref["node_id"], "kind": node.get("kind") or ref["kind"], "title": node["title"],
                          "parent_id": parent, "children": [], "state": node.get("state", view.get("stage_status", "unknown")),
                          "is_current": node.get("is_current", view.get("is_current", False) if root else False),
                          "confirmation": node.get("confirmation"), "depends_on": node.get("depends_on", []),
                          "block_reason": node.get("block_reason", view.get("block_reason", "") if root else ""), "freshness": view.get("freshness", "not_checked" if view.get("external") else "saved_record"),
                          "source_id": node.get("source_id") or view.get("source_id") or view.get("plan_id"),
                          "source_version": bundle["source_versions"].get(node.get("source_id") or view.get("source_id") or view.get("plan_id"), f'local:r{bundle["revision"]}' if not view.get("external") else ""),
                          "source_project_id": node.get("source_project_id") or view.get("source_project_id"),
                          "predecessor_ids": view.get("predecessor_ids", []) if root else []}
    for obj in objects.values():
        if obj["parent_id"] in objects:
            objects[obj["parent_id"]]["children"].append(obj["id"])
    for project in projects.values():
        roots = [v for v in bundle["views"] if v["project_id"] == project["id"] and v["record_kind"] != "source_material"]
        materials = [v["id"] for v in bundle["views"] if v["project_id"] == project["id"] and v["record_kind"] == "source_material"]
        project["reading"] = {"root_ids": [v["id"] for v in roots], "material_ids": materials,
                              "current_ids": [v["id"] for v in roots if v.get("is_current")],
                              "counts": {state: sum(v.get("stage_status", "unknown") == state for v in roots)
                                         for state in ("active", "blocked", "planned", "paused", "closed", "unknown")},
                              "business_stage_ids": [x["id"] for x in objects.values() if x["project_id"] == project["id"] and x["kind"] == "business_stage"],
                              "current_stage_id": next((x["id"] for x in objects.values() if x["project_id"] == project["id"] and x["kind"] == "business_stage" and x["is_current"]), None)}
    return {"schema": "project-reading/v1", "project_ids": list(projects), "objects": objects,
            "context": bundle["context"]}


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".reading-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def export_bundle(bundle, output):
    output = Path(output)
    for name in ("reader.css", "reader.js"):
        atomic_write(output / name, (STATIC / name).read_text())
    for key, svg in bundle["svgs"].items():
        atomic_write(output / f"{key}.svg", svg)
        atomic_write(output / f"{key}.puml", bundle["sources"][key])
    encoded = json.dumps(bundle, ensure_ascii=False).replace("<", "\\u003c")
    atomic_write(output / "bundle.json", encoded)
    shell = (STATIC / "reader.html").read_text().replace('id="initial-data" type="application/json">null', f'id="initial-data" type="application/json">{encoded}')
    atomic_write(output / "index.html", shell)
    return output / "index.html"


class ReadingViews:
    def __init__(self, store, context_path, output, tracking_path=None):
        self.store, self.context_path, self.output = store, Path(context_path), Path(output)
        self.tracking_path = Path(tracking_path) if tracking_path is not None else store.path.parent / "tracking.json"
        self.sync = SourceSync(store.path.parent / "sources.json", self.tracking_path)
        self.reader_sources_path = store.path.parent / "reader-sources.json"
        self.source_states = []
        self.lock = threading.Lock()
        self.last = None
        self.checked = False
        self.failed_key = None
        self.retry_at = 0
        # Cache identity prevents a second ledger from inheriting this case's diagrams.
        self.owner = hashlib.sha256(str(store.path.resolve()).encode()).hexdigest()
        try:
            saved = json.loads((self.output / "last-success.json").read_text())
            if saved["owner"] == self.owner:
                self.last = saved["bundle"]
        except (OSError, ValueError, KeyError):
            pass

    def snapshot(self):
        with self.lock:
            key = None
            try:
                workspace = self.store.read()
                context = json.loads(self.context_path.read_text())
                tracking, legacy_states = self.sync.refresh(load_tracking(self.tracking_path))
                previous_supplement = (self.last or {}).get("supplement")
                supplement = read_sources(self.reader_sources_path, previous=previous_supplement) if self.reader_sources_path.exists() or previous_supplement else None
                self.source_states = self._combined_states(legacy_states, supplement, tracking)
                key = fingerprint(workspace, context, tracking, supplement)
                if not self.checked or self.last is None or self.last["fingerprint"] != key:
                    if key == self.failed_key and time.monotonic() < self.retry_at:
                        return self._response(True)
                    bundle = build_bundle(workspace, context, tracking=tracking, previous=self.last, supplement=supplement, registered_projects=catalog_projects(self.reader_sources_path))
                    if supplement is not None:
                        confirmed = read_sources(self.reader_sources_path, previous=supplement)
                        if confirmed.get("fingerprint_inputs") != supplement.get("fingerprint_inputs"):
                            # A material can change while legacy graphs render. Reuse those graphs,
                            # then publish the newest independently validated source projection.
                            supplement = confirmed
                            bundle = build_bundle(workspace, context, tracking=tracking, previous=bundle, supplement=supplement, registered_projects=catalog_projects(self.reader_sources_path))
                            key = bundle["fingerprint"]
                        self.source_states = self._combined_states(legacy_states, confirmed, tracking)
                    stable = (self.sync.stable(legacy_states)
                              and self.store.read() == workspace
                              and json.loads(self.context_path.read_text()) == context
                              and load_tracking(self.tracking_path) == tracking)
                    if not stable:
                        for state in self.source_states:
                            if state['status'] == 'synced':
                                state.update(status='checking', message='生成期间来源发生变化，下一次检查将重试')
                        return self._response(True)
                    atomic_write(self.output / "last-success.json", json.dumps({"owner": self.owner, "bundle": bundle}, ensure_ascii=False))
                    self.last = bundle
                self.checked = True
                self.failed_key = None
                return self._response(False)
            except Exception:
                # A view failure must not turn a successful ledger save into a failed save.
                self.failed_key, self.retry_at = key, time.monotonic() + 10
                return self._response(True)

    @staticmethod
    def _combined_states(legacy, supplement, tracking):
        owners = {goal["plan_id"]: f'tracking:project:{goal["project_id"]}' for goal in tracking["goals"]}
        return [{**state, "project_id": owners.get(state.get("plan_id"))} for state in legacy] + (supplement or {}).get("source_states", [])

    def _response(self, stale):
        states = []
        for source in self.source_states:
            state = dict(source)
            rendered = (self.last or {}).get('source_versions', {}).get(state['plan_id'], '')
            state['rendered_version'] = rendered
            if state['status'] == 'synced' and (stale or rendered != state.get('raw_sha256')):
                state.update(status='render_failed' if stale else 'checking', message='当前图尚未对应本次来源，保留上次可靠图')
            states.append(state)
        return {"bundle": self.last, "stale": stale, "sources": states,
                "message": ("图暂未更新，正在显示上次成功生成的内容；将自动重试。" if self.last else "图暂时无法生成；将自动重试。") if stale else ""}
