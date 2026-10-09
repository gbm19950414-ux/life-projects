"use strict";
// Shared with the small Node regression suite; these helpers never read or write data.
const ReaderNavigation = (() => {
  const contextKeys = ["mode", "group", "direction", "scope", "q"];
  function context(route={}) { return Object.fromEntries(contextKeys.filter(k=>route[k]).map(k=>[k,route[k]])); }
  function parse(hash) {
    const p = new URLSearchParams(String(hash || "").replace(/^#/, ""));
    const route = {project:p.get("project")||"",view:p.get("view")||"",item:p.get("item")||""};
    for(const k of contextKeys) if(p.get(k)) route[k]=p.get(k);
    return route;
  }
  function format(route={}) {
    const p=new URLSearchParams();
    for(const k of [...contextKeys,"project","view","item"]) if(route[k]) p.set(k,route[k]);
    return p.size?"#"+p.toString():"";
  }
  function resolve(data, requested={}) {
    const ctx=context(requested), route={...ctx,project:requested.project||"",view:requested.view||"",item:requested.item||""};
    if(ctx.mode && !["overview","structure"].includes(ctx.mode)) delete ctx.mode,delete route.mode;
    if(ctx.group && !["directions","areas"].includes(ctx.group)) delete ctx.group,delete route.group;
    let filterError="";
    if(ctx.direction && !(data.context?.directions||[]).some(d=>d.id===ctx.direction)) {delete ctx.direction;delete route.direction;filterError="原方向筛选已不可用，已清除筛选。";}
    const allViews=data.views||[], allProjects=data.projects||Array.from(new Map(allViews.map(v=>[v.project_id,{id:v.project_id,title:v.project_title}])).values()), navigation=data.navigation||{};
    const item=route.item&&Object.hasOwn(navigation,route.item)?navigation[route.item]:null;
    const requestedView=allViews.find(v=>v.id===route.view);
    if(!route.project) route.project=item?.project_id||requestedView?.project_id||"";
    const project=allProjects.find(p=>p.id===route.project);
    if(route.project&&!project) return {route:{...ctx},error:"此项目不在当前读取范围中。已返回项目概览。"};
    if(route.view&&(!requestedView||requestedView.project_id!==route.project)) return {route:{...ctx,project:route.project},project,error:"该阶段或材料入口已不可用，或不属于此项目。"};
    if(route.item){
      const itemView=item?.view_id?allViews.find(v=>v.id===item.view_id):null;
      if(!item||!data.nodes||!Object.hasOwn(data.nodes,item.node_id)||item.project_id!==route.project||(route.view&&item.view_id!==route.view)||(item.view_id&&(!itemView||itemView.project_id!==route.project))) return {route:{...ctx,project:route.project,view:route.view},project,view:requestedView,error:"原事项已不可用，或链接的项目／阶段不匹配。保留所属入口供继续查看。"};
      route.view=item.view_id||"";return {route,project,view:itemView,item,node_id:item.node_id,error:filterError};
    }
    return {route,project,view:requestedView,error:filterError};
  }
  function forNode(data,nodeId,projectId,viewId){
    const matches=Object.entries(data.navigation||{}).filter(([,r])=>r.node_id===nodeId);
    const m=matches.find(([,r])=>r.project_id===projectId&&r.view_id===viewId)||matches.find(([,r])=>r.project_id===projectId)||(matches.length===1?matches[0]:null);
    return m?{project:m[1].project_id,view:m[1].view_id||"",item:m[0]}:null;
  }
  function parent(route={}){const c=context(route);if(route.item)return {...c,project:route.project,view:route.view||""};if(route.view)return {...c,project:route.project};return c;}
  function stageLabel(s){return ({active:"推进中",planned:"计划中",paused:"已暂停",blocked:"受阻",closed:"已结束",unknown:"状态待明确"})[s]||"状态待明确";}
  function itemLabel(s){return ({proposed:"候选",todo:"待做",doing:"进行中",blocked:"受阻",done:"完成",cancelled:"取消",unknown:"未知"})[s]||stageLabel(s);}
  function relations(data,id){
    const c=data.context||{areas:[],directions:[],relations:[]}, own=c.relations.filter(r=>r.from===id), membership=own.find(r=>r.kind==="belongs_to");
    return {membership,direct:own.filter(r=>r.kind==="serves"),via:membership?c.relations.filter(r=>r.from===membership.to&&r.kind==="serves").map(r=>({...r,via_area:membership.to,membership_status:membership.status})):[],supports:own.filter(r=>r.kind==="supports")};
  }
  function appearanceKey(scope,id){return scope?JSON.stringify([scope,id]):id;}
  function structureGroups(data,grouping="directions"){
    const c=data.context||{areas:[],directions:[],relations:[]}, all=data.projects||[];
    const byArea=items=>{
      const groups=c.areas.map(area=>({area,projects:items.filter(p=>relations(data,p.id).membership?.to===area.id)}));
      const known=new Set(c.areas.map(a=>a.id));
      const unknown=items.filter(p=>!known.has(relations(data,p.id).membership?.to));
      if(unknown.length)groups.push({area:null,projects:unknown});
      return groups.filter(g=>g.projects.length);
    };
    if(grouping==="areas")return [{id:"areas",direction:null,areas:byArea(all),project_ids:all.map(p=>p.id)}];
    const linked=new Set(),groups=c.directions.map(direction=>{
      const items=all.filter(p=>{const r=relations(data,p.id);return [...r.direct,...r.via].some(x=>x.to===direction.id);});
      items.forEach(p=>linked.add(p.id));
      return {id:appearanceKey("direction",direction.id),direction,areas:byArea(items),project_ids:items.map(p=>p.id)};
    });
    const unknown=all.filter(p=>!linked.has(p.id));
    if(unknown.length)groups.push({id:"no-direction",direction:null,areas:byArea(unknown),project_ids:unknown.map(p=>p.id)});
    return groups;
  }
  function summary(data,project){
    const rows=(data.views||[]).filter(v=>v.project_id===project.id&&!["source_material","markdown","material"].includes(v.record_kind));
    const counts=Object.fromEntries(["active","blocked","planned","paused","closed","unknown"].map(s=>[s,rows.filter(v=>v.stage_status===s).length]));
    const objects=data.reading_model?.objects||{}, current=objects[project.reading?.current_stage_id];
    let text=current?`当前：${current.title}`:"";
    const local=rows.find(v=>v.is_current&&v.record_kind==="local_goal");
    if(!text&&local)text=`当前目标：${local.title}`+(rows.some(v=>v.record_kind==="implementation_plan")?`；另有 ${rows.filter(v=>v.record_kind==="implementation_plan").length} 项实施计划`:"");
    if(!text)text=[["active","在推进"],["blocked","受阻"],["planned","计划中"],["paused","暂停"]].filter(([s])=>counts[s]).map(([s,label])=>`${counts[s]} 项${label}`).join("，");
    if(!text)text=rows.length?(counts.closed===rows.length?"当前登记阶段已结束":"当前内容待核对"):"尚无可靠阶段记录";
    return {text,counts,roots:rows,materials:(data.views||[]).filter(v=>v.project_id===project.id&&["source_material","markdown","material"].includes(v.record_kind)),businessStages:(project.reading?.business_stage_ids||[]).map(id=>objects[id]).filter(Boolean)};
  }
  function kindLabel(kind){return ({project:"项目",local_goal:"阶段目标",business_goal:"业务目标",business_stage:"业务阶段",business_action:"行动",task:"事项",implementation_goal:"实施阶段",implementation_plan:"实施计划",implementation_milestone:"里程碑",source_material:"材料"})[kind]||"记录";}
  function partition(items,preview=false){
    const rank={active:0,blocked:1,planned:2,paused:3,unknown:4,closed:5};
    const ordered=[...items].sort((a,b)=>(rank[a.stage_status]??4)-(rank[b.stage_status]??4)||Number(Boolean(b.is_current))-Number(Boolean(a.is_current))||a.id.localeCompare(b.id));
    const current=ordered.filter(v=>v.stage_status!=="closed");
    return {visible:preview?current.slice(0,3):current,remaining:preview?current.slice(3):[],history:ordered.filter(v=>v.stage_status==="closed")};
  }
  function path(data,id){const objects=data.reading_model?.objects||{}, chain=[],seen=new Set();let n=objects[id];while(n&&!seen.has(n.id)){seen.add(n.id);chain.unshift(n);n=objects[n.parent_id];}return chain;}
  function search(data,q){const value=String(q||"").trim().toLocaleLowerCase();if(!value)return [];return Object.values(data.reading_model?.objects||{}).filter(o=>(o.title+" "+o.id+" "+kindLabel(o.kind)).toLocaleLowerCase().includes(value)).map(o=>({...o,path:path(data,o.id)}));}
  return {parse,format,resolve,forNode,parent,stageLabel,itemLabel,context,relations,structureGroups,appearanceKey,summary,partition,kindLabel,path,search};
})();
if (typeof module !== "undefined" && module.exports) module.exports = ReaderNavigation;
if (typeof document !== "undefined") {
const $ = id => document.getElementById(id);
const STORAGE = "life-projects.nested-reader.v1";
const statusLabels = {synced:"已同步", checking:"检查中", pending:"待同步", unavailable:"来源不可用", render_failed:"生成失败"};
let bundle = null, sourceStates = [], pending = false, serial = 0;
let routeState = null, renderedHash = null;
const routePositions = new Map();

let prefs = {open:{}, graphs:{}, unassigned:{}, initialized:false};
try { const saved = JSON.parse(localStorage.getItem(STORAGE)); if (saved && typeof saved.open === "object" && saved.open && typeof saved.graphs === "object" && saved.graphs) prefs = {...prefs,...saved}; } catch (_) { /* Storage is optional. */ }
if (!prefs.unassigned || typeof prefs.unassigned !== "object") prefs.unassigned = {};
if(prefs.presentation!==4){prefs.mode=prefs.open.context?"structure":"overview";prefs.open.context=false;prefs.presentation=4;}
const graphs = new Map();
function savePrefs() { try { localStorage.setItem(STORAGE, JSON.stringify(prefs)); } catch (_) { /* Reading still works in private/full storage. */ } }
function el(tag, cls, text) { const node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; }
function button(text, action, token) { const b = el("button", "", text); b.type = "button"; b.onclick = action; if (token) b.dataset.focus = token; return b; }
function formatTime(value) { if (!value) return "未记录"; const d = new Date(value); return Number.isNaN(d.valueOf()) ? value : d.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false}); }
function views() { return bundle?.views || []; }
function projects() {
  if (bundle?.projects) return bundle.projects;
  return Array.from(new Map(views().map(v => [v.project_id,{id:v.project_id,title:v.project_title,external:v.external,unassigned_task_nodes:[]}])).values());
}
function nodeId(group) { const value = group?.getAttribute("data-qualified-name") || "", tail = value.split(".").pop(); return bundle?.nodes?.[value] ? value : (bundle?.nodes?.[tail] ? tail : null); }
function badge(text, proposed=false) { return el("span", `badge${proposed ? " proposed" : ""}`, text); }
function disclosure(key, title, cls, defaultOpen=false) {
  const d = el("details", `branch ${cls}`), summary = el("summary"), copy = el("span", "summary-copy"), body = el("div", "branch-body");
  d.dataset.branch = key; summary.dataset.focus = `summary:${key}`; copy.append(el("span", "summary-title", title)); summary.append(copy); d.append(summary, body);
  const open = Object.hasOwn(prefs.open,key) ? Boolean(prefs.open[key]) : (!prefs.initialized && defaultOpen);
  prefs.open[key] = open; d.open = open;
  d.addEventListener("toggle", () => {
    if (!d.isConnected) return;
    prefs.open[key] = d.open; savePrefs();
    if (d.open) requestAnimationFrame(() => { for (const graph of graphs.values()) graph.ensureVisible(); });
  });
  return {root:d, summary, copy, body};
}
function renderDetails(host,id,close,token){
  const node=bundle?.nodes?.[id];host.replaceChildren();host.hidden=!node;if(!node)return;
  const b=button("×",close,token);b.className="close";b.setAttribute("aria-label","关闭详情");host.append(b,el("h3","",node.title));
  const object=Object.values(bundle.reading_model?.objects||{}).find(o=>o.node_id===id&&o.kind!=="project");
  if(object?.state)host.append(el("p","item-state",ReaderNavigation.itemLabel(object.state)+(object.is_current?" · 当前":"")));
  const view=object&&views().find(v=>v.id===object.view_id),issue=view&&sourceIssue(view);if(issue)host.append(el("p","source-issue",issue));
  const primaryKeys=new Set(["完成条件","期望结果","必要前置","明确前置","前置","阻碍"]),primary=el("dl"),report=disclosure(`node-report:${token||id}`,"进展、原文与依据","node-report",false),all=el("dl");
  for(const [key,value] of node.fields||[]){
    const text=fullText(value),main=primaryKeys.has(key)&&!["未记录","未声明","无"].includes(text),dd=el("dd");
    if(text.length>240||text.split("\n").length>4)dd.append(expandableText(key,text,`detail:${token||id}:${key}`));else dd.textContent=text;
    (main?primary:all).append(el("dt","",key),dd);
  }
  if(primary.children.length)host.append(primary);report.body.append(all);host.append(report.root);
}
class Graph {
  constructor(id) {
    this.id=id; this.prefix=`graph-${++serial}-`; this.markup=null; this.width=800; this.height=600;
    const saved=prefs.graphs[id] || {};
    this.state={scale:null,x:0,y:0,mode:saved.mode==="fit"?"fit":"manual",viewportWidth:saved.viewportWidth,viewportHeight:saved.viewportHeight,full:Boolean(saved.full),selected:typeof saved.selected === "string" ? saved.selected : null};
    if (Number.isFinite(saved.scale) && saved.scale>=.08 && saved.scale<=4 && Number.isFinite(saved.x) && Number.isFinite(saved.y)) Object.assign(this.state,{scale:saved.scale,x:saved.x,y:saved.y});
    this.root=el("div","graph"); this.root.dataset.graph=id;
    this.tools=el("div","graph-toolbar"); this.zoomLabel=el("span","zoom-value");
    this.tools.append(button("−",()=>this.zoom(.8),`${id}:minus`),this.zoomLabel,button("+",()=>this.zoom(1.25),`${id}:plus`),button("适合画布",()=>this.fit(),`${id}:fit`));
    this.tools.append(button("重置",()=>this.readable(),`${id}:reset`));
    this.tools.children[0].setAttribute("aria-label","缩小"); this.tools.children[2].setAttribute("aria-label","放大");
    this.all=el("input"); this.all.type="checkbox"; this.all.checked=this.state.full; this.all.dataset.focus=`${id}:full`;
    this.allLabel=el("label"); this.allLabel.append(this.all,document.createTextNode("显示本阶段全部事项")); this.tools.append(this.allLabel);
    this.all.onchange=()=>{this.state.full=this.all.checked; this.state.scale=null; this.update(this.item); this.remember();};
    this.canvas=el("div","canvas"); this.canvas.tabIndex=0; this.canvas.setAttribute("role","region"); this.canvas.dataset.focus=`${id}:canvas`;
    this.drawing=el("div","drawing"); this.canvas.append(this.drawing);
    this.details=el("section","node-details"); this.details.hidden=true; this.details.setAttribute("aria-label","节点详情");
    this.root.append(this.details,this.tools,this.canvas,el("p","graph-hint","点击节点查看详情 · 按钮缩放 · 鼠标拖动平移；普通滚动阅读页面"));
    this.canvas.addEventListener("click",e=>{if (this.moved) return; const group=e.target.closest("g[data-qualified-name]"); if(group) this.detail(nodeId(group));});
    this.canvas.addEventListener("keydown",e=>{const group=e.target.closest("g[data-qualified-name]"); if(group && (e.key==="Enter"||e.key===" ")){e.preventDefault();this.detail(nodeId(group));} if(e.key==="Escape") this.closeDetail();});
    this.details.addEventListener("keydown",e=>{if(e.key==="Escape") this.closeDetail();});
    // Browser/system zoom remains untouched; graph zoom uses its own buttons.
    this.canvas.addEventListener("pointerdown",e=>{if(e.button!==0 || e.pointerType==="touch")return;this.moved=false;this.drag={id:e.pointerId,x:e.clientX,y:e.clientY,left:this.state.x,top:this.state.y};});
    this.canvas.addEventListener("pointermove",e=>{const d=this.drag;if(!d||e.pointerId!==d.id)return;const dx=e.clientX-d.x,dy=e.clientY-d.y;if(Math.abs(dx)+Math.abs(dy)>5){this.moved=true;this.canvas.classList.add("dragging");this.canvas.setPointerCapture(e.pointerId);}if(this.moved){this.state.x=d.left+dx;this.state.y=d.top+dy;this.state.mode="manual";this.transform();}});
    const end=()=>{this.drag=null;this.canvas.classList.remove("dragging");this.remember();};
    this.canvas.addEventListener("pointerup",end);this.canvas.addEventListener("pointercancel",end);
  }
  remember(){prefs.graphs[this.id]={...this.state};savePrefs();}
  transform(){const s=this.state;this.drawing.style.transform=`translate(${s.x}px,${s.y}px) scale(${s.scale||1})`;this.zoomLabel.textContent=`${Math.round((s.scale||1)*100)}%`;}
  fit(){if(!this.canvas.clientWidth||!this.markup)return;this.state.mode="fit";this.state.viewportWidth=this.canvas.clientWidth;this.state.viewportHeight=this.canvas.clientHeight;this.state.scale=Math.max(.08,Math.min(1.25,(this.canvas.clientWidth-28)/this.width,(this.canvas.clientHeight-28)/this.height));this.state.x=(this.canvas.clientWidth-this.width*this.state.scale)/2;this.state.y=Math.max(12,(this.canvas.clientHeight-this.height*this.state.scale)/2);this.transform();this.remember();}
  zoom(factor,px=this.canvas.clientWidth/2,py=this.canvas.clientHeight/2){const s=this.state,old=s.scale||1,next=Math.min(4,Math.max(.08,old*factor));s.x=px-(px-s.x)*next/old;s.y=py-(py-s.y)*next/old;s.scale=next;s.mode="manual";this.transform();this.remember();}
  readable(){this.state.scale=1;this.state.x=12;this.state.y=12;this.state.mode="manual";this.transform();this.remember();}
  ensureVisible(){if(!this.root.isConnected||!this.canvas.clientWidth||this.root.closest("details:not([open]),[hidden]"))return;if(this.state.scale===null)this.readable();else if(this.state.mode==="fit"&&(this.state.viewportWidth!==this.canvas.clientWidth||this.state.viewportHeight!==this.canvas.clientHeight))this.fit();}
  closeDetail(){const id=this.state.selected;this.state.selected=null;this.details.hidden=true;this.remember();const group=Array.from(this.drawing.querySelectorAll("g[data-qualified-name]")).find(g=>nodeId(g)===id);(group||this.canvas).focus({preventScroll:true});}
  detail(id){
    if(!bundle?.nodes?.[id])return;
    const route=this.id!=="context" ? ReaderNavigation.forNode(bundle,id,this.item.project_id,this.item.id) : null;
    if(route){this.state.selected=id;this.remember();navigate(route);return;}
    this.state.selected=id;renderDetails(this.details,id,()=>this.closeDetail(),`${this.id}:close`);this.remember();
    this.details.tabIndex=-1;this.details.focus({preventScroll:true});this.details.scrollIntoView({block:"nearest"});
  }
  update(item){
    this.item=item;this.canvas.setAttribute("aria-label",`${item.title}，推进图，点击节点查看详情`);
    this.allLabel.hidden=!item.full_key||item.full_key===item.flow_key;
    const key=this.state.full&&item.full_key?item.full_key:item.flow_key,markup=bundle.svgs?.[key];
    if(this.markup!==markup){
      this.markup=markup;this.drawing.innerHTML=markup||"";
      const svg=this.drawing.querySelector("svg");
      if(svg){
        // Multiple diagrams share PlantUML IDs: namespace them without changing semantic aliases.
        const ids=new Map();for(const n of svg.querySelectorAll("[id]")){const old=n.id;ids.set(old,this.prefix+old);n.id=this.prefix+old;}
        for(const n of svg.querySelectorAll("*"))for(const attr of Array.from(n.attributes)){
          if(attr.name==="id"||attr.name==="data-qualified-name")continue;
          let value=attr.value;for(const [old,next] of ids){if(value===`#${old}`)value=`#${next}`;value=value.replaceAll(`url(#${old})`,`url(#${next})`);}if(value!==attr.value)n.setAttribute(attr.name,value);
        }
        const box=svg.viewBox.baseVal;this.width=box.width||parseFloat(svg.getAttribute("width"))||800;this.height=box.height||parseFloat(svg.getAttribute("height"))||600;
        svg.style.width=`${this.width}px`;svg.style.height=`${this.height}px`;
        for(const group of svg.querySelectorAll("g[data-qualified-name]")){const id=nodeId(group);if(id){group.tabIndex=0;group.setAttribute("tabindex","0");group.setAttribute("role","button");group.setAttribute("aria-label",bundle.nodes[id].title+"，查看详情");group.dataset.focus=`${this.id}:node:${id}`;}}
      } else this.drawing.append(el("p","empty","此目标暂时没有可用图形。"));
    }
    if(this.state.selected&&bundle.nodes[this.state.selected]&&(this.id==="context"||!ReaderNavigation.forNode(bundle,this.state.selected,item.project_id,item.id)))renderDetails(this.details,this.state.selected,()=>this.closeDetail(),`${this.id}:close`);
    else {this.state.selected=null;this.details.hidden=true;}
    this.transform();this.ensureVisible();
  }
}
function graphFor(item,key=item.id){let graph=graphs.get(key);if(!graph){graph=new Graph(key);graphs.set(key,graph);}graph.update(item);return graph.root;}
function relationText(relation,targets){const target=targets.find(t=>t.id===relation.to);return `${target?.title||relation.to}${relation.status==="proposed"||target?.status==="proposed"?"〔待确认〕":""}`;}
function sourceIssue(item){const state=sourceStates.find(s=>s.plan_id===(item.source_id||item.plan_id));return state&&state.status!=="synced"?`${statusLabels[state.status]||state.status}：${state.message||"当前保留上次可靠记录"}`:"";}
function isMaterial(item){return ["markdown","material","source_material"].includes(item.record_kind);}
function recordLabel(item){return isMaterial(item)?"相关材料":item.record_kind==="pilot_goal"?"业务目标":["execplan","implementation_plan"].includes(item.record_kind)||item.external?"来源实施阶段":"阶段";}
function recordNoun(item){return isMaterial(item)?"材料入口":item.record_kind==="pilot_goal"?"目标":"阶段";}
function fullText(value){if(value==null)return "";return Array.isArray(value)?value.map(fullText).join("\n"):typeof value==="object"?JSON.stringify(value):String(value);}
function shortText(value,limit=110){const text=fullText(value);return text.length>limit?text.slice(0,limit)+"…":text;}
function expandableText(label,value,key){
  const text=fullText(value)||"未记录",root=el("div","summary-field");
  root.append(el("span","field-label",label));
  if(text.length>110||text.includes("\n")){
    const d=el("details","summary-text"),summary=el("summary","",label?`展开${label}`:"查看完整内容");summary.dataset.focus=`text:${key}`;
    if(prefs.open[`text:${key}`])d.open=true;
    d.append(summary,el("p","complete-text",text));
    d.addEventListener("toggle",()=>{if(d.isConnected){prefs.open[`text:${key}`]=d.open;savePrefs();}});root.append(d);
  }else root.append(el("p","",text));
  return root;
}
function routeLink(text,route,token){
  route={...ReaderNavigation.context(routeState?.route||{}),...route};const a=el("a","reading-link",text);a.href=ReaderNavigation.format(route)||"#";if(token)a.dataset.focus=token;
  a.addEventListener("click",event=>{if(event.button||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;event.preventDefault();navigate(route);});return a;
}
function projectSources(project){const ids=new Set(views().filter(v=>v.project_id===project.id).map(v=>v.source_id||v.plan_id).filter(Boolean));return sourceStates.filter(s=>s.project_id===project.id||ids.has(s.plan_id));}
function projectFreshness(project){
  const states=projectSources(project),issues=states.filter(s=>s.status!=="synced");
  if(location.protocol==="file:")return "离线快照，未核对当前来源";
  if(issues.length)return `${issues.length} 个来源需要关注：${issues.map(s=>statusLabels[s.status]||s.status).join("、")}`;
  return states.length?`来源已核对 · ${formatTime(states[0].checked_at)}`:"本地记录；来源核对范围见上方";
}
function objectFor(id){return bundle.reading_model?.objects?.[id];}
function objectRoute(object){return object.kind==="project"?{project:object.project_id}:{project:object.project_id,view:object.view_id||"",item:object.id};}
function nodeList(item,ids,scope=""){
  const root=el("div","record-list");
  for(const id of [...new Set(ids||[])]){
    const object=objectFor(id),node=bundle.nodes?.[object?.node_id||id];if(!node)continue;
    const route=object?objectRoute(object):ReaderNavigation.forNode(bundle,id,item.project_id,item.id);
    const row=el("div","record-row");if(route)row.append(routeLink(node.title,route,ReaderNavigation.appearanceKey(scope,`item:${route.item||route.project}`)));
    else continue;
    if(object){const text=object.kind==="business_stage"?ReaderNavigation.itemLabel(object.state):ReaderNavigation.itemLabel(object.state);row.append(el("span","summary-meta",ReaderNavigation.kindLabel(object.kind)+" · "+text+(object.is_current?" · 当前":"")+(object.confirmation==="proposed"?" · 拟议":"")));}
    root.append(row);
  }
  if(!root.querySelector(".record-row"))root.append(el("p","empty","尚未规划推进事项。目标及其完成条件仍可查看。"));
  return root;
}
function goalBranch(item,scope=""){
  const presentationId=ReaderNavigation.appearanceKey(scope,item.id);
  const d=disclosure(presentationId,item.title,"goal-branch",false);
  if(routeState?.route.view===item.id){d.root.open=true;prefs.open[presentationId]=true;}
  if(item.is_current)d.copy.querySelector(".summary-title").append(badge("当前"));
  d.copy.append(el("span","summary-meta",`${recordLabel(item)}${isMaterial(item)?"":` · ${ReaderNavigation.stageLabel(item.stage_status)}`}`));
  const issue=el("span","source-issue",sourceIssue(item));issue.dataset.source=item.source_id||item.plan_id||"";d.copy.append(issue);
  if(routeState?.route.view===item.id&&item.target_state)d.body.append(expandableText("期望结果",item.target_state,`${presentationId}:primary-target`));
  if(item.stage_status==="blocked"&&item.block_reason)d.body.append(el("p","source-issue",`实际阻碍：${item.block_reason}`));
  d.body.append(routeLink(`打开此${recordNoun(item)}`,{project:item.project_id,view:item.id},`view:${presentationId}`));
  const object=objectFor(item.id),children=object?.children||item.detail_node_ids?.filter(id=>id!==item.goal_node)||[];
  const selected=routeState?.item&&objectFor(routeState.route.item);
  if(selected?.kind==="business_stage"&&selected.view_id===item.id){d.body.append(el("h3","section-heading",selected.title+" · 行动"),nodeList(item,selected.children,presentationId));}
  else d.body.append(nodeList(item,children,presentationId));
  if(item.flow_key&&bundle.svgs?.[item.flow_key]){
    const key=`graph-view:${presentationId}`,tools=el("div","local-view-tools");
    tools.append(button(prefs.open[key]?"返回事项列表":"查看关系图",()=>{prefs.open[key]=!prefs.open[key];savePrefs();const pos=capturePosition();renderTree();restorePosition(pos);},`${presentationId}:switch-graph`));
    d.body.append(tools);if(prefs.open[key])d.body.append(graphFor(item,presentationId));
  }
  if(item.predecessor_ids?.length){const row=el("p","goal-associations","接续：");for(const id of item.predecessor_ids){const predecessor=views().find(v=>v.id===id);if(predecessor)row.append(routeLink(predecessor.title,{project:predecessor.project_id,view:id},`${presentationId}:predecessor:${id}`));else row.append(el("span","","未载入的阶段"));}d.body.append(row);}
  const report=disclosure(`report:${presentationId}`,isMaterial(item)?"材料原文与来源":"进展与依据","stage-report",false);
  for(const [label,value] of [["期望结果",item.target_state],["评价",item.status],["来源下一步",item.next]])if(value)report.body.append(expandableText(label,value,`${presentationId}:${label}`));
  report.body.append(el("p","summary-meta",item.observed_at?`来源核对于 ${formatTime(item.observed_at)}`:"来源核对时间未记录"));
  if(item.goal_node&&bundle.nodes[item.goal_node]){const details=el("section","node-details");renderDetails(details,item.goal_node,()=>{details.hidden=true;},`${presentationId}:report-close`);report.body.append(details);}
  d.body.append(report.root);return d.root;
}
function projectRelations(project,context,scope=""){
  const rels=context.relations.filter(r=>r.from===project.id),membership=rels.find(r=>r.kind==="belongs_to"),direct=rels.filter(r=>r.kind==="serves"),supports=rels.filter(r=>r.kind==="supports"),root=el("div","relations");
  const row=(label,text,kind)=>classificationRow(label,text,kind,project.id,project.title,scope);
  root.append(row("主要领域",membership?relationText(membership,context.areas):"尚未登记","area"),row("服务方向",direct.map(r=>relationText(r,context.directions)).join("、")||"尚未登记","directions"));
  if(supports.length)root.append(el("p","",`也支持：${supports.map(r=>relationText(r,context.areas)).join("、")}`));return root;
}
function summaryFields(host,project){
  for(const [key,label] of [["recent_change","记录说明"],["next_step","来源下一步"],["blockers","相关说明"]])if(project.summary?.[key])host.append(expandableText(label,project.summary[key],`${project.id}:${key}`));
  if(project.material_updates?.length)host.append(expandableText("材料版本",project.material_updates.join("\n"),`${project.id}:materials`));
  if(project.summary?.source_label)host.append(el("p","summary-meta",fullText(project.summary.source_label)));
}
function renderCoverage(){
  const host=$("coverage");host.replaceChildren(el("span","",`当前接入 ${projects().length} 个项目`));
  $("coverage-list").replaceChildren();
  const labels={included:"已接入",connected:"已接入",partial:"部分接入",not_connected:"未接入",unavailable:"来源不可用"};
  for(const item of Array.isArray(bundle.coverage)?bundle.coverage:[])$("coverage-list").append(expandableText(`${item.title} · ${labels[item.status]||item.status}`,item.message||"范围说明未记录",`coverage:${item.id}`));
}
function projectIssue(project){const issues=projectSources(project).filter(s=>s.status!=="synced");return issues.length?`${issues.length} 处记录未更新；展开查看上次可靠内容`:location.protocol==="file:"?"离线快照；当前来源尚未核对":"";}
function projectItems(project){return views().filter(v=>v.project_id===project.id&&!isMaterial(v));}
function structureCounts(project){const s=ReaderNavigation.summary(bundle,project),parts=[];if(s.roots.length)parts.push(`${s.roots.length} 个目标／实施阶段`);if(s.businessStages.length)parts.push(`${s.businessStages.length} 个业务阶段`);if(s.materials.length)parts.push(`${s.materials.length} 份材料`);return parts.join(" · ")||"尚无可靠阶段记录";}
function stageGroups(host,project,items,preview=false,scope=""){
  const {visible,remaining,history:closed}=ReaderNavigation.partition(items,preview),groups=[["推进中","active"],["受阻","blocked"],["计划中","planned"],["暂停","paused"],["待核对","unknown"]];
  for(const [label,status] of groups){const group=visible.filter(v=>(v.stage_status||"unknown")===status);if(!group.length)continue;host.append(el("h3","stage-group-title",`${label} · ${group.length}`));for(const item of group)host.append(goalBranch(item,scope));}
  if(remaining.length){const more=disclosure(ReaderNavigation.appearanceKey(scope,`more:${project.id}`),`另有 ${remaining.length} 项，展开查看`,"more-stages",false);for(const item of remaining)more.body.append(goalBranch(item,scope));host.append(more.root);}
  if(closed.length){const history=disclosure(ReaderNavigation.appearanceKey(scope,`history:${project.id}`),`历史阶段 · ${closed.length}`,"history-branch",false);if(closed.some(v=>v.id===routeState.route.view))history.root.open=true;for(const item of closed)history.body.append(goalBranch(item,scope));host.append(history.root);}
  if(!items.length)host.append(el("p","empty","尚无可靠的目标或阶段记录。"));
}
function renderOverview(){
  const host=$("overview");host.replaceChildren();host.hidden=false;$("project-detail").hidden=true;$("context-region").hidden=true;
  const heading=el("h1","","项目概览");heading.id="overview-title";heading.tabIndex=-1;host.append(heading);
  const list=el("div","project-rows");
  for(const project of projects()){
    const row=el("article","project-row"),copy=el("div","project-row-copy"),s=ReaderNavigation.summary(bundle,project);
    const h=el("h2");h.append(routeLink(project.title,{project:project.id},`project-card:${project.id}`));copy.append(h,el("p","current-line",s.text));
    const issue=el("p","source-issue",projectIssue(project));issue.dataset.projectFreshness=project.id;copy.append(issue);
    const actions=el("div","project-row-actions");
    actions.append(routeLink(s.businessStages.length?"查看行动 →":"查看目标与阶段 →",{project:project.id},`project-open:${project.id}`));
    if(s.counts.blocked)actions.append(routeLink(`受阻事项 ${s.counts.blocked}`,{project:project.id},`project-blocked:${project.id}`));
    if(s.materials.length)actions.append(routeLink(`相关材料 ${s.materials.length}`,{project:project.id,view:s.materials[0].id},`project-materials:${project.id}`));
    row.append(copy,actions);list.append(row);
  }
  if(!projects().length)list.append(el("p","empty","当前读取范围内没有项目。"));host.append(list);
}
function relationLabels(project){
  const r=ReaderNavigation.relations(bundle,project.id),c=bundle.context,root=el("div","direction-relations");
  if(r.membership?.status==="proposed")root.append(el("span","relation-label","主要归属 · 待确认"));
  for(const relation of r.direct)root.append(el("span","relation-label",`直接服务：${relationText(relation,c.directions)}`));
  for(const relation of r.via)root.append(el("span","relation-label",`经 ${c.areas.find(a=>a.id===relation.via_area)?.title||relation.via_area} 关联：${relationText(relation,c.directions)}${relation.membership_status==="proposed"?"〔归属待确认〕":""}`));
  for(const relation of r.supports)root.append(el("span","relation-label",`也支持：${relationText(relation,c.areas)}`));
  return root;
}
function structureProject(project,scope){
  const key=ReaderNavigation.appearanceKey(scope,project.id),branch=disclosure(key,"项目 · "+project.title,"structure-project",false);
  branch.copy.append(el("span","summary-meta",structureCounts(project)),relationLabels(project));
  const issue=el("span","source-issue",projectIssue(project));issue.dataset.projectFreshness=project.id;branch.copy.append(issue);
  branch.body.append(routeLink("进入项目 →",{project:project.id},`${key}:open`));
  const classify=disclosure(`${key}:classify`,"修改领域与方向","project-classification",false);
  classify.body.append(projectRelations(project,bundle.context,key),el("p","muted","这里只保存本地归属说明，不修改原项目的目标、进度或材料。"));branch.body.append(classify.root);
  let rows=projectItems(project);if(routeState.route.scope==="current")rows=rows.filter(v=>v.stage_status!=="closed");stageGroups(branch.body,project,rows,true,key);
  return branch.root;
}
function structureAreas(host,areas,scope){
  for(const {area,projects:items} of areas){
    const group=el("section","area-group"),title=el("h3","area-heading",area?"相关领域 · "+area.title+(area.status==="proposed"?" · 待确认":""):"领域待明确");
    group.append(title);
    for(const project of items)group.append(structureProject(project,ReaderNavigation.appearanceKey(scope,area?.id||"no-area")));
    host.append(group);
  }
}
function renderStructure(){
  $("overview").hidden=true;$("project-detail").hidden=true;const host=$("context-region");host.hidden=false;host.replaceChildren();
  const heading=el("h1","","整体结构");heading.id="structure-title";heading.tabIndex=-1;host.append(heading);
  const grouping=routeState.route.group||"directions";
  host.append(el("p","structure-guide",grouping==="directions"?"先选方向，再按领域找到项目，展开目标与行动。领域是这里的分组；一个项目可以服务多个方向，进度只有一份。":"按主要领域查找项目；项目的服务方向和跨领域支持仍可查看。"));
  const tools=el("div","structure-tools");
  for(const [group,label] of [["directions","按方向展开"],["areas","按领域查看"]]){const b=button(label,()=>navigate({...ReaderNavigation.context(routeState.route),mode:"structure",group}),`structure-group:${group}`);b.setAttribute("aria-pressed",String(grouping===group));tools.append(b);}
  for(const [scope,label] of [["","全部阶段"],["current","未结束阶段"]]){const b=button(label,()=>navigate({...ReaderNavigation.context(routeState.route),mode:"structure",scope}),`structure-scope:${scope}`);b.setAttribute("aria-pressed",String((routeState.route.scope||"")===scope));tools.append(b);}
  const search=el("input");search.type="search";search.placeholder="查找项目、阶段或事项";search.setAttribute("aria-label","查找项目、阶段或事项");search.dataset.focus="structure-search";search.value=routeState.route.q||"";
  const commitSearch=()=>{if(!search.isConnected)return;const pos=capturePosition(),start=search.selectionStart,end=search.selectionEnd;const route={...ReaderNavigation.context(routeState.route),mode:"structure",q:search.value};try{history.replaceState({...history.state},"",location.pathname+location.search+(ReaderNavigation.format(route)||"#"));}catch(_){location.hash=ReaderNavigation.format(route);}renderTree();restorePosition(pos);const input=document.querySelector('[data-focus="structure-search"]');try{input?.setSelectionRange(start,end);}catch(_){}};search.oninput=event=>{if(!event.isComposing)commitSearch();};search.oncompositionend=commitSearch;tools.append(search);host.append(tools);
  let listed=projects();
  if(routeState.route.direction){listed=listed.filter(p=>{const r=ReaderNavigation.relations(bundle,p.id);return [...r.direct,...r.via].some(x=>x.to===routeState.route.direction);});const note=el("p","filter-notice",`仅看此方向关联项目 · ${listed.length} 个`);note.append(button("清除方向筛选",()=>navigate({...ReaderNavigation.context(routeState.route),mode:"structure",direction:""}),"clear-direction"));host.append(note);}
  if(routeState.route.q){const found=ReaderNavigation.search(bundle,routeState.route.q).filter(o=>listed.some(p=>p.id===o.project_id));const result=el("section","search-results");result.append(el("h2","section-heading",`找到 ${found.length} 项`));for(const o of found.slice(0,200)){const row=el("div","record-row");row.append(routeLink(o.title,objectRoute(o),`search:${o.id}`),el("span","summary-meta",o.path.map(n=>n.title).join(" / ")+" · "+ReaderNavigation.kindLabel(o.kind)));result.append(row);}if(found.length>200)result.append(el("p","muted","请增加关键词以缩小结果；已显示前 200 项。"));host.append(result);return;}
  const projected={...bundle,projects:listed};
  for(const group of ReaderNavigation.structureGroups(projected,grouping)){
    if(grouping==="areas"){structureAreas(host,group.areas,"area-reading");continue;}
    if(routeState.route.direction&&group.direction?.id!==routeState.route.direction)continue;
    const key=`direction-reading:${group.id}`,title=group.direction?"方向 · "+group.direction.title+(group.direction.status==="proposed"?" · 待确认":""):"方向待明确";
    const branch=disclosure(key,title,"direction-branch",false),names=listed.filter(p=>group.project_ids.includes(p.id)).map(p=>p.title);
    branch.copy.append(el("span","summary-meta",`${names.length} 个项目`+(names.length?" · "+names.join("、"):" · 尚无关联项目")));
    if(group.direction)branch.body.append(el("p","relation-label","以下是服务于此方向的项目，按其主要领域分组。"));
    else branch.body.append(el("p","relation-label","这些项目还没有登记方向关联；可展开项目后修改。"));
    structureAreas(branch.body,group.areas,key);host.append(branch.root);
  }
  if(!listed.length)host.append(el("p","empty","当前范围没有项目。"));
  const noArea=projects().filter(p=>!ReaderNavigation.relations(bundle,p.id).membership).length,noDirection=projects().filter(p=>{const r=ReaderNavigation.relations(bundle,p.id);return !r.direct.length&&!r.via.length;}).length;
  const info=disclosure("context-management","归属说明与管理","context-management",false);info.body.append(el("p","",`${projects().length} 个不同项目；${noArea} 个尚未登记主要领域，${noDirection} 个尚未登记方向关联。多个方向下的同名入口共享项目记录。`));
  for(const area of bundle.context.areas){const rels=bundle.context.relations.filter(r=>r.from===area.id&&r.kind==="serves");info.body.append(el("h3","",area.title),classificationRow("领域服务方向",rels.map(r=>relationText(r,bundle.context.directions)).join("、")||"尚未登记","directions",area.id,area.title));if(area.status==="proposed"){const b=button("确认此领域",()=>openClassification("confirm",area.id,area.title),`confirm:${area.id}`);b.disabled=location.protocol==="file:";info.body.append(b);}}host.append(info.root);
}
function backToReading(fallback){if(history.state?.readingFrom!=null){history.back();return;}navigate(fallback);}
function renderProject(project){
  $("overview").hidden=true;$("context-region").hidden=true;const host=$("project-detail");host.replaceChildren();host.hidden=false;
  host.append(button(routeState.route.view||routeState.route.item?"← 返回上一阅读位置":`← 返回${routeState.route.mode==="structure"?"整体结构":"项目概览"}`,()=>backToReading(ReaderNavigation.context(routeState.route)),"project-back"));
  const heading=el("h1","",project.title);heading.id="project-title";heading.tabIndex=-1;host.append(heading);
  const issue=el("p","source-issue",projectIssue(project));issue.dataset.projectFreshness=project.id;host.append(issue);
  if(routeState.node_id){
    const detail=el("section","node-details selected-detail");detail.id="selected-detail";detail.tabIndex=-1;
    renderDetails(detail,routeState.node_id,()=>backToReading(ReaderNavigation.parent(routeState.route)),"selected-detail:close");
    const path=ReaderNavigation.path(bundle,routeState.route.item);detail.prepend(el("p","detail-path",path.map(n=>n.title).join(" / ")));
    detail.append(button("← 返回阅读位置",()=>backToReading(ReaderNavigation.parent(routeState.route)),"selected-detail:back"),routeLink("此事项的稳定链接",routeState.route,"selected-detail:permalink"));host.append(detail);
    const obj=objectFor(routeState.route.item);if(obj?.depends_on?.length){const refs=el("section","prerequisite-links");refs.append(el("h2","section-heading","明确前置"));for(const id of obj.depends_on){const dep=objectFor(id);if(dep)refs.append(routeLink(ReaderNavigation.path(bundle,id).map(x=>x.title).join(" / "),objectRoute(dep),`dependency:${id}`));else refs.append(el("p","source-issue",`未载入的前置：${id}`));}host.append(refs);}
  }
  const items=projectItems(project),selected=routeState.route.view&&items.find(v=>v.id===routeState.route.view);
  host.append(el("h2","section-heading",selected?"当前查看的目标／阶段":"目标与阶段"));
  if(selected){host.append(goalBranch(selected));const other=disclosure(`other-stages:${project.id}`,`其他目标与阶段 · ${items.length-1}`,"other-stages",false);stageGroups(other.body,project,items.filter(v=>v.id!==selected.id));host.append(other.root);}else stageGroups(host,project,items);
  const materials=views().filter(v=>v.project_id===project.id&&isMaterial(v));
  if(materials.length){const section=disclosure(`materials:${project.id}`,`相关材料 · ${materials.length}`,"materials-branch",false);if(materials.some(v=>v.id===routeState.route.view))section.root.open=true;for(const item of materials)section.body.append(goalBranch(item));host.append(section.root);}
  if(project.unassigned_task_nodes?.length){const other=disclosure(`unassigned:${project.id}`,`未归阶段的事项 · ${project.unassigned_task_nodes.length}`,"unassigned-branch",false);if(routeState.item&&!routeState.route.view)other.root.open=true;other.body.append(nodeList({id:null,project_id:project.id},project.unassigned_task_nodes));host.append(other.root);}
  const relation=disclosure(`project-relations:${project.id}`,"项目关系","project-relations",false);relation.body.append(projectRelations(project,bundle.context));host.append(relation.root);
  const summary=disclosure(`summary:${project.id}`,"记录与来源说明","project-summary",false);summaryFields(summary.body,project);host.append(summary.root);
}
function capturePosition(){const active=document.activeElement,token=active?.dataset?.focus;const anchor=Array.from(document.querySelectorAll("[data-focus]")).find(n=>{const r=n.getBoundingClientRect();return r.height&&r.bottom>0&&r.top<window.innerHeight;});return{token,id:active?.id,anchor:anchor?.dataset.focus,top:anchor?.getBoundingClientRect().top,y:window.scrollY};}
function restorePosition(position){if(!position)return;const find=token=>Array.from(document.querySelectorAll("[data-focus]")).find(n=>n.dataset.focus===token);const focus=position.token?find(position.token):$(position.id);if(!$("classification").open||$("classification").contains(focus))focus?.focus({preventScroll:true});const anchor=position.anchor?find(position.anchor):null;window.scrollTo(0,anchor?window.scrollY+anchor.getBoundingClientRect().top-position.top:position.y);}
function renderTree(){
  const requested=ReaderNavigation.parse(location.hash);if(!requested.mode&&prefs.mode==="structure")requested.mode="structure";
  routeState=ReaderNavigation.resolve(bundle,requested);prefs.mode=routeState.route.mode||"overview";renderCoverage();
  $("route-notice").hidden=!routeState.error;$("route-notice").textContent=routeState.error;
  const ctx=ReaderNavigation.context(routeState.route),crumbs=$("breadcrumbs");crumbs.replaceChildren(routeLink(prefs.mode==="structure"?"整体结构":"项目概览",ctx,"breadcrumb:home"));crumbs.hidden=!routeState.project;
  if(routeState.project){crumbs.append(el("span",""," / "),routeLink(routeState.project.title,{...ctx,project:routeState.project.id},"breadcrumb:project"));if(routeState.view)crumbs.append(el("span",""," / "),routeLink(routeState.view.title,{...ctx,project:routeState.project.id,view:routeState.view.id},"breadcrumb:view"));if(routeState.node_id)crumbs.append(el("span",""," / "),el("span","",bundle.nodes[routeState.node_id].title));renderProject(routeState.project);}else if(prefs.mode==="structure")renderStructure();else renderOverview();
  $("overview-toggle").setAttribute("aria-pressed",String(prefs.mode!=="structure"));$("context-toggle").setAttribute("aria-pressed",String(prefs.mode==="structure"));
  const valid=new Set(views().map(v=>v.id));for(const [id,graph] of graphs)if(!valid.has(graph.item?.id))graphs.delete(id);
  prefs.initialized=true;savePrefs();renderedHash=location.hash;requestAnimationFrame(()=>{for(const graph of graphs.values())graph.ensureVisible();});
}
function focusRoute(){
  if(routeState?.node_id){$("selected-detail")?.focus({preventScroll:true});$("selected-detail")?.scrollIntoView({block:"start"});return;}
  if(routeState?.view){const branch=Array.from(document.querySelectorAll("[data-branch]")).find(n=>n.dataset.branch===routeState.view.id);branch?.querySelector("summary")?.focus({preventScroll:true});branch?.scrollIntoView({block:"start"});return;}
  const heading=$(routeState?.project?"project-title":prefs.mode==="structure"?"structure-title":"overview-title");heading?.focus({preventScroll:true});heading?.scrollIntoView({block:"start"});
}
function rememberRoutePosition(){const position=capturePosition();routePositions.set(location.hash,position);try{history.replaceState({...history.state,readingPosition:position,readingHash:location.hash},"",location.href);}catch(_){/* File previews can still navigate. */}}
function navigate(route){
  if(!bundle)return;rememberRoutePosition();const hash=ReaderNavigation.format(route);
  if(hash!==location.hash){try{history.pushState({readingFrom:location.hash},"",location.pathname+location.search+(hash||"#"));}catch(_){location.hash=hash;return;}}
  renderTree();requestAnimationFrame(()=>{const saved=routePositions.get(location.hash);if(saved)restorePosition(saved);else focusRoute();});
}
function locationChanged(){
  if(!bundle||renderedHash===location.hash)return;
  // popstate has already moved history.state; retain the outgoing screen separately.
  if(renderedHash!==null)routePositions.set(renderedHash,capturePosition());
  renderTree();requestAnimationFrame(()=>{const saved=routePositions.get(location.hash)||(history.state?.readingHash===location.hash?history.state.readingPosition:null);if(saved)restorePosition(saved);else focusRoute();});
}
function freshness(response){
  sourceStates=response.sources||[];const issues=sourceStates.filter(s=>s.status!=="synced");
  $("source-summary").textContent=location.protocol==="file:"?"离线快照，未核对当前来源":sourceStates.length?`${issues.length?`${issues.length} 个来源需要关注`:"已核对的外部来源均已同步"} · 最近检查 ${formatTime(sourceStates[0].checked_at)}`:"本地账本已读取；尚无自动核对的外部来源";
  $("source-list").replaceChildren(...sourceStates.map(s=>el("li","",`${s.title}：${statusLabels[s.status]||s.status}。${s.message||""} 来源版本 ${(s.raw_sha256||s.source_version||"未知").slice(0,10)} · 展示版本 ${(s.rendered_version||"尚无").slice(0,10)}`)));
  // Do not open panels during background polling: the visible summary reports problems.
  for(const node of document.querySelectorAll("[data-source]")){const item=views().find(v=>(v.source_id||v.plan_id)===node.dataset.source);if(item)node.textContent=sourceIssue(item);}
  for(const node of document.querySelectorAll("[data-project-freshness]")){const project=projects().find(p=>p.id===node.dataset.projectFreshness);if(project)node.textContent=projectIssue(project);}
}
function accept(response){
  const position=capturePosition(),previous=bundle;freshness(response);
  $("warning").hidden=!response.stale;$("warning").textContent=response.message||"";
  if(!response.bundle){$("loading").textContent=response.message||"正在生成当前视图…";return;}
  const changed=bundle?.fingerprint!==response.bundle.fingerprint;bundle=response.bundle;
  try{if(changed)renderTree();}catch(error){bundle=previous;if(bundle)renderTree();throw error;}
  $("loading").hidden=true;$("updated").textContent=`${response.stale?"上次可靠内容":"阅读包生成"} ${bundle.updated_at} · 上海`;
  $("version").textContent=`本地账本版本 ${bundle.revision} · 阅读包生成 ${bundle.updated_at} · 上海；各来源版本分别见来源面板`;
  if(previous)restorePosition(position);else if(location.hash||history.state?.readingPosition)requestAnimationFrame(()=>{const saved=history.state?.readingHash===location.hash?history.state.readingPosition:null;if(saved)restorePosition(saved);else focusRoute();});
}
async function refresh(){
  if(pending||location.protocol==="file:")return;pending=true;$("refresh-sources").disabled=true;
  try{const result=await fetch("/api/reading",{cache:"no-store"});if(!result.ok)throw Error(`HTTP ${result.status}`);accept(await result.json());}
  catch(error){console.error("Reading refresh failed",error);$("warning").hidden=false;$("warning").textContent=bundle?"暂时无法更新阅读页，保留上次可靠内容；恢复后会自动重试。":"暂时无法读取项目；恢复连接后会自动读取。";$("source-summary").textContent="来源不可用或显示失败；正在保留上次可靠视图";}
  finally{pending=false;$("refresh-sources").disabled=false;}
}
// The dialog lives outside the refreshed tree: polling cannot replace its draft.
let editSession=null, undoRecord=null;
function classificationRow(label,text,kind,id,title,scope=""){
  const row=el("p","classification-row"),b=button(text,()=>openClassification(kind,id,title),ReaderNavigation.appearanceKey(scope,`edit:${kind}:${id}`));
  b.className="classification-value";b.setAttribute("aria-label",`${title}：修改${label}，${text}`);
  b.disabled=location.protocol==="file:";row.append(document.createTextNode(label+"："),b);return row;
}
async function contextRequest(change,expected){
  const response=await fetch("/api/context",change?{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({expected_version:expected,change})}:{cache:"no-store"});
  const data=await response.json();if(!response.ok){const error=Error(data.error||`HTTP ${response.status}`);error.status=response.status;throw error;}return data;
}
function editError(message){$("edit-error").textContent=message||"";$("edit-error").hidden=!message;}
function editBusy(busy){if(editSession)editSession.busy=busy;for(const control of $("classification-form").querySelectorAll("input,select,button"))control.disabled=busy;}
function editLabel(text,input){const label=el("label","edit-field",text);label.append(input);return label;}
async function openClassification(kind,id,title){
  editSession={kind,id,title,busy:true};$("edit-title").textContent=kind==="area"?"设置主要领域":kind==="directions"?"设置服务方向":"确认领域";
  $("edit-subject").textContent=title;$("edit-fields").replaceChildren();editError();$("edit-reload").hidden=true;
  $("edit-help").textContent="正在读取最新归属…";$("classification").showModal();editBusy(true);await loadClassification();
}
async function loadClassification(){
  const session=editSession;if(!session)return;editBusy(true);editError();$("edit-reload").hidden=true;
  try{
    const data=await contextRequest();if(editSession!==session)return;
    session.version=data.version;const context=data.context,fields=$("edit-fields");fields.replaceChildren();
    const outside=projects().some(p=>p.id===session.id&&p.external)?"这里只调整本地归属，原项目的开发进度保持不变。":"";
    if(session.kind==="area"){
      $("edit-help").textContent="领域是持续关注的一类事情，如科研、健康。保存即确认所选领域及此项目的归属；不改变服务方向。"+outside;
      const select=el("select");select.id="edit-area";
      for(const [value,text] of [["","暂不归属"],...context.areas.map(n=>[n.id,n.title+(n.status==="proposed"?"〔待确认〕":"")]),["__new__","＋ 新建领域…"]]){const option=el("option","",text);option.value=value;select.append(option);}
      select.value=context.relations.find(r=>r.from===session.id&&r.kind==="belongs_to")?.to||"";
      const input=el("input");input.id="edit-new-area";input.type="text";input.maxLength=120;input.placeholder="如：科研";
      const label=editLabel("新领域名称",input);label.hidden=true;input.disabled=true;
      select.onchange=()=>{label.hidden=select.value!=="__new__";input.required=!label.hidden;input.disabled=label.hidden;if(!label.hidden)input.focus();};
      fields.append(editLabel("主要领域",select),label);
    }else if(session.kind==="directions"){
      $("edit-help").textContent="方向说明希望这件事支持什么，如保持健康与行动能力。可多选，也可以暂时不选。保存即确认所选方向及服务关系。"+outside;
      const selected=new Set(context.relations.filter(r=>r.from===session.id&&r.kind==="serves").map(r=>r.to)),list=el("fieldset");list.append(el("legend","","已有方向"));
      if(!context.directions.length)list.append(el("p","muted","还没有方向，可在下方直接新建。"));
      for(const node of context.directions){const input=el("input");input.type="checkbox";input.value=node.id;input.name="direction";input.checked=selected.has(node.id);const label=el("label","direction-choice");label.append(input,document.createTextNode(node.title+(node.status==="proposed"?"〔待确认〕":"")));list.append(label);}
      const input=el("input");input.id="edit-new-direction";input.type="text";input.maxLength=120;input.placeholder="填写后，保存时新建并选用";fields.append(list,editLabel("新增一个方向（选填）",input));
    }else{
      $("edit-help").textContent="确认将此名称作为一个持续关注的领域；项目的拟议归属仍需各自确认。";
      fields.append(el("p","",session.title));
    }
    editBusy(false);const areaInput=$("edit-new-area");if(areaInput)areaInput.disabled=true;
    fields.querySelector("select,input")?.focus();
  }catch(error){if(editSession!==session)return;editBusy(false);$("edit-save").disabled=true;$("edit-reload").hidden=false;editError(error.message);}
}
function notifyEdit(message,canUndo=false){$("edit-notice").hidden=false;$("edit-notice").querySelector("span").textContent=message;$("undo-context").hidden=!canUndo;}
$("edit-cancel").onclick=()=>{if(!editSession?.busy){editSession=null;$("classification").close();}};
$("classification").addEventListener("cancel",event=>{if(editSession?.busy)event.preventDefault();else editSession=null;});
$("edit-reload").onclick=loadClassification;
$("classification-form").onsubmit=async event=>{
  event.preventDefault();const session=editSession;if(!session||session.busy)return;
  let change;
  if(session.kind==="area"){const value=$("edit-area").value;change={action:"assign_area",project_id:session.id,area:value==="__new__"?{title:$("edit-new-area").value}:value?{id:value}:null};}
  else if(session.kind==="directions"){
    const refs=Array.from($("edit-fields").querySelectorAll('input[name="direction"]:checked')).map(n=>({id:n.value})),title=$("edit-new-direction").value.trim();if(title)refs.push({title});change={action:"set_directions",subject_id:session.id,directions:refs};
  }else change={action:"confirm_area",area_id:session.id};
  editBusy(true);editError();
  try{
    const result=await contextRequest(change,session.version);
    if(result.changed){undoRecord={version:result.version,context:result.previous,subjectId:session.id};}
    if(session.kind==="area"){const area=result.context.relations.find(r=>r.from===session.id&&r.kind==="belongs_to")?.to;prefs.open[area?`area:${area}`:"group:unassigned-area"]=true;prefs.open[`project:${session.id}`]=true;savePrefs();}
    editSession=null;$("classification").close();notifyEdit(result.changed?"已保存归属；图将自动更新。":"归属没有变化。",Boolean(undoRecord));refresh();
  }catch(error){editBusy(false);if($("edit-new-area"))$("edit-new-area").disabled=$("edit-area").value!=="__new__";editError(error.message);$("edit-reload").hidden=false;if(error.status===409)$("edit-save").disabled=true;}
};
$("undo-context").onclick=async()=>{
  if(!undoRecord)return;$("undo-context").disabled=true;
  try{await contextRequest({action:"restore",context:undoRecord.context},undoRecord.version);undoRecord=null;notifyEdit("已撤销这次归属修改；图将自动更新。");refresh();}
  catch(error){notifyEdit(`未撤销：${error.message}`,false);undoRecord=null;}
  finally{$("undo-context").disabled=false;}
};

$("refresh-sources").onclick=()=>{$("source-summary").textContent="正在核对已保存来源…";refresh();};
$("overview-toggle").onclick=()=>navigate({mode:"overview"});
$("context-toggle").onclick=()=>navigate({mode:"structure"});
window.addEventListener("resize",()=>{for(const graph of graphs.values())graph.ensureVisible();});
window.addEventListener("popstate",locationChanged);window.addEventListener("hashchange",locationChanged);
window.addEventListener("pagehide",()=>{if(bundle)rememberRoutePosition();});
if("scrollRestoration" in history)history.scrollRestoration="manual";
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
if(location.protocol==="file:")$("offline-note").textContent=" 当前为离线快照，不会自动更新。";
const initial=JSON.parse($("initial-data").textContent);if(initial)accept({bundle:initial,stale:false});
refresh();if(location.protocol!=="file:")setInterval(()=>{if(!document.hidden)refresh();},5000);

}
