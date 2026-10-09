"""Read-only, reproducible views of the current project ledger."""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .model import ValidationError, derived_view, validate_workspace
from .tracking import empty_tracking, load_tracking, validate_tracking


STATE_LABELS = {
    "proposed": "候选", "todo": "待做", "doing": "进行中",
    "blocked": "受阻", "done": "完成", "cancelled": "取消",
}
STATE_COLORS = {
    "proposed": "#F1F5F9", "todo": "#E0F2FE", "doing": "#DBEAFE",
    "blocked": "#FEE2E2", "done": "#DCFCE7", "cancelled": "#E5E7EB",
}
STAGE_LABELS = {"planned": "计划中", "active": "推进中", "paused": "已暂停", "blocked": "受阻", "closed": "已结束", "unknown": "状态未记录"}
ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
RELATIONS = {"belongs_to", "serves", "supports"}


def _puml(value):
    """Keep user text inside one PlantUML label, never in diagram syntax."""
    return str(value).replace("\\", "\\\\").replace('"', "'").replace("\r", " ").replace("\n", " ").replace("<", "‹").replace(">", "›").replace("@", "＠")


def _alias(value):
    return "N" + value.encode("utf-8").hex()


def _wrapped(value, width=25):
    text = _puml(value)
    return "\\n".join(text[index:index + width] for index in range(0, len(text), width))


def validate_context(context, project_id, tracking=None, registered_projects=()):
    if not isinstance(context, dict) or set(context) != {"schema_version", "project_id", "directions", "areas", "relations"}:
        raise ValidationError("方向说明字段无效")
    if type(context["schema_version"]) is not int or context["schema_version"] not in {1, 2} or context["project_id"] != project_id:
        raise ValidationError("方向说明版本或项目引用无效")
    nodes = {project_id: "project"}
    if context["schema_version"] == 2:
        tracking = validate_tracking(tracking if tracking is not None else empty_tracking())
        for project in tracking["projects"]:
            nodes[f'tracking:project:{project["id"]}'] = "project"
    if context["schema_version"] == 2:
        for project in registered_projects:
            nodes[project["id"]] = "project"
    for key, kind in (("directions", "direction"), ("areas", "area")):
        items = context[key]
        if not isinstance(items, list) or len(items) > 100:
            raise ValidationError(f"{key} 必须是列表")
        for node in items:
            if not isinstance(node, dict) or set(node) != {"id", "title", "status"}:
                raise ValidationError(f"{key} 节点字段无效")
            if not isinstance(node["id"], str) or not ID.fullmatch(node["id"]) or node["id"] in nodes:
                raise ValidationError(f"{key} 节点 ID 无效或重复")
            if not isinstance(node["title"], str) or not node["title"].strip() or len(node["title"]) > 120:
                raise ValidationError(f"{key} 节点标题无效")
            if not isinstance(node["status"], str) or node["status"] not in {"proposed", "confirmed"}:
                raise ValidationError(f"{key} 节点状态无效")
            nodes[node["id"]] = kind
    if not isinstance(context["relations"], list) or len(context["relations"]) > 300:
        raise ValidationError("方向关系必须是列表")
    seen, primary_areas = set(), set()
    for relation in context["relations"]:
        if not isinstance(relation, dict) or set(relation) != {"from", "to", "kind", "status"}:
            raise ValidationError("方向关系字段无效")
        source, target, kind = relation["from"], relation["to"], relation["kind"]
        if not isinstance(source, str) or not isinstance(target, str) or not isinstance(kind, str) or source not in nodes or target not in nodes or source == target or kind not in RELATIONS:
            raise ValidationError("方向关系引用或类型无效")
        if kind == "belongs_to" and nodes[target] != "area":
            raise ValidationError("归属目标必须是领域")
        if kind == "belongs_to" and context["schema_version"] == 2 and nodes[source] != "project":
            raise ValidationError("归属起点必须是项目")
        if kind == "belongs_to" and context["schema_version"] == 2:
            if source in primary_areas:
                raise ValidationError("项目只能有一个主要领域")
            primary_areas.add(source)
        if kind == "serves" and nodes[target] != "direction":
            raise ValidationError("服务目标必须是方向")
        if kind == "serves" and context["schema_version"] == 2 and nodes[source] not in {"project", "area"}:
            raise ValidationError("服务起点必须是项目或领域")
        if kind == "supports" and (context["schema_version"] != 2 or nodes[source] != "project" or nodes[target] != "area"):
            raise ValidationError("支持关系必须从项目指向领域")
        if nodes[source] == "direction":
            raise ValidationError("方向不能作为关系起点")
        if not isinstance(relation["status"], str) or relation["status"] not in {"proposed", "confirmed"}:
            raise ValidationError("方向关系状态无效")
        edge = (source, target, kind)
        if edge in seen:
            raise ValidationError("方向关系重复")
        seen.add(edge)
    return context


ASSESSMENT_LABELS = {"unassessed": "未评价", "met": "已达成", "not_met": "未达成", "insufficient_evidence": "证据不足"}
DECISION_LABELS = {"active": "继续推进", "paused": "暂停", "closed": "本阶段结束", "unknown": "历史决定未记载"}


def current_goal(workspace):
    goal_id = workspace["projects"][0]["current_goal_id"]
    return next(goal for goal in workspace["goals"] if goal["id"] == goal_id)


def latest_assessment(goal):
    return next((a for a in reversed(goal["assessments"]) if a["definition_version"] == goal["definition_version"]), None)


def goal_status(goal):
    assessment = latest_assessment(goal)
    return ASSESSMENT_LABELS[assessment["outcome"] if assessment else "unassessed"]


def tracking_alias(kind, value):
    return _alias("tracking:" + kind + ":" + value)


def milestone_alias(goal_id, milestone_id):
    return _alias("tracking:milestone:" + json.dumps([goal_id, milestone_id], ensure_ascii=False, separators=(",", ":")))


def _brief(value, width=22):
    value = str(value)
    return _puml(value[:width] + ("…" if len(value) > width else ""))


def context_puml(workspace, context, tracking=None, registered_projects=()):
    validate_workspace(workspace)
    project = workspace["projects"][0]
    tracking = validate_tracking(tracking if tracking is not None else empty_tracking())
    validate_context(context, project["id"], tracking, registered_projects)
    def short_label(value, width=12):
        value = str(value)
        if value.isascii() and len(value) <= 18:
            return _puml(value)
        clipped = value[:width * 2] + ("…" if len(value) > width * 2 else "")
        return _wrapped(clipped, width)

    def stack(aliases):
        for first, second in zip(aliases, aliases[1:]):
            # PlantUML transposes axes in left-to-right mode: right stacks vertically.
            lines.append(f'{first} -[hidden]right-> {second}')

    project_ids = [project["id"]] + [f'tracking:project:{item["id"]}' for item in tracking["projects"]]
    additional = [item for item in registered_projects if item["id"] not in project_ids]
    project_ids += [item["id"] for item in additional]
    def project_alias(project_id):
        return tracking_alias("project", project_id.split(":", 2)[2]) if project_id.startswith("tracking:project:") else _alias(project_id)

    def unknown_lines(project_id):
        relations = context["relations"]
        primary = [rel["to"] for rel in relations if rel["from"] == project_id and rel["kind"] == "belongs_to"]
        has_direction = any(rel["from"] == project_id and rel["kind"] == "serves" for rel in relations) or any(rel["from"] in primary and rel["kind"] == "serves" for rel in relations)
        lines = []
        if not primary:
            lines.append("领域待明确")
        if not has_direction:
            lines.append("方向待明确")
        return "".join("\\n" + line for line in lines)

    lines = ["@startuml", "title 方向与归属 · 项目及阶段", "left to right direction",
             "skinparam shadowing false", "skinparam defaultFontName SansSerif", "skinparam defaultFontSize 16",
             "skinparam roundcorner 14", "skinparam linetype ortho", "skinparam nodesep 6", "skinparam ranksep 6",
             "legend bottom", "关系标注（拟议）：待确认", "接续不表示前置", "外部计划：只读观察", "endlegend"]
    lines.append('package "方向" as DIRECTIONS {')
    direction_aliases = []
    for node in context["directions"]:
        alias = _alias(node["id"])
        direction_aliases.append(alias)
        lines.append(f'rectangle "{short_label(node["title"])}\\n{("拟议" if node["status"] == "proposed" else "已确认")}" as {alias} #EDE9FE')
    stack(direction_aliases)
    lines.append("}")
    lines.append('package "领域" as AREAS {')
    area_aliases = []
    for node in context["areas"]:
        alias = _alias(node["id"])
        area_aliases.append(alias)
        lines.append(f'rectangle "{short_label(node["title"])}\\n{("拟议" if node["status"] == "proposed" else "已确认")}" as {alias} #FEF3C7')
    stack(area_aliases)
    lines.append("}")
    lines.append('package "项目" as PROJECTS {')
    lines.append(f'rectangle "{short_label(project["title"])}{unknown_lines(project["id"])}" as {_alias(project["id"])} #DBEAFE')
    for external in tracking["projects"]:
        lines.append(f'rectangle "{short_label(external["title"])}\\n外部只读{unknown_lines(f"tracking:project:{external["id"]}")}" as {tracking_alias("project", external["id"])} #DBEAFE')
    for item in additional:
        lines.append(f'rectangle "{short_label(item["title"])}\\n来源事实只读{unknown_lines(item["id"])}" as {project_alias(item["id"])} #DBEAFE')
    stack([project_alias(project_id) for project_id in project_ids])
    lines.append("}")
    lines.append('package "阶段目标" as GOALS {')
    goal_aliases = []
    for goal in workspace["goals"]:
        tag = "当前" if goal["id"] == project["current_goal_id"] else "历史"
        alias = _alias(goal["id"])
        goal_aliases.append(alias)
        lines.append(f'rectangle "{short_label(goal["title"])}\\n{tag} · {goal_status(goal)}" as {alias} #DCFCE7')
    for goal in tracking["goals"]:
        stage = goal.get("stage_status", "unknown")
        alias = tracking_alias("goal", goal["id"])
        goal_aliases.append(alias)
        lines.append(f'rectangle "{short_label(goal["title"])}\\n外部只读 · {STAGE_LABELS[stage]}" as {alias} #F1F5F9')
    stack(goal_aliases)
    lines.append("}")
    lines.extend(["DIRECTIONS -[hidden]down-> AREAS", "AREAS -[hidden]down-> PROJECTS", "PROJECTS -[hidden]down-> GOALS"])
    for rel in context["relations"]:
        arrow = "-[norank]->" if rel["status"] == "confirmed" else ".[norank].>"
        label = {"belongs_to": "归属", "serves": "服务于", "supports": "支持"}[rel["kind"]]
        if rel["status"] == "proposed":
            label += "（拟议）"
        source = project_alias(rel["from"]) if rel["from"] in project_ids else _alias(rel["from"])
        lines.append(f'{source} {arrow} {_alias(rel["to"])} : {label}')
    for goal in workspace["goals"]:
        lines.append(f'{_alias(goal["id"])} -[norank]-> {_alias(project["id"])} : 归属')
        if goal["predecessor_id"]:
            lines.append(f'{_alias(goal["id"])} .[norank]down.> {_alias(goal["predecessor_id"])} : 接续（非前置）')
    for goal in tracking["goals"]:
        lines.append(f'{tracking_alias("goal", goal["id"])} -[norank]-> {tracking_alias("project", goal["project_id"])} : 归属')
    for rel in tracking["relations"]:
        label = "接续（非前置）" if rel["kind"] == "follows" else "依赖于"
        arrow = "-[norank]down->" if rel["status"] == "confirmed" else ".[norank]down.>"
        if rel["status"] == "proposed":
            label += "（拟议）"
        lines.append(f'{tracking_alias("goal", rel["from"])} {arrow} {tracking_alias("goal", rel["to"])} : {label}')
    if tracking["goals"]:
        lines.append(f'caption 外部只读观察于 {_short_datetime(tracking["observed_at"])}（上海）；来源报告，不代表独立验收')
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def _short_datetime(value):
    if not value:
        return "时间未记录"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")


def flow_puml(workspace, focus=False, goal_id=None):
    validate_workspace(workspace)
    project = workspace["projects"][0]
    goal = current_goal(workspace) if goal_id is None else next(item for item in workspace["goals"] if item["id"] == goal_id)
    derived = derived_view(workspace)
    ready = set(derived["ready_task_ids"])
    lines = ["@startuml", "title 目标推进", "top to bottom direction",
             "skinparam shadowing false", "skinparam defaultFontName SansSerif", "skinparam defaultFontSize 18",
             "skinparam roundcorner 14",
             f'rectangle "项目：{_puml(project["title"])}" as PROJECT #DBEAFE']
    tasks = sorted(workspace["tasks"], key=lambda task: (task["sort_order"], task["id"]))
    goal_tasks = [task for task in tasks if task["goal_id"] == goal["id"]]
    if goal_id is not None:
        # Other goals are not children; retain only explicitly referenced prerequisites.
        members = {task["id"] for task in goal_tasks}
        references = {dep for task in goal_tasks for dep in task["depends_on"]}
        tasks = [task for task in tasks if task["id"] in members | references]
    all_tasks = tasks
    if focus and (goal_id is None or goal["decision"] != "closed"):
        active = [task for task in goal_tasks if task["state"] not in {"done", "cancelled"}]
        visible = {task["id"] for task in active} | {dep for task in active for dep in task["depends_on"]}
        tasks = [task for task in tasks if task["id"] in visible]
    if focus:
        lines = [line for line in lines if " as PROJECT " not in line]
    goal_label = "\\n".join(["阶段终点：" + _wrapped(goal["title"], 18), _wrapped(goal["target_state"], 18),
                                  f'结果：{goal_status(goal)} · {DECISION_LABELS[goal["decision"]]}'])
    lines.append(f'rectangle "{goal_label}" as {_alias(goal["id"])} #EDE9FE')
    visible_ids = {task["id"] for task in tasks}
    hidden_count = len(all_tasks) - len(tasks)
    if hidden_count:
        hidden_candidates = sum(task["state"] == "proposed" and task["id"] not in visible_ids for task in all_tasks)
        caption = f"已收起 {hidden_count} 项其他事项（展开全部查看）"
        if hidden_candidates:
            caption += f"；其中候选 {hidden_candidates} 项"
        lines.append("caption " + caption)
    blocks = workspace["time_blocks"]
    for task in tasks:
        task_blocks = sorted((block for block in blocks if block["task_id"] == task["id"]), key=lambda block: block["planned_start"])
        planned = "；".join(_short_datetime(block["planned_start"]) for block in task_blocks) if task_blocks else "未安排"
        detail = [_wrapped(task["title"], 18), _puml(f'状态：{STATE_LABELS[task["state"]]}' + (" · 下一步可做" if task["id"] in ready else ""))]
        if goal_id is not None and task["goal_id"] != goal["id"]:
            owner = next((item["title"] for item in workspace["goals"] if item["id"] == task["goal_id"]), "未关联阶段")
            detail.append(_wrapped("关联前置（不属本目标）：" + owner, 18))
        if task["block_reason"] and task["state"] == "blocked":
            detail.append(_wrapped("阻碍：" + task["block_reason"]))
        if task["due_date"]:
            detail.append(_puml("期限：" + task["due_date"]))
        if not focus:
            detail.append(_puml("计划：" + planned))
        label = "\\n".join(detail)
        lines.append(f'rectangle "{label}" as {_alias(task["id"])} {STATE_COLORS[task["state"]]}')
    roots = [task for task in tasks if not task["depends_on"]]
    for task in ([] if focus else roots):
        label = "候选记录（未承诺）" if task["state"] == "proposed" else "起点"
        lines.append(f'PROJECT --> {_alias(task["id"])} : {label}')
    for task in tasks:
        for predecessor in task["depends_on"]:
            if predecessor not in visible_ids:
                continue
            lines.append(f'{_alias(predecessor)} --> {_alias(task["id"])} : 前置')
    successors = {dep for task in goal_tasks for dep in task["depends_on"]}
    leaves = [task for task in tasks if task["goal_id"] == goal["id"] and task["id"] not in successors and task["state"] != "cancelled"]
    for task in leaves:
        lines.append(f'{_alias(task["id"])} ..> {_alias(goal["id"])} : 支持结果（需另行评价）')
    if goal["decision"] != "active":
        lines.append(f'note bottom of {_alias(goal["id"])} : {DECISION_LABELS[goal["decision"]]}；任务状态保留，不推荐下一步')
    elif not goal_tasks:
        lines.append(f'note bottom of {_alias(goal["id"])} : 尚未规划推进事项')
    elif not tasks:
        lines.append(f'note bottom of {_alias(goal["id"])} : 当前路径已无待办；请依据证据评价目标，任务完成不等于目标达成')
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def external_flow_puml(tracking, goal, focus=False):
    """Render only one observed external stage, without deriving task facts from prose."""
    project = next(item for item in tracking["projects"] if item["id"] == goal["project_id"])
    milestones = goal.get("milestones", [])
    stage = goal.get("stage_status", "unknown")
    lines = ["@startuml", f'title {_brief(goal["title"])} · 外部只读推进', "top to bottom direction",
             "skinparam shadowing false", "skinparam defaultFontName SansSerif", "skinparam defaultFontSize 18",
             "skinparam roundcorner 14"]
    if not focus:
        lines.append(f'rectangle "项目：{_brief(project["title"])}\\n外部只读" as {tracking_alias("project", project["id"])} #DBEAFE')
    goal_node = tracking_alias("goal", goal["id"])
    lines.append(f'rectangle "阶段终点：{_brief(goal["title"])}\\n{_wrapped(goal["target_state"], 22)}\\n{STAGE_LABELS[stage]} · 来源报告" as {goal_node} #EDE9FE')
    if not focus:
        lines.append(f'{tracking_alias("project", project["id"])} ..> {goal_node} : 阶段目标')
    if not milestones:
        lines.append(f'note bottom of {goal_node} : 来源未记录结构化里程碑；不从文字进度推断前置')
    else:
        visible = milestones
        if focus and stage == "active":
            active = {item["id"] for item in milestones if item["state"] not in {"done", "cancelled"}}
            dependencies = {dep for item in milestones if item["id"] in active for dep in item["depends_on"]}
            visible = [item for item in milestones if item["id"] in active | dependencies]
            if not visible:
                visible = milestones
        visible_ids = {item["id"] for item in visible}
        successors = {dep for item in visible for dep in item["depends_on"]}
        for item in visible:
            node = milestone_alias(goal["id"], item["id"])
            lines.append(f'rectangle "{_brief(item["title"])}\\n{STATE_LABELS.get(item["state"], "未记录")}" as {node} {STATE_COLORS.get(item["state"], "#F1F5F9")}')
        for item in visible:
            for dep in item["depends_on"]:
                if dep in visible_ids:
                    lines.append(f'{milestone_alias(goal["id"], dep)} --> {milestone_alias(goal["id"], item["id"])} : 来源明确前置')
        for item in visible:
            if item["id"] not in successors and item["state"] != "cancelled":
                lines.append(f'{milestone_alias(goal["id"], item["id"])} ..> {goal_node} : 支持结果（需另行评价）')
    if stage == "closed":
        successors = [item for item in tracking["relations"] if item["kind"] == "follows" and item["status"] == "confirmed" and item["to"] == goal["id"]]
        if successors:
            next_title = next(item["title"] for item in tracking["goals"] if item["id"] == successors[0]["from"])
            lines.append(f'note right of {goal_node} : 此阶段已结束；接续阶段见 {_brief(next_title)}')
        else:
            lines.append(f'note right of {goal_node} : 此阶段已结束；接续关系未登记，请查看来源下一步')
    elif stage == "planned":
        lines.append(f'note right of {goal_node} : 尚在计划中；不建议立即执行')
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def _render_svg(source, jar=None):
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "rendering.json").read_text(encoding="utf-8"))
    selected = Path(jar) if jar is not None else root / config["plantuml"]["path"]
    if not selected.is_file():
        raise FileNotFoundError("本地绘图工具暂不可用")
    if jar is None and hashlib.sha256(selected.read_bytes()).hexdigest() != config["plantuml"]["sha256"]:
        raise RuntimeError("绘图工具与声明版本不一致")
    java = str(root / config["java"]) if "/" in config["java"] else config["java"]
    result = subprocess.run([java, "-Djava.awt.headless=true", "-DPLANTUML_SECURITY_PROFILE=SECURE", "-jar", str(selected), "-pipe", "-tsvg", "-charset", "UTF-8"],
                            input=source.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    if result.returncode or b"<svg" not in result.stdout or b"Syntax Error" in result.stdout:
        raise RuntimeError("PlantUML 渲染失败：" + result.stderr.decode("utf-8", "replace")[-1000:])
    return result.stdout


def generate(workspace, context, output, jar=None, tracking=None, registered_projects=()):
    from .reader import build_bundle, export_bundle
    bundle = build_bundle(workspace, context, jar, tracking=tracking, registered_projects=registered_projects)
    return export_bundle(bundle, Path(output))


def main():
    parser = argparse.ArgumentParser(description="Generate read-only project views")
    parser.add_argument("--data", default="data/workspace.json")
    parser.add_argument("--context", default="data/context.json")
    parser.add_argument("--output", default="views/current")
    parser.add_argument("--tracking", default=None)
    parser.add_argument("--plantuml", default=None, help="Optional explicit renderer override; defaults to rendering.json")
    args = parser.parse_args()
    workspace = json.loads(Path(args.data).read_text(encoding="utf-8"))
    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    tracking = load_tracking(args.tracking or Path(args.data).parent / "tracking.json")
    from .project_read_model import registered_projects
    catalog = registered_projects(Path(args.context).parent / "reader-sources.json")
    print(generate(workspace, context, args.output, Path(args.plantuml) if args.plantuml else None, tracking=tracking, registered_projects=catalog).resolve())


if __name__ == "__main__":
    main()
