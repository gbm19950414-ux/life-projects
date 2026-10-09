const $ = (selector, root = document) => root.querySelector(selector);
const h = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[ch]);
const TZ = "Asia/Shanghai";
const STATUS = {proposed:"候选",todo:"待做",doing:"执行中",blocked:"受阻",done:"完成",cancelled:"取消"};
let state = null;
let busy = false;
let weekDate = new Intl.DateTimeFormat("en-CA", {timeZone:TZ,year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());

function message(text, kind="saved") {
  $("#message").textContent = text;
  $("#save-status").className = "status-dot " + kind;
}
function editable() {
  const w = state.workspace;
  return {projects:structuredClone(w.projects),goals:structuredClone(w.goals),tasks:structuredClone(w.tasks),time_blocks:structuredClone(w.time_blocks),work_logs:structuredClone(w.work_logs)};
}
function taskName(id) { return state.workspace.tasks.find((task)=>task.id===id)?.title || "未知任务"; }
function shDate(value, options={}) {
  if (!value) return "未记录";
  return new Intl.DateTimeFormat("zh-CN",{timeZone:TZ,month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false,...options}).format(new Date(value));
}
function inputTime(value) {
  if (!value) return "";
  const parts = new Intl.DateTimeFormat("sv-SE", {timeZone:TZ,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value));
  return parts.replace(" ","T");
}
function withOffset(value) { return value ? value + ":00+08:00" : null; }
function weekDays(day) {
  const d = new Date(day + "T12:00:00+08:00");
  const weekday = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - weekday);
  return Array.from({length:7}, (_,i)=> {
    const v = new Date(d); v.setUTCDate(d.getUTCDate() + i);
    return new Intl.DateTimeFormat("en-CA",{timeZone:TZ,year:"numeric",month:"2-digit",day:"2-digit"}).format(v);
  });
}
async function load() {
  if (busy) return;
  try {
    message("正在读取本机账本…", "");
    const response = await fetch("/api/workspace",{cache:"no-store"});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "读取失败");
    state = result;
    $("#app").hidden = false;
    render();
    message(`已读取 · 第 ${state.workspace.revision} 版`);
  } catch (error) {
    message(error.message + "。请检查服务和账本。", "error");
  }
}
async function save(draft, reason="") {
  if (busy) return false;
  busy = true;
  message("正在保存…", "");
  try {
    const response = await fetch("/api/workspace", {
      method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({expected_revision:state.workspace.revision,workspace:draft,reason})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存失败");
    state = result;
    render();
    message(`已保存 · 第 ${state.workspace.revision} 版`);
    return true;
  } catch (error) {
    message(error.message + "。输入仍在页面中；可修改后重试，或点重新读取。", "error");
    return false;
  } finally {
    busy = false;
  }
}
function render() {
  const w = state.workspace, p = w.projects[0], g = w.goals.find(goal => goal.id === p.current_goal_id);
  $("#project-card").innerHTML = `<span class="small-heading">项目目标 · ${h(p.id)}</span><h2>${h(g.target_state)}</h2><p class="reason">${h(p.reason)}</p><p><strong>验收：</strong>${h(g.acceptance_criteria)}</p><details class="project-editor"><summary>调整目标与验收</summary><form id="project-form"><label>标题<input name="title" value="${h(p.title)}" required></label><label>目标<textarea name="goal" required>${h(g.target_state)}</textarea></label><label>理由<textarea name="reason">${h(p.reason)}</textarea></label><label>验收标准<textarea name="acceptance_criteria">${h(g.acceptance_criteria)}</textarea></label><label>来源引用<input name="source_ref" value="${h(p.source_ref)}"></label><label>调整原因<input name="change_reason" placeholder="这次为什么要改变目标或验收"></label><div class="actions"><button class="primary">保存项目</button></div></form></details>`;
  const tasks = [...w.tasks].sort((a,b)=>a.sort_order-b.sort_order || a.title.localeCompare(b.title));
  const ready = tasks.filter((task)=>state.derived.ready_task_ids.includes(task.id));
  const active = tasks.filter((task)=>task.state==="doing" && task.goal_id===g.id && g.decision==="active");
  const blocked = tasks.filter((task)=>task.state==="blocked");
  const visibleNext = [...active, ...ready];
  const emptyNext = tasks.every((task)=>["done","cancelled"].includes(task.state))
    ? "当前已选择的任务均已结束；有新工作时先记为候选。"
    : "目前没有可做的待办；查看候选和阻碍。";
  $("#next-card").innerHTML = `<span class="small-heading">当前与下一步</span><h2>${active.length?`${active.length} 项正在推进`:`${ready.length} 项可做`}</h2>${visibleNext.length?`<ul class="next-list">${visibleNext.slice(0,4).map((task)=>`<li><strong>${h(task.title)}</strong><small>${task.state==="doing"?"执行中 · ":"可做 · "}${h(task.completion_criteria)}</small></li>`).join("")}</ul>`:`<p class="empty">${emptyNext}</p>`}${blocked.length?`<p class="muted">另有 ${blocked.length} 项受阻。</p>`:""}`;
  $("#task-count").textContent = `${tasks.length} 项任务 · ${tasks.filter((task)=>task.state==="done").length} 项完成`;
  $("#task-list").innerHTML = tasks.map(renderTask).join("");
  renderWarnings();
  renderSelects(tasks);
  $("#week-date").value = weekDate;
  renderWeek();
  renderHistory();
}
function renderTask(task) {
  const all = state.workspace.tasks, ready = state.derived.ready_task_ids.includes(task.id);
  const blocks = state.workspace.time_blocks.filter((block)=>block.task_id===task.id);
  const logs = state.workspace.work_logs.filter((log)=>log.task_id===task.id);
  const deps = all.filter((other)=>other.id !== task.id).map((other)=>`<label><input type="checkbox" name="depends_on" value="${h(other.id)}" ${task.depends_on.includes(other.id)?"checked":""}>${h(other.title)} <small>(${h(STATUS[other.state])})</small></label>`).join("") || "<span class=\"empty\">暂无其他任务</span>";
  const badges = `<span class="badge ${h(task.state)}">${h(STATUS[task.state])}</span>${ready?`<span class="badge ready">可做</span>`:""}`;
  const related = `<details class="task-related"><summary>安排 ${blocks.length} · 实际 ${logs.length}</summary><div class="task-related-body"><strong>安排</strong>${blocks.length?blocks.map((block)=>`<p>${h(shDate(block.planned_start))} 至 ${h(shDate(block.planned_end))}</p>`).join(""):"<p>尚未安排</p>"}<strong>实际</strong>${logs.length?logs.map((log)=>`<p>${h(shDate(log.actual_start))}${log.actual_end?` 至 ${h(shDate(log.actual_end))}`:""} · ${h(log.result||log.obstacle||log.note)}</p>`).join(""):"<p>尚未记录</p>"}</div></details>`;
  return `<article class="card task-card" data-task-id="${h(task.id)}"><div class="task-main"><div><div class="task-title">${h(task.title)}</div><div class="task-sub">${h(task.stage||"未分阶段")} · ${h(task.completion_criteria||"完成条件待明确")}${task.due_date?` · 期限 ${h(task.due_date)}`:""}</div></div><div class="badges">${badges}</div></div>${task.block_reason&&task.state==="blocked"?`<div class="warning">受阻：${h(task.block_reason)}</div>`:""}${related}<details><summary>查看与调整任务</summary><form class="task-form" data-task-id="${h(task.id)}"><label>标题<input name="title" value="${h(task.title)}" required></label><label>完成条件<textarea name="completion_criteria" placeholder="选择为待做时再明确">${h(task.completion_criteria)}</textarea></label><div class="field-triple"><div><label>状态<select name="state">${Object.entries(STATUS).map(([value,label])=>`<option value="${value}" ${task.state===value?"selected":""}>${label}</option>`).join("")}</select></label></div><div><label>阶段<input name="stage" value="${h(task.stage)}" placeholder="可空"></label></div><div><label>期限<input name="due_date" type="date" value="${h(task.due_date||"")}"></label></div></div><div class="field-pair"><div><label>估时（分钟，可空）<input name="effort_minutes" type="number" min="0" value="${h(task.effort_minutes??"")}"></label></div><div><label>排序<input name="sort_order" type="number" value="${h(task.sort_order)}"></label></div></div><label>前置任务</label><div class="check-list">${deps}</div><label>来源<input name="source_ref" value="${h(task.source_ref)}"></label><label>具体阻碍（受阻时必填）<input name="block_reason" value="${h(task.block_reason)}"></label><label>本次结果（完成时必填）<textarea name="result" placeholder="记录实际做成的事"></textarea></label><label class="check-inline"><input type="checkbox" name="criteria_checked">已核对这项任务的完成条件</label><label>必要时说明原因<input name="reason" placeholder="取消、重开或解除阻碍时填写"></label><div class="actions"><button type="submit" class="primary">保存任务</button></div></form></details></article>`;
}
function renderWarnings() {
  const warnings = state.derived.warnings;
  $("#warnings").innerHTML = warnings.map((warn)=>warn.kind==="upstream_reopened" ? `<div class="warning">${h(taskName(warn.task_id))} 的前置任务重新打开了；请检查后续安排，已有实际记录保持原样。</div>` : `<div class="warning">两个时间块重叠。请检查是否需要调整；系统不会自动改期。</div>`).join("");
}
function renderSelects(tasks) {
  const options = `<option value="">选择任务</option>` + tasks.map((task)=>`<option value="${h(task.id)}">${h(task.title)}</option>`).join("");
  for (const id of ["#block-task","#log-task"]) {
    const select = $(id), previous = select.value;
    select.innerHTML = options;
    if (tasks.some((task)=>task.id===previous)) select.value=previous;
  }
  renderLogBlocks();
}
function renderLogBlocks() {
  const select = $("#log-block"), taskId = $("#log-task").value, previous = select.value;
  select.innerHTML = `<option value="">无对应安排</option>` + state.workspace.time_blocks.filter((block)=>block.task_id===taskId).map((block)=>`<option value="${h(block.id)}">${h(shDate(block.planned_start))} 至 ${h(shDate(block.planned_end))}</option>`).join("");
  if (state.workspace.time_blocks.some((block)=>block.id===previous&&block.task_id===taskId)) select.value=previous;
}
function renderWeek() {
  const days=weekDays(weekDate), all=state.workspace.time_blocks;
  const shDay=(value)=>new Intl.DateTimeFormat("en-CA",{timeZone:TZ,year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date(value));
  const today=new Intl.DateTimeFormat("en-CA",{timeZone:TZ,year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
  $("#week-grid").innerHTML=days.map((day,i)=>{
    const blocks=all.filter((block)=>shDay(block.planned_start)===day).sort((a,b)=>a.planned_start.localeCompare(b.planned_start));
    return `<div class="day ${day===today?"today":""}"><h3>${"一二三四五六日"[i]} · ${h(day.slice(5))}</h3>${blocks.length?blocks.map((block)=>`<div class="slot"><time>${h(shDate(block.planned_start,{hour:"2-digit",minute:"2-digit"}))}—${h(shDate(block.planned_end,{hour:"2-digit",minute:"2-digit"}))}</time>${h(taskName(block.task_id))}<br><button type="button" class="text-button" data-edit-block="${h(block.id)}">改期</button><button type="button" class="text-button danger" data-remove-block="${h(block.id)}">移除</button></div>`).join(""):`<p class="empty">尚未安排</p>`}</div>`;
  }).join("");
}
function renderHistory() {
  const w=state.workspace;
  const changes=w.changes.slice(-8).reverse().map((change)=>{
    const thing=change.new?.title || change.old?.title || (change.entity==="time_block"?"时间安排":change.entity==="work_log"?"执行记录":"项目");
    return `<div class="history-item"><strong>${h(thing)}</strong> · ${h(change.reason)}<small>${h(shDate(change.at))} · ${h(change.entity)}</small></div>`;
  });
  const logs=w.work_logs.slice(-5).reverse().map((log)=>`<div class="history-item"><strong>${h(taskName(log.task_id))}</strong> · ${h(log.result||log.obstacle||log.note)}<small>实际时间：${h(shDate(log.actual_start))}${log.actual_end?` 至 ${h(shDate(log.actual_end))}`:""}</small></div>`);
  $("#history-card").innerHTML=`<h3>实际记录</h3>${logs.join("")||"<p class=\"empty\">尚未记录实际。</p>"}<h3 style="margin-top:18px">最近变更</h3>${changes.join("")||"<p class=\"empty\">尚无变更。</p>"}`;
}

$("#reload").addEventListener("click",load);
$("#capture-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); const title=$("#capture-title").value.trim(); if(!title)return;
  const draft=editable(); draft.tasks.push({title}); if(await save(draft)) $("#capture-form").reset();
});
$("#project-card").addEventListener("submit",async(event)=>{
  if(event.target.id!=="project-form")return; event.preventDefault();
  const form=event.target, data=new FormData(form), draft=editable();
  for(const field of ["title","reason","source_ref"])draft.projects[0][field]=String(data.get(field)||"").trim();
  const goal = draft.goals.find(item=>item.id===draft.projects[0].current_goal_id);
  goal.target_state=String(data.get("goal")||"").trim();
  goal.acceptance_criteria=String(data.get("acceptance_criteria")||"").trim();
  await save(draft,String(data.get("change_reason")||""));
});
$("#task-list").addEventListener("submit",async(event)=>{
  const form=event.target.closest("form.task-form"); if(!form)return; event.preventDefault();
  const data=new FormData(form), draft=editable();
  const task=draft.tasks.find((item)=>item.id===form.dataset.taskId), old=state.workspace.tasks.find((item)=>item.id===task.id);
  task.title=String(data.get("title")||"").trim();
  task.completion_criteria=String(data.get("completion_criteria")||"").trim();
  task.state=String(data.get("state"));
  if(old.state==="proposed" && task.state!=="proposed") task.goal_id=draft.projects[0].current_goal_id;
  task.stage=String(data.get("stage")||"").trim();
  task.due_date=data.get("due_date")||null;
  task.effort_minutes=data.get("effort_minutes")===""?null:Number(data.get("effort_minutes"));
  task.sort_order=Number(data.get("sort_order"));
  task.depends_on=data.getAll("depends_on");
  task.source_ref=String(data.get("source_ref")||"").trim();
  task.block_reason=String(data.get("block_reason")||"").trim();
  const result=String(data.get("result")||"").trim();
  const checked=data.get("criteria_checked")==="on";
  if(task.state==="done"&&old.state!=="done"&&(!result||!checked)){
    message("完成任务前，请写结果并勾选已核对完成条件。","error"); return;
  }
  if(result)draft.work_logs.push({task_id:task.id,result,criteria_checked:checked});
  await save(draft,String(data.get("reason")||"").trim());
});
$("#block-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); const data=new FormData(event.target), draft=editable();
  const block={task_id:String(data.get("task_id")),planned_start:withOffset(data.get("planned_start")),planned_end:withOffset(data.get("planned_end"))};
  const id=String(data.get("id")||"");
  if(id){const entry=draft.time_blocks.find((item)=>item.id===id);if(entry)Object.assign(entry,block);}
  else draft.time_blocks.push(block);
  if(await save(draft,id?"调整计划时间":"预留任务时间")){event.target.reset();event.target.elements.id.value="";$("#block-cancel-edit").hidden=true;}
});
$("#block-cancel-edit").addEventListener("click",()=>{ $("#block-form").reset();$("#block-form [name=id]").value="";$("#block-cancel-edit").hidden=true; });
$("#week-grid").addEventListener("click",async(event)=>{
  const edit=event.target.closest("[data-edit-block]");
  if(edit){const block=state.workspace.time_blocks.find((item)=>item.id===edit.dataset.editBlock);const form=$("#block-form");form.elements.id.value=block.id;form.elements.task_id.value=block.task_id;form.elements.planned_start.value=inputTime(block.planned_start);form.elements.planned_end.value=inputTime(block.planned_end);$("#block-cancel-edit").hidden=false;form.scrollIntoView({behavior:"smooth",block:"center"});return;}
  const remove=event.target.closest("[data-remove-block]");if(remove){
    const draft=editable(), blockId=remove.dataset.removeBlock;
    draft.time_blocks=draft.time_blocks.filter((item)=>item.id!==blockId);
    for(const log of draft.work_logs)if(log.block_id===blockId)log.block_id=null;
    await save(draft,"移除计划时间；保留实际记录并解除时间块关联");
  }
});
$("#log-form").addEventListener("submit",async(event)=>{
  event.preventDefault();const data=new FormData(event.target),draft=editable();
  draft.work_logs.push({task_id:String(data.get("task_id")),block_id:data.get("block_id")||null,actual_start:withOffset(data.get("actual_start")),actual_end:withOffset(data.get("actual_end")),result:String(data.get("result")||"").trim(),obstacle:String(data.get("obstacle")||"").trim(),evidence:String(data.get("evidence")||"").trim(),note:"",criteria_checked:false});
  if(await save(draft,"记录实际执行"))event.target.reset();
});
$("#log-task").addEventListener("change",renderLogBlocks);
$("#week-date").addEventListener("change",(event)=>{if(event.target.value){weekDate=event.target.value;renderWeek();}});
$("#this-week").addEventListener("click",()=>{weekDate=new Intl.DateTimeFormat("en-CA",{timeZone:TZ,year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());$("#week-date").value=weekDate;renderWeek();});
load();
