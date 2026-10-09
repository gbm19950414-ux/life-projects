"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const navigation = require("../app/static/reader.js");
const data = {
  projects:[{id:"local:p"},{id:"pd:个人发展",read_only:true}],
  views:[{id:"v:one",project_id:"local:p"},{id:"v:two",project_id:"local:p"},{id:"pd:材料",project_id:"pd:个人发展",flow_key:null}],
  nodes:{n1:{title:"A"},n2:{title:"B"},n3:{title:"材料"},n4:{title:"未关联事项"}},
  navigation:{
    "task:one":{project_id:"local:p",view_id:"v:one",node_id:"n1",kind:"task"},
    "task:two":{project_id:"local:p",view_id:"v:two",node_id:"n2",kind:"task"},
    "material:原文 & + #":{project_id:"pd:个人发展",view_id:"pd:材料",node_id:"n3",kind:"source_material"},
    "task:unassigned":{project_id:"local:p",view_id:null,node_id:"n4",kind:"task"}
  }
};
test("Unicode and reserved characters round trip through stable URLs",()=>{
  const route={project:"pd:个人发展",view:"pd:材料",item:"material:原文 & + #"};
  assert.deepEqual(navigation.parse(navigation.format(route)),route);
});
test("empty hash is summary overview",()=>assert.equal(navigation.format(navigation.resolve(data,navigation.parse("")).route),""));
test("direct item link resolves true project, stage and node",()=>{
  const result=navigation.resolve(data,{item:"task:two"});
  assert.equal(result.error,"");assert.equal(result.route.project,"local:p");assert.equal(result.route.view,"v:two");assert.equal(result.node_id,"n2");
});
test("project mismatch cannot show another project's material",()=>{
  const result=navigation.resolve(data,{project:"local:p",item:"material:原文 & + #"});assert.ok(result.error);assert.equal(result.node_id,undefined);assert.equal(result.route.project,"local:p");
});
test("stage mismatch cannot show a different stage's task",()=>{
  const result=navigation.resolve(data,{project:"local:p",view:"v:one",item:"task:two"});assert.ok(result.error);assert.equal(result.node_id,undefined);assert.equal(result.route.view,"v:one");
});
test("unavailable project returns overview with explicit reason",()=>{
  const result=navigation.resolve(data,{project:"deleted",view:"v:one"});assert.deepEqual(result.route,{});assert.ok(result.error);
});
test("unavailable stage preserves project navigation",()=>{
  const result=navigation.resolve(data,{project:"local:p",view:"deleted"});assert.deepEqual(result.route,{project:"local:p"});assert.ok(result.error);
});
test("deleted item preserves the existing stage and reports missing evidence",()=>{
  const result=navigation.resolve(data,{project:"local:p",view:"v:one",item:"deleted"});assert.ok(result.error);assert.equal(result.route.view,"v:one");assert.equal(result.node_id,undefined);
});
test("no-graph material uses the same stable route and full node",()=>{
  const route=navigation.forNode(data,"n3","pd:个人发展","pd:材料");assert.equal(route.item,"material:原文 & + #");const result=navigation.resolve(data,route);assert.equal(result.node_id,"n3");assert.equal(result.view.flow_key,null);
});
test("graph prerequisite points to its real owning stage",()=>{
  const route=navigation.forNode(data,"n2","local:p","v:one");assert.equal(route.view,"v:two");assert.equal(route.item,"task:two");
});
test("unassigned task does not invent a stage",()=>{
  const result=navigation.resolve(data,{project:"local:p",item:"task:unassigned"});assert.equal(result.error,"");assert.equal(result.route.view,"");assert.equal(result.view,null);assert.equal(result.node_id,"n4");
});
test("parent navigation preserves the correct context at each level",()=>{
  assert.deepEqual(navigation.parent({project:"p",view:"v",item:"i"}),{project:"p",view:"v"});
  assert.deepEqual(navigation.parent({project:"p",view:"v"}),{project:"p"});
  assert.deepEqual(navigation.parent({project:"p"}),{});
});
test("unknown node aliases do not become made-up stable item IDs",()=>assert.equal(navigation.forNode(data,"unknown","local:p","v:one"),null));
test("inherited object property is not a registered item",()=>assert.ok(navigation.resolve(data,{project:"local:p",item:"constructor"}).error));
test("mapping to a missing node is not accepted",()=>{
  const broken={...data,navigation:{...data.navigation,bad:{project_id:"local:p",view_id:"v:one",node_id:"constructor"}}};
  assert.ok(navigation.resolve(broken,{project:"local:p",item:"bad"}).error);
});
test("route helpers leave business records unchanged",()=>{
  const before=JSON.stringify(data);navigation.resolve(data,{item:"task:one"});navigation.forNode(data,"n2","local:p","v:one");assert.equal(JSON.stringify(data),before);
});

test("cross-project node follows its uniquely declared source owner",()=>{
  const route=navigation.forNode(data,"n3","local:p","v:one");assert.equal(route.project,"pd:个人发展");assert.equal(route.view,"pd:材料");
});
test("legacy bundle with views still resolves its project links",()=>{
  const legacy={...data};delete legacy.projects;assert.equal(navigation.resolve(legacy,{project:"local:p"}).project.id,"local:p");
});

test("a formally blocked stage is visible as blocked in the compact summary",()=>assert.equal(navigation.stageLabel("blocked"),"受阻"));
test("all formal stage statuses have an explicit display label",()=>{
  for(const status of ["active","planned","paused","blocked","closed"])assert.notEqual(navigation.stageLabel(status),"状态待明确");
  assert.equal(navigation.stageLabel("unknown"),"状态待明确");assert.equal(navigation.stageLabel("unrecognized"),"状态待明确");
});

test('reading modes and filters round trip and survive parent navigation',()=>{
 const route={mode:'structure',direction:'d',scope:'current',q:'历史 +',project:'p',view:'v',item:'i'};
 assert.deepEqual(navigation.parse(navigation.format(route)),route);
 assert.deepEqual(navigation.parent(route),{mode:'structure',direction:'d',scope:'current',q:'历史 +',project:'p',view:'v'});
});
test('removed direction is explained without losing the original project',()=>{
 const r=navigation.resolve({...data,context:{directions:[]}},{mode:'structure',direction:'missing',project:'local:p'});
 assert.ok(r.error.includes('筛选'));assert.equal(r.route.direction,undefined);assert.equal(r.route.mode,'structure');assert.equal(r.project.id,'local:p');
});
test('multiple direction references do not create additional canonical projects',()=>{
 const fixture={...data,context:{relations:[{from:'local:p',to:'area',kind:'belongs_to',status:'proposed'},{from:'local:p',to:'d1',kind:'serves',status:'confirmed'},{from:'local:p',to:'d2',kind:'serves',status:'proposed'},{from:'area',to:'d3',kind:'serves',status:'confirmed'},{from:'local:p',to:'other-area',kind:'supports',status:'confirmed'}]}};
 const before=JSON.stringify(fixture),r=navigation.relations(fixture,'local:p');
 assert.equal(r.direct.length,2);assert.equal(r.via.length,1);assert.equal(r.via[0].via_area,'area');assert.equal(r.via[0].membership_status,'proposed');assert.equal(r.supports.length,1);assert.equal(fixture.projects.length,2);assert.equal(JSON.stringify(fixture),before);
});
test('a hundred stages retain bounded previews and exact history counts',()=>{
 const stages=Array.from({length:100},(_,i)=>({id:'s'+String(i).padStart(3,'0'),stage_status:i<30?'closed':i<32?'blocked':i<34?'planned':'active'}));
 const before=JSON.stringify(stages),p=navigation.partition(stages,true);
 assert.equal(p.visible.length,3);assert.equal(p.remaining.length,67);assert.equal(p.history.length,30);assert.equal(p.history.every(s=>s.stage_status==='closed'),true);assert.equal(JSON.stringify(stages),before);
 assert.equal(navigation.partition(stages).visible.length,70);
});
test('two thousand same-titled items can be located by exact ID and true parent path',()=>{
 const fixture={projects:[{id:'p'}],views:[],nodes:{},navigation:{},reading_model:{objects:{p:{id:'p',project_id:'p',title:'项目',kind:'project',parent_id:null}}}};
 for(let s=0;s<100;s++){const id='s'+s;fixture.views.push({id,project_id:'p'});fixture.reading_model.objects[id]={id,project_id:'p',title:'阶段',parent_id:'p',kind:'implementation_plan'};
 for(let j=0;j<20;j++){const item=id+':item:'+j;fixture.reading_model.objects[item]={id:item,project_id:'p',title:'同名事项',parent_id:id,kind:'implementation_milestone'};fixture.nodes[item]={title:'同名事项'};fixture.navigation[item]={project_id:'p',view_id:id,node_id:item};}}
 const found=navigation.search(fixture,'s99:item:19');assert.equal(found.length,1);assert.deepEqual(found[0].path.map(n=>n.id),['p','s99','s99:item:19']);
 assert.equal(navigation.resolve(fixture,{item:found[0].id}).route.view,'s99');
});
test('parallel status counts neither pick an arbitrary priority nor count materials as phases',()=>{
 const project={id:'p'},fixture={views:[{id:'a',project_id:'p',record_kind:'implementation_plan',stage_status:'active',title:'A'},{id:'b',project_id:'p',record_kind:'implementation_plan',stage_status:'planned',title:'B'},{id:'c',project_id:'p',record_kind:'implementation_plan',stage_status:'blocked',title:'C'},{id:'d',project_id:'p',record_kind:'source_material',title:'报告'}]};
 const s=navigation.summary(fixture,project);assert.equal(s.text,'1 项在推进，1 项受阻，1 项计划中');assert.equal(s.roots.length,3);assert.equal(s.materials.length,1);assert.equal(s.text.includes('A'),false);
});
test('renaming keeps stable ownership and deletion never matches a same titled item',()=>{
 const renamed=structuredClone(data);renamed.nodes.n1.title='新名称';renamed.nodes.n2.title='新名称';
 assert.equal(navigation.resolve(renamed,{item:'task:one'}).node_id,'n1');delete renamed.navigation['task:one'];
 const r=navigation.resolve(renamed,{mode:'structure',project:'local:p',view:'v:one',item:'task:one'});assert.ok(r.error);assert.equal(r.node_id,undefined);assert.equal(r.route.view,'v:one');assert.equal(r.route.mode,'structure');
});

test('direction entries deduplicate direct and inherited links without copying projects',()=>{
 const fixture={projects:[{id:'p',title:'P'},{id:'q',title:'Q'},{id:'r',title:'R'}],context:{areas:[{id:'area',title:'领域'},{id:'support',title:'支持领域'}],directions:[{id:'d1',title:'方向一'},{id:'d2',title:'方向二'}],relations:[
 {from:'p',to:'area',kind:'belongs_to',status:'proposed'},
 {from:'p',to:'d1',kind:'serves',status:'proposed'},
 {from:'p',to:'d2',kind:'serves',status:'confirmed'},
 {from:'area',to:'d1',kind:'serves',status:'confirmed'},
 {from:'q',to:'area',kind:'belongs_to',status:'confirmed'},
 {from:'r',to:'support',kind:'supports',status:'confirmed'},
 {from:'support',to:'d2',kind:'serves',status:'confirmed'}]}};
 const before=JSON.stringify(fixture),groups=navigation.structureGroups(fixture);
 assert.deepEqual(groups[0].project_ids,['p','q']);assert.deepEqual(groups[1].project_ids,['p']);
 assert.deepEqual(groups[2].project_ids,['r']);assert.equal(groups[2].direction,null);
 assert.strictEqual(groups[0].areas[0].projects[0],fixture.projects[0]);
 assert.strictEqual(groups[1].areas[0].projects[0],fixture.projects[0]);
 assert.equal(groups[2].areas[0].area,null);assert.equal(JSON.stringify(fixture),before);
 assert.deepEqual(navigation.structureGroups(fixture,'areas')[0].project_ids,['p','q','r']);
});
test('unassigned and empty directions remain readable without invented areas or links',()=>{
 const fixture={projects:[{id:'p'}],context:{areas:[],directions:[{id:'empty'}],relations:[]}};
 const g=navigation.structureGroups(fixture);assert.equal(g.length,2);assert.deepEqual(g[0].areas,[]);assert.deepEqual(g[1].project_ids,['p']);assert.equal(g[1].areas[0].area,null);
 assert.equal(navigation.structureGroups({...fixture,projects:[]})[0].project_ids.length,0);
});
test('grouping mode and legacy direction filter survive navigation and reject unknown modes',()=>{
 const route={mode:'structure',group:'areas',direction:'d',project:'local:p',view:'v:one'};
 const fixture={...data,context:{directions:[{id:'d'}]}};
 assert.deepEqual(navigation.parent(navigation.parse(navigation.format(route))),{mode:'structure',group:'areas',direction:'d',project:'local:p'});
 assert.equal(navigation.resolve(fixture,route).route.group,'areas');
 assert.equal(navigation.resolve(fixture,{...route,group:'unrecognized'}).route.group,undefined);
 assert.equal(navigation.resolve(fixture,{...route,group:'directions'}).route.project,'local:p');
});
test('appearance identities separate UI state while canonical routes stay identical',()=>{
 const a=navigation.appearanceKey('direction:a','same-project'),b=navigation.appearanceKey('direction:b','same-project');
 assert.notEqual(a,b);assert.equal(navigation.appearanceKey('direction:a','same-project'),a);
 assert.notEqual(navigation.appearanceKey(a,'goal:x'),navigation.appearanceKey(b,'goal:x'));
 assert.notEqual(navigation.appearanceKey('a:b','c'),navigation.appearanceKey('a','b:c'));
 assert.equal(navigation.appearanceKey('','goal:x'),'goal:x');
 const route={mode:'structure',group:'directions',project:'local:p',view:'v:one'};
 assert.equal(navigation.resolve(data,route).route.project,'local:p');
});
