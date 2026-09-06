// Execute the shipped inline dashboard against a deterministic DOM/API double.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {randomUUID} from 'node:crypto';

const source = readFileSync(new URL('../motor_diagnosis/web.py', import.meta.url), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
let checks = 0;
const failures = [];
const clone = value => JSON.parse(JSON.stringify(value));
function harness() {
  const calls = [], requests = [], elements = new Map(), intervals = [];
  const canvas = Object.fromEntries(['arc','beginPath','clearRect','fill','fillRect','fillText','lineTo','moveTo','setLineDash','stroke'].map(name => [name,(...args) => calls.push([name,...args])]));
  canvas.measureText = text => ({width:String(text).length * 8});
  class Element {
    children = []; textContent = ''; hidden = false; disabled = false; value = '';
    width = 900; height = 280; listeners = {}; dataset = {};
    attributes = {}; style = {};
    constructor(tag = 'div') {this.tagName = tag;}
    addEventListener(name, callback) {this.listeners[name] = callback;}
    setAttribute(name, value) {this.attributes[name] = String(value);}
    getAttribute(name) {return this.attributes[name] ?? null;}
    append(...children) {this.children.push(...children);}
    appendChild(child) {this.children.push(child);}
    replaceChildren(...children) {this.children = children; this.textContent = ''; if (this.tagName === 'select') this.value = children[0]?.value || '';}
    getContext() {return canvas;}
    getBoundingClientRect() {return {left:0,width:900};}
    click() {} remove() {}
  }
  const selectIds = new Set([...source.matchAll(/<select id="([^"]+)"/g)].map(match => match[1]));
  const get = id => {if (!elements.has(id)) elements.set(id,new Element(selectIds.has(id) ? 'select' : 'div')); return elements.get(id);};
  const context = vm.createContext({
    document:{body:new Element(),getElementById:get,createElement:tag => new Element(tag),addEventListener() {}},
    URL, URLSearchParams, atob, Blob, crypto:{randomUUID}, setTimeout, console, setInterval:fn => intervals.push(fn), confirm:() => true, alert:message => {throw new Error(message);},
    fetch:async (path,options = {}) => {
      requests.push({path,options});
      const body = await context.respond(path,options);
      return {ok:true,status:200,headers:{get:() => 'application/json'},json:async () => body,text:async () => JSON.stringify(body)};
    },
  });
  context.respond = async () => {throw new Error('Unexpected request');};
  const run = code => vm.runInContext(code,context);
  vm.runInContext(script,context);
  run(`permissions = ['*']; token = 'test-token'; sites = [{id:'S1',name:'Site 1'}];`);
  get('siteSelect').value = 'S1'; get('assetSelect').value = 'A1'; get('periodSelect').value = '24';
  get('eventSort').value = 'occurredAt_desc';
  return {run,get,context,requests,calls,intervals,Element};
}
function detail(id) {
  return {event:{id,title:`<img onerror=attack()> ${id}`,occurredAt:'2026-09-06T00:00:00Z',label:'needs_review',note:'original',durationSec:10,status:'closed'},
    context:{from:'2026-09-05T23:59:30Z',to:'2026-09-06T00:01:00Z',points:[],units:{},rawDataMissing:true,source:'unavailable'},
    featureSnapshot:{source:'saved-feature'},appliedRule:{scoreThreshold:70,hysteresis:10,durationSec:10,active:true},deviceSnapshot:{deviceId:'DEV1'},modelVersion:'score-v1'};
}
const emptyReviews = {items:[],page:1,size:20,total:0};
const emptyNotes = {items:[],latestByCategory:{},total:0};
const note = {id:'N1',category:'inspection',text:'<script>attack()</script>',author:{name:'Operator'},updatedAt:'2026-09-06T00:00:00Z',attachmentRefs:['https://example.invalid/<img>']};
function detailResponses(path) {
  if (path.includes('/anomaly/events/')) return detail(path.split('/').at(-1));
  if (path.includes('/reviews?')) return emptyReviews;
  if (path.endsWith('/notes')) return emptyNotes;
  throw new Error(path);
}
async function check(name, callback) {
  try {await callback(); checks++; console.log(`PASS ${name}`);}
  catch (error) {failures.push(name); console.error(`FAIL ${name}: ${error.stack}`);}
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve=yes; reject=no;});
  return {promise,resolve,reject};
}
function notesPage(items) {
  return {items,total:items.length,latestByCategory:Object.fromEntries(items.map(item => [item.category,item]))};
}
const textOf = element => typeof element === 'string' ? element : element.textContent + element.children.map(textOf).join(' ');
const tableRows = table => table.children.find(child => child.tagName === 'tbody').children;

await check('missing values stay missing; zero remains valid', () => {
  const h = harness();
  for (const value of ['null','undefined','false','true','[]','{}','""','" "','NaN','Infinity']) assert.equal(h.run(`finiteNumber(${value})`),null);
  assert.equal(h.run('finiteNumber(0)'),0); assert.equal(h.run('finiteNumber("0")'),0);
  assert.ok(!source.includes('.innerHTML'));
});

await check('site pagination exposes all 65 sites', () => {
  const h = harness();
  h.run(`siteSummaries = Array.from({length:65},(_,i) => ({siteName:'Site '+i})); sitePageNumber=6; renderSiteRows();`);
  assert.equal(h.get('siteRows').children.length,5);
  assert.match(h.get('sitesPage').textContent,/6 \/ 6.*65/);
  assert.equal(h.get('sitesNext').disabled,true);
  h.get('sitesPrev').listeners.click(); assert.equal(h.get('siteRows').children.length,12);
});

await check('list filters, sort and pagination use supported API keys', () => {
  const h = harness();
  h.get('severityFilter').value='critical'; h.get('reviewedFilter').value='false'; h.get('eventLabelFilter').value='needs_review';
  h.run('eventPageNumber=3');
  const query = new URLSearchParams(h.run('eventFilterParams()'));
  assert.equal(query.get('severity'),'critical'); assert.equal(query.get('reviewed'),'false');
  assert.equal(query.get('label'),'needs_review'); assert.equal(query.get('page'),'3'); assert.equal(query.get('sort'),'occurredAt_desc');
});

await check('late event detail cannot overwrite the newer selection', async () => {
  const h = harness(); let release;
  const delayed = new Promise(resolve => {release = resolve;});
  h.context.respond = path => path === '/api/anomaly/events/OLD' ? delayed : detailResponses(path);
  const older = h.run('selectEvent("OLD")');
  await h.run('selectEvent("NEW")'); release(detail('OLD')); await older;
  assert.equal(h.run('selectedEventId'),'NEW'); assert.equal(h.run('selectedDetail.event.id'),'NEW');
  assert.match(h.get('eventDetail').children[0].textContent,/NEW/);
  assert.equal(h.get('eventDetail').children[0].children.length,0);
  assert.match(textOf(h.get('eventDetail')),/요약 시계열 없음/);
  assert.match(h.get('eventEvidence').textContent,/saved-feature/);
});

await check('failed detail load cannot enable review of stale data', async () => {
  const h = harness(); h.context.respond = detailResponses;
  await h.run('selectEvent("OLD")');
  h.context.respond = async () => {throw new Error('detail unavailable');};
  await assert.rejects(h.run('selectEvent("NEW")'),/unavailable/);
  assert.equal(h.run('selectedDetail'),null); assert.equal(h.get('saveReview').disabled,true);
});

await check('review sends reason and keeps independent memo drafts safe', async () => {
  const h = harness(); h.context.respond = (path,options) => options.method === 'POST' ? {} : detailResponses(path);
  await h.run('selectEvent("E1")'); h.run('render = async () => {}');
  h.get('reviewReason').value='Confirmed during inspection'; h.get('labelSelect').value='confirmed_anomaly';
  h.run('memoDirty=true'); await h.get('saveReview').listeners.click();
  assert.ok(!h.requests.some(request => request.options.method === 'POST'));
  h.run('memoDirty=false'); await h.get('saveReview').listeners.click();
  const saved = h.requests.find(request => request.options.method === 'POST');
  assert.equal(saved.path,'/api/events/E1/review');
  assert.equal(JSON.parse(saved.options.body).reason,'Confirmed during inspection');
});

await check('notes render text safely and support edit/history/delete', async () => {
  const h = harness(); h.context.notes = {items:[note],latestByCategory:{inspection:note},total:1};
  h.context.respond = (path,options) => path.endsWith('/history') ? {items:[{version:1,text:note.text}]} : options.method === 'DELETE' ? {} : emptyNotes;
  h.run('selectedEventId="E1"; renderNotes(notes)');
  assert.match(h.get('eventNotes').children[0].children[0].textContent,/\[최신\].*inspection/);
  assert.equal(h.get('eventNotes').children[0].children[0].children.length,0);
  await h.run('noteAction("N1","edit")'); assert.equal(h.get('memoText').value,note.text);
  await h.run('noteAction("N1","history")'); assert.match(h.get('noteHistory').textContent,/version/);
  await h.run('noteAction("N1","delete")');
  assert.ok(h.requests.some(request => request.path === '/api/events/E1/notes/N1' && request.options.method === 'DELETE'));
});

await check('review history loads subsequent pages without truncating at 20', async () => {
  const h = harness(); h.context.respond = () => ({items:[{id:'R21'}],page:2,size:20,total:21});
  h.run('selectedEventId="E1"; reviewPage=1; renderReviewHistory({items:[{id:"R1"}],page:1,size:20,total:21})');
  assert.equal(h.get('reviewsMore').hidden,false);
  await h.get('reviewsMore').listeners.click();
  assert.equal(h.get('reviewHistory').children.length,2); assert.equal(h.get('reviewsMore').hidden,true);
  assert.match(h.requests[0].path,/page=2/);
});

await check('event spans, thresholds, zoom and cursor survive refresh', () => {
  const h = harness();
  h.run(`allPoints = Array.from({length:11},(_,i)=>({timestamp:new Date(Date.UTC(2026,8,6,0,0,i)).toISOString(),anomalyScore:80,rpm:i===5?null:1800}));
    currentRule={scoreThreshold:70,hysteresis:10,durationSec:10,active:true};
    chartEvents=[{id:'E1',occurredAt:'2026-09-06T00:00:01Z',endAt:'2026-09-06T00:00:05Z'}]; redrawChart(); chartSelectionAt=Date.UTC(2026,8,6,0,0,5); zoomChart(.5);`);
  const viewport = clone(h.run('chartViewport'));
  h.run('redrawChart()'); assert.deepEqual(clone(h.run('chartViewport')),viewport);
  assert.equal(h.run('chartSelectionAt'),Date.UTC(2026,8,6,0,0,5));
  assert.ok(h.calls.some(call => call[0] === 'fillRect' && call[3] > 1 && call[3] < 900));
  assert.ok(h.calls.some(call => call[0] === 'fillText' && String(call[1]).includes('진입 70')));
  assert.ok(h.calls.some(call => call[0] === 'fillText' && String(call[1]).includes('종료 60')));
});

await check('export snapshots include exact scope, zoom, dataset and format', () => {
  const h = harness(); h.get('datasetSelect').value='DATASET-1'; h.get('exportFormat').value='xlsx';
  h.run(`lastQuery={siteId:'S1',assetId:'A1',from:'2026-09-06T00:00:00Z',to:'2026-09-06T01:00:00Z'}; chartViewport={firstAt:Date.UTC(2026,8,6,0,10),lastAt:Date.UTC(2026,8,6,0,20)};`);
  const query = clone(h.run('exportQuery()'));
  assert.equal(query.from,'2026-09-06T00:10:00.000Z'); assert.equal(query.to,'2026-09-06T00:20:00.000Z');
  assert.equal(query.format,'xlsx'); assert.equal(query.datasetId,'DATASET-1');
  h.get('assetSelect').value='A2'; assert.throws(() => h.run('exportQuery()'),/먼저 조회/);
});

await check('read-only roles and device-managed parameters cannot write', async () => {
  const h = harness();
  h.run(`permissions=['parameter:read']; setupManagement();`);
  h.get('managementKind').value='parameter';
  h.context.respond = () => [{key:'RETENTION_RAW_DAYS',value:30,type:'integer',mutable:true}];
  await h.run('loadManagement()'); assert.equal(h.get('managementSave').hidden,true);
  await h.run('saveManagement()'); assert.equal(h.requests.length,1);
  h.run(`permissions=['*']`); h.context.respond = () => [{key:'EDGE_BUFFER_HOURS',value:24,type:'integer',mutable:false,managedBy:'firmware'}];
  await h.run('loadManagement()'); assert.equal(h.get('managementSave').hidden,true);
  assert.match(h.get('managementStatus').textContent,/firmware/); await h.run('saveManagement()'); assert.equal(h.requests.length,2);
});

await check('management uses typed changed fields and the captured resource', async () => {
  const h = harness(); h.get('managementKind').value='rule';
  h.context.respond = () => ({assetId:'A1',version:'RULE-v1',scoreThreshold:70,hysteresis:10,durationSec:10,mergeWindowSec:5,active:true});
  await h.run('loadManagement()');
  h.run(`managementInputs.find(item=>item.definition.key==='durationSec').input.value='12';`);
  assert.deepEqual(clone(h.run('managementPayload()')),{durationSec:12});
  h.run(`managementInputs.find(item=>item.definition.key==='durationSec').input.value='';`);
  assert.throws(() => h.run('managementPayload()'),/숫자/);
  h.run(`managementInputs.find(item=>item.definition.key==='durationSec').input.value='12';`);
  h.get('managementReason').value='Observed duration';
  h.run('loadManagement=async()=>{}; render=async()=>{}'); await h.run('saveManagement()');
  const write = h.requests.find(request => request.options.method === 'PUT');
  assert.equal(write.path,'/api/anomaly/rules/A1'); assert.equal(JSON.parse(write.options.body).durationSec,12);
});

await check('late management response is discarded after scope change', async () => {
  const h = harness(); let release;
  h.get('managementKind').value='rule'; h.context.respond = () => new Promise(resolve => {release=resolve;});
  const pending = h.run('loadManagement()'); h.run('invalidateScope()'); release({assetId:'A1'}); await pending;
  assert.equal(h.run('managementScope'),null); assert.equal(h.get('managementSave').hidden,true);
});

await check('render races preserve newer rows and user drafts', async () => {
  const h = harness(); let release;
  h.context.respond = path => {
    const url = new URL(path,'http://local');
    if (url.pathname === '/api/telemetry') return url.searchParams.get('assetId') === 'A1' ? new Promise(resolve => {release=resolve;}) : {points:[],units:{}};
    if (url.pathname === '/api/dashboard/sites-summary') return [];
    if (url.pathname === '/api/events') return {items:[{id:url.searchParams.get('assetId'),occurredAt:'2026-09-06T00:00:00Z'}],page:1,size:12,total:1};
    if (url.pathname.startsWith('/api/anomaly/rules/')) return {};
    if (url.pathname.endsWith('/devices')) return [];
    if (url.pathname === '/api/health/dependencies') return {};
    return {items:[]};
  };
  h.run(`selectedEventId='E1'; reviewDirty=true; $("noteInput").value='draft';`);
  const first = h.run('render()'); h.get('assetSelect').value='A2'; await h.run('render()');
  release({points:[],units:{}}); await first;
  assert.equal(h.run('events[0].id'),'A2'); assert.equal(h.get('noteInput').value,'draft');
  assert.equal(h.run('selectedEventId'),'E1');
});

await check('typing during review save retains the new draft', async () => {
  const h = harness(); let release;
  h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.get('reviewReason').value = 'First reason'; h.get('noteInput').value = 'First note';
  h.context.respond = (path, options) => options.method === 'POST' ? new Promise(resolve => {release=resolve;}) : detailResponses(path);
  const pending = h.get('saveReview').listeners.click();
  h.get('noteInput').value = 'Next draft'; h.run('reviewDirty=true');
  release({}); await pending;
  assert.equal(h.get('noteInput').value,'Next draft'); assert.equal(h.run('reviewDirty'),true);
  assert.match(h.get('appStatus').textContent,/아직 저장되지/);
});

await check('typing during note save retains the new draft without double submission', async () => {
  const h = harness(); let release;
  h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.get('memoText').value = 'First memo'; h.get('noteCategory').value = 'inspection';
  h.context.respond = (path, options) => options.method === 'POST' ? new Promise(resolve => {release=resolve;}) : emptyNotes;
  const pending = h.get('saveNote').listeners.click();
  h.get('memoText').value = 'Next memo'; h.run('memoDirty=true');
  await h.get('saveNote').listeners.click(); release({}); await pending;
  assert.equal(h.get('memoText').value,'Next memo'); assert.equal(h.run('memoDirty'),true);
  assert.equal(h.requests.filter(item => item.options.method === 'POST').length,1);
});

await check('management save cannot reset a subsequently selected record', async () => {
  const h = harness(); let release;
  h.get('managementKind').value = 'parameter';
  h.context.respond = () => [{key:'ONE',type:'integer',value:1,mutable:true},{key:'TWO',type:'integer',value:2,mutable:true}];
  await h.run('loadManagement()'); h.run('managementInputs[0].input.value="3"');
  h.get('managementReason').value='Change first record';
  h.context.respond = () => new Promise(resolve => {release=resolve;});
  const pending = h.run('saveManagement()');
  h.get('managementRecord').value='TWO'; h.run('editManagement(false)');
  h.run('managementInputs[0].input.value="4"; managementDirty=true');
  release({}); await pending;
  assert.equal(h.run('managementRow.key'),'TWO');
  assert.equal(h.run('managementInputs[0].input.value'),'4'); assert.equal(h.run('managementDirty'),true);
});

await check('master registration works without existing sites or assets', async () => {
  const h = harness(); h.run('sites=[]'); h.get('siteSelect').value=''; h.get('assetSelect').value='';
  h.get('managementKind').value='site'; h.context.respond = () => [];
  await h.run('loadManagement()'); assert.equal(h.get('managementNew').hidden,false);
  h.run('editManagement(true)'); assert.equal(h.get('managementSave').hidden,false);
  h.get('siteSelect').value='NEW-SITE'; h.get('managementKind').value='asset';
  await h.run('loadManagement()'); h.run('editManagement(true)');
  assert.equal(h.run('managementScope.query.siteId'),'NEW-SITE');
  assert.equal(h.get('managementSave').hidden,false);
});

await check('scope invalidation clears stale signals while preserving site summary', () => {
  const h = harness(); h.run('allPoints=[{timestamp:"2026-09-06T00:00:00Z",rpm:1800}]; events=[{id:"OLD"}]; siteSummaries=[{siteName:"S1"}]; invalidateScope();');
  assert.equal(h.run('allPoints.length'),0); assert.equal(h.run('events.length'),0);
  assert.equal(h.run('siteSummaries.length'),1); assert.equal(h.run('lastQuery'),null);
});

await check('device health exposes zero readings and sensor fault count', () => {
  const h = harness(); h.run('renderHealth([{deviceId:"D1",health:"online",rssiDbm:0,rebootCount:0,bufferUsagePct:0,sensorHealth:"fault",activeSensorFaults:[{}]}],{})');
  assert.deepEqual(tableRows(h.get('deviceHealth').children[0])[0].children.map(cell => cell.textContent), ['D1 / —','온라인','—','0 dBm','0회','0%','고장 / 1건']);
});

await check('a memo started during review save is not discarded', async () => {
  const h = harness(); let release;
  h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.get('reviewReason').value='Reviewed';
  h.context.respond = () => new Promise(resolve => {release=resolve;});
  const pending = h.get('saveReview').listeners.click();
  h.get('memoText').value='Independent memo draft'; h.run('memoDirty=true');
  release({}); await pending;
  assert.equal(h.get('memoText').value,'Independent memo draft'); assert.equal(h.run('memoDirty'),true);
});

await check('rollout PUT keeps its required unchanged fields', async () => {
  const h = harness(); h.get('managementKind').value='rollout';
  h.context.respond = () => ({networkProfileId:'NET-STORE-FWD',targetAssetIds:['A1'],installPriority:'normal',note:'old'});
  await h.run('loadManagement()'); h.run('managementInputs.find(item=>item.definition.key==="note").input.value="new"');
  assert.deepEqual(clone(h.run('managementPayload()')),{networkProfileId:'NET-STORE-FWD',targetAssetIds:['A1'],installPriority:'normal',note:'new'});
});

await check('confirmed POST/PATCH is not repeated after the following list GET fails', async () => {
  for (const method of ['POST','PATCH']) {
    const h = harness(); h.context.respond = detailResponses; await h.run('selectEvent("E1")');
    if (method === 'PATCH') h.run('editingNoteId="N1"');
    h.get('memoText').value='Saved once'; h.get('noteCategory').value='inspection'; h.run('memoDirty=true');
    h.context.respond = (path,options) => {
      if (options.method === method) return {...note,text:'Saved once'};
      throw new Error('list unavailable');
    };
    await h.get('saveNote').listeners.click();
    assert.equal(h.run('memoDirty'),false); assert.equal(h.get('memoText').value,'');
    assert.match(h.get('notesStatus').textContent,/저장.*완료.*목록.*실패/);
    await h.get('saveNote').listeners.click();
    assert.equal(h.requests.filter(item => item.options.method === method).length,1);
    h.context.respond = () => notesPage([note]); await h.get('notesRefresh').listeners.click();
    assert.deepEqual(clone(h.run('noteRows.map(item=>item.id)')),['N1']);
    assert.equal(h.requests.filter(item => item.options.method === method).length,1);
  }
});

await check('a failed note write retains its draft and can be retried', async () => {
  const h = harness(); h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.get('memoText').value='Retry me'; h.get('noteCategory').value='inspection'; h.run('memoDirty=true');
  h.context.respond = () => {throw new Error('write failed');};
  await h.get('saveNote').listeners.click();
  assert.equal(h.run('memoDirty'),true); assert.equal(h.get('memoText').value,'Retry me');
  h.context.respond = (path,options) => options.method === 'POST' ? note : notesPage([note]);
  await h.get('saveNote').listeners.click();
  assert.equal(h.run('memoDirty'),false);
  assert.equal(h.requests.filter(item => item.options.method === 'POST').length,2);
});

await check('confirmed write plus failed refresh preserves only the newer draft', async () => {
  const h = harness(), write = deferred(); h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.get('memoText').value='First draft'; h.get('noteCategory').value='inspection'; h.run('memoDirty=true');
  h.context.respond = (path,options) => {
    if (options.method === 'POST') return write.promise;
    throw new Error('list failed');
  };
  const pending = h.get('saveNote').listeners.click(); h.get('memoText').value='Second draft';
  write.resolve(note); await pending;
  assert.equal(h.get('memoText').value,'Second draft'); assert.equal(h.run('memoDirty'),true);
  assert.match(h.get('notesStatus').textContent,/저장.*완료.*목록.*실패/);
  h.context.respond = (path,options) => options.method === 'POST' ? {...note,id:'NEW'} : notesPage([{...note,id:'NEW'}]);
  await h.get('saveNote').listeners.click();
  const payloads = h.requests.filter(item=>item.options.method === 'POST').map(item=>JSON.parse(item.options.body).text);
  assert.deepEqual(payloads,['First draft','Second draft']);
});

await check('late delete refresh cannot erase the subsequently saved note', async () => {
  const h = harness(), oldList = deferred(), listStarted = deferred();
  h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.context.page = notesPage([note]); h.run('renderNotes(page)');
  h.context.respond = (path,options) => {
    if (options.method === 'DELETE') return {deleted:true};
    listStarted.resolve(); return oldList.promise;
  };
  const deletion = h.run('noteAction("N1","delete")'); await listStarted.promise;
  const saved = {...note,id:'NEW',text:'New note'};
  h.context.respond = (path,options) => options.method === 'POST' ? saved : notesPage([saved]);
  h.get('memoText').value='New note'; h.get('noteCategory').value='inspection';
  await h.get('saveNote').listeners.click();
  assert.deepEqual(clone(h.run('noteRows.map(item=>item.id)')),['NEW']);
  oldList.resolve(emptyNotes); await deletion;
  assert.deepEqual(clone(h.run('noteRows.map(item=>item.id)')),['NEW']);
});

await check('failed filter or page requests cannot retain selectable old events', async () => {
  for (const control of ['severityFilter','eventLabelFilter','reviewedFilter','eventSort','eventsNext']) {
    const h = harness(); h.context.respond = detailResponses; await h.run('selectEvent("OLD")');
    h.run('events=[{id:"OLD",title:"Old warning",severity:"warning"}]; eventTotal=24; renderEvents(); renderPager("events",1,24); rememberSelection();');
    h.get(control).value = {severityFilter:'critical',eventLabelFilter:'confirmed_anomaly',reviewedFilter:'true',eventSort:'score_desc'}[control] || '';
    h.context.respond = () => {throw new Error('filtered request failed');};
    await h.get(control).listeners[control === 'eventsNext' ? 'click' : 'change']();
    assert.equal(h.run('events.length'),0,control); assert.equal(h.get('events').children.length,0,control);
    assert.equal(h.run('selectedEventId'),null); assert.equal(h.get('saveReview').disabled,true);
    assert.equal(h.get('eventsNext').disabled,true); assert.equal(h.get('eventsPrev').disabled,true);
    assert.match(h.get('appStatus').textContent,/filtered request failed/);
  }
});

await check('the submitted draft is cleared before a delayed GET and subsequent input survives', async () => {
  const h = harness(), list = deferred(), started = deferred();
  h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.get('memoText').value='Submitted'; h.get('noteCategory').value='inspection'; h.run('memoDirty=true');
  h.context.respond = (path,options) => {
    if (options.method === 'POST') return note;
    started.resolve(); return list.promise;
  };
  const pending = h.get('saveNote').listeners.click(); await started.promise;
  assert.equal(h.get('memoText').value,''); assert.equal(h.run('memoDirty'),false);
  h.get('memoText').value='Typed during refresh'; h.run('memoDirty=true');
  list.resolve(notesPage([note])); await pending;
  assert.equal(h.get('memoText').value,'Typed during refresh'); assert.equal(h.run('memoDirty'),true);
});

await check('late note failures and responses from another event cannot replace current state', async () => {
  const h = harness(), failed = deferred();
  h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.context.respond = () => failed.promise;
  const older = h.run('refreshNotes()');
  h.context.respond = () => notesPage([note]); await h.run('refreshNotes()');
  failed.reject(new Error('stale failure')); await older;
  assert.deepEqual(clone(h.run('noteRows.map(item=>item.id)')),['N1']);
  assert.ok(!h.get('notesStatus').textContent.includes('stale failure'));
  assert.equal(h.get('notesRefresh').disabled,false);
  const oldEventList = deferred(); h.context.respond = () => oldEventList.promise;
  const oldEvent = h.run('refreshNotes()');
  h.context.respond = path => path === '/api/events/E2/notes' ? notesPage([{...note,id:'E2-NOTE'}]) : detailResponses(path);
  await h.run('selectEvent("E2")'); oldEventList.resolve(notesPage([note])); await oldEvent;
  assert.deepEqual(clone(h.run('noteRows.map(item=>item.id)')),['E2-NOTE']);
});

await check('a pre-filter response stays discarded after failure and refresh recovers the new filter', async () => {
  const h = harness(), oldTelemetry = deferred();
  const warning = {id:'OLD',title:'Warning',severity:'warning'}, critical = {id:'NEW',title:'Critical',severity:'critical'};
  function response(path) {
    const url = new URL(path,'http://local');
    if (url.pathname === '/api/telemetry') return {points:[],units:{}};
    if (url.pathname === '/api/events') return {items:[critical],page:1,size:12,total:1};
    if (url.pathname === '/api/dashboard/sites-summary' || url.pathname.endsWith('/devices')) return [];
    if (url.pathname.startsWith('/api/anomaly/rules/') || url.pathname === '/api/health/dependencies') return {};
    return {items:[]};
  }
  h.context.warning = warning; h.run('events=[warning]; eventTotal=1; renderEvents();');
  h.get('severityFilter').value='warning'; h.run('rememberSelection()');
  h.context.respond = path => path.startsWith('/api/telemetry?') ? oldTelemetry.promise : response(path);
  const previous = h.run('render()');
  h.get('severityFilter').value='critical'; h.context.respond = () => {throw new Error('filter unavailable');};
  await h.get('severityFilter').listeners.change();
  oldTelemetry.resolve({points:[],units:{}}); await previous;
  assert.equal(h.run('events.length'),0); assert.equal(h.get('eventsPage').textContent,'조회 실패');
  h.context.respond = response; await h.get('refreshBtn').listeners.click();
  assert.deepEqual(clone(h.run('events.map(item=>item.id)')),['NEW']);
  assert.equal(h.get('severityFilter').value,'critical'); assert.match(h.get('eventsPage').textContent,/1.*1건/);
  const queries = h.requests.filter(item=>item.path.startsWith('/api/events?'));
  assert.ok(queries.some(item=>new URL(item.path,'http://local').searchParams.get('severity') === 'critical'));
});

await check('workspace navigation is wired to unique existing panels', () => {
  const ids = [...source.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size,ids.length);
  const h = harness();
  for (const config of Object.values(clone(h.run('WORKSPACE_VIEWS')))) {
    assert.ok(ids.includes(config.button));
    assert.equal(typeof h.get(config.button).listeners.click,'function');
    const button = source.match(new RegExp(`<button id="${config.button}"[^>]+>`))[0];
    const controlled = button.match(/aria-controls="([^"]+)"/)[1].split(' ');
    assert.deepEqual(controlled,config.panels);
    for (const id of config.panels) assert.ok(ids.includes(id));
  }
  assert.match(source,/\[hidden\] \{ display:none !important;/);
  assert.match(source,/@media \(max-width:900px\)/);
});

await check('each workspace reveals only its panels without new API requests', () => {
  const h = harness(); h.run('setupManagement()');
  assert.equal(h.get('managementPanel').hidden,true);
  const views = clone(h.run('WORKSPACE_VIEWS'));
  for (const view of ['events','management','overview']) {
    h.get(views[view].button).listeners.click();
    for (const [name,config] of Object.entries(views)) {
      assert.equal(h.get(config.button).getAttribute('aria-pressed'),String(name === view));
      for (const panel of config.panels) assert.equal(h.get(panel).hidden,name !== view,panel);
    }
    assert.equal(h.get('viewHeading').textContent,views[view].title);
  }
  assert.equal(h.requests.length,0);
  h.run('setView("not-a-view")'); assert.equal(h.run('currentView'),'overview');
});

await check('navigation preserves review, memo, management drafts and chart selection', async () => {
  const h = harness(); h.context.respond = detailResponses; await h.run('selectEvent("E1")');
  h.run(`allPoints=Array.from({length:11},(_,i)=>({timestamp:new Date(Date.UTC(2026,8,6,0,0,i)).toISOString(),rpm:1000+i}));
    redrawChart(); zoomChart(.5); chartSelectionAt=Date.UTC(2026,8,6,0,0,5);
    reviewDirty=true; memoDirty=true; managementDirty=true; eventPageNumber=3; editingNoteId='N1';`);
  for (const [id,value] of Object.entries({noteInput:'review draft',reviewReason:'review reason',memoText:'memo draft',attachmentRefs:'survey://draft',managementReason:'management draft',severityFilter:'critical'})) h.get(id).value=value;
  const before = clone(h.run('[chartViewport,chartSelectionAt,detailGeneration,noteListGeneration,managementGeneration,selectedDetail,eventPageNumber]'));
  const calls = h.requests.length;
  h.context.confirm = () => {throw new Error('Navigation must not discard or confirm drafts');};
  h.run('setView("events"); setView("management"); setView("overview"); setView("events")');
  assert.deepEqual(clone(h.run('[chartViewport,chartSelectionAt,detailGeneration,noteListGeneration,managementGeneration,selectedDetail,eventPageNumber]')),before);
  assert.equal(h.get('memoText').value,'memo draft'); assert.equal(h.get('noteInput').value,'review draft');
  assert.equal(h.get('managementReason').value,'management draft'); assert.equal(h.get('reviewReason').value,'review reason');
  assert.equal(h.get('attachmentRefs').value,'survey://draft'); assert.equal(h.get('severityFilter').value,'critical');
  assert.deepEqual(clone(h.run('[reviewDirty,memoDirty,managementDirty,editingNoteId]')),[true,true,true,'N1']);
  assert.equal(h.requests.length,calls);
});

await check('management navigation respects read access including audit-only users', () => {
  const h = harness(); h.run('permissions=["event:read"]; setupManagement(); setView("management")');
  assert.equal(h.get('navManagement').hidden,true); assert.equal(h.get('managementPanel').hidden,true);
  assert.equal(h.run('currentView'),'overview');
  h.run('permissions=["audit-log:read"]; setupManagement(); setView("management")');
  assert.equal(h.get('navManagement').hidden,false); assert.equal(h.get('auditPanel').hidden,false);
  assert.equal(h.get('managementKind').children.length,0); assert.equal(h.run('can("site:write")'),false);
  h.run('permissions=["event:read"]; setupManagement()');
  assert.equal(h.run('currentView'),'overview'); assert.equal(h.get('managementPanel').hidden,true);
});

await check('health tables distinguish missing readings, zero and reported degraded states', () => {
  const h = harness();
  h.run(`renderHealth([{deviceId:'<img onerror=attack()>',rssiDbm:null,rebootCount:null,bufferUsagePct:null}],{
    status:'degraded',checkedAt:null,quarantinedMessageCount:0,
    dependencies:[{id:'storage',status:'degraded',latencyMs:0,errorRatePct:0,lastFailureAt:null,lastRecoveryAt:null,impactScope:'<script>attack()</script>'}]
  })`);
  const device = tableRows(h.get('deviceHealth').children[0])[0];
  assert.equal(device.children[0].textContent,'<img onerror=attack()> / —'); assert.equal(device.children[0].children.length,0);
  assert.deepEqual(device.children.slice(3,6).map(cell=>cell.textContent),['미수신','미수신','미수신']);
  assert.equal(device.children[6].textContent,'— / 미수신');
  const service = tableRows(h.get('serviceHealth').children[1])[0];
  assert.deepEqual(service.children.map(cell=>cell.textContent),['storage','성능 저하','0 ms','0%','—','—','<script>attack()</script>']);
  assert.equal(service.children.at(-1).children.length,0);
  assert.match(textOf(h.get('serviceHealth').children[0]),/격리된 메시지.*0건/);
  h.run('renderHealth([],{error:"권한 없음"})');
  assert.equal(h.get('deviceHealth').children.length,0); assert.equal(h.get('serviceHealth').children.length,0);
  assert.equal(h.get('serviceHealth').textContent,'권한 없음');
});

await check('evidence summary uses the selected snapshot and clears when selection changes', async () => {
  const h = harness(); h.context.respond = path => path.includes('/anomaly/events/') ? {...detail('E1'),appliedRule:{version:'RULE-0',scoreThreshold:0,durationSec:0,active:false}} : detailResponses(path);
  await h.run('selectEvent("E1")');
  const summary = textOf(h.get('evidenceSummary'));
  assert.match(summary,/모델 버전.*score-v1/); assert.match(summary,/규칙 버전.*RULE-0/);
  assert.match(summary,/진입 점수 임계값.*0/); assert.match(summary,/규칙 지속시간.*0초/);
  assert.match(summary,/규칙 활성.*비활성/); assert.match(summary,/신호 출처.*unavailable/);
  h.run('clearEventSelection()'); assert.equal(h.get('evidenceSummary').children.length,0);
  assert.equal(h.run('formatLocalTime(null)'),'—'); assert.equal(h.run('formatLocalTime(undefined)'),'—');
  assert.equal(h.run('formatLocalTime("")'),'—'); assert.notEqual(h.run('formatLocalTime(0)'),'—');
});

await check('review cards show actor, reason and label transitions as safe text', () => {
  const h = harness(); h.run(`renderReviewHistory({items:[{id:'R1',changedAt:null,actor:{name:'<img>'},before:{label:'needs_review'},after:{label:'confirmed_anomaly',note:'<script>draft</script>'},reason:'checked sensor'}],page:1,size:20,total:1})`);
  const card = h.get('reviewHistory').children[0], summary = textOf(card.children[1]);
  assert.equal(card.children[0].textContent,'— · <img>'); assert.equal(card.children[0].children.length,0);
  assert.match(summary,/이전 라벨.*검수 필요.*변경 라벨.*이상 확인/);
  assert.match(summary,/변경 사유.*checked sensor/); assert.match(summary,/<script>draft<\/script>/);
  assert.equal(card.children[2].tagName,'details');
});

await check('audit tables render latest requested page and retain raw before-after evidence', async () => {
  const h = harness(), old = deferred(); h.context.respond = () => old.promise;
  const pending = h.run('loadAudit()');
  h.context.respond = () => ({items:[{id:'NEW',actor:{name:'Operator'},action:'update',targetType:'asset',targetId:'A1',reason:'<script>reason</script>',before:{ratedRpm:1000},after:{ratedRpm:1200}}],page:2,total:13});
  h.run('auditPageNumber=2'); await h.run('loadAudit()');
  old.resolve({items:[{id:'OLD',reason:'outdated'}],page:1,total:1}); await pending;
  const row = tableRows(h.get('auditRows').children[0])[0];
  assert.deepEqual(row.children.map(cell=>cell.textContent),['—','Operator','update','asset / A1','<script>reason</script>']);
  assert.equal(row.children.at(-1).children.length,0);
  assert.match(textOf(h.get('auditRows').children[1]),/ratedRpm.*1000/s);
  assert.match(h.get('auditPage').textContent,/2 \/ 2.*13/);
});

await check('a background render never reopens overview or clears hidden event drafts', async () => {
  const h = harness(); h.run('setView("events"); memoDirty=true'); h.get('memoText').value='hidden draft';
  h.context.respond = path => {
    if (path.startsWith('/api/telemetry')) return {points:[],units:{}};
    if (path.startsWith('/api/events')) return {items:[],page:1,size:12,total:0};
    if (path.includes('sites-summary') || path.endsWith('/devices')) return [];
    if (path.includes('/anomaly/rules/') || path.includes('/health/dependencies')) return {};
    return {items:[]};
  };
  h.run('setView("management")'); await h.run('render()');
  assert.equal(h.run('currentView'),'management'); assert.equal(h.get('managementPanel').hidden,false);
  assert.equal(h.get('siteKpis').hidden,true); assert.equal(h.get('eventReviewPanel').hidden,true);
  assert.equal(h.get('memoText').value,'hidden draft'); assert.equal(h.run('memoDirty'),true);
});

await check('RPM quality keeps a measured stop but gaps stale and invalid values', () => {
  const h = harness();
  assert.equal(h.run('rpmValue({rpm:0,rpmStatus:"valid"})'),0);
  assert.equal(h.run('rpmValue({rpm:1450.25,rpmStatus:"valid"})'),1450.25);
  for (const status of ['stale','invalid','unavailable','unexpected',null]) {
    assert.equal(h.run(`rpmValue({rpm:1450,rpmStatus:${JSON.stringify(status)}})`),null);
  }
  assert.equal(h.run('rpmValue({rpm:null,rpmStatus:"valid"})'),null);
  assert.match(h.run('rpmDescription({rpm:0,rpmStatus:"valid"})'),/RPM: 0 \(유효 보고값\)/);
});

await check('legacy RPM never acquires an invented measured status or rated fallback', () => {
  const h = harness();
  assert.equal(h.run('rpmValue({rpm:1500})'),1500);
  assert.equal(h.run('rpmValue({rpm:null,ratedRpm:1800})'),null);
  assert.match(h.run('rpmDescription({rpm:1500})'),/상태정보 없음/);
  assert.match(h.run('rpmDescription({rpm:null,rpmStatus:"unavailable"})'),/미취득/);
});

await check('RPM hover displays original measurement time and source as text', () => {
  const h = harness();
  h.run(`latestPoints=[{timestamp:'2026-09-06T00:02:00Z',rpm:0,rpmStatus:'valid',rpmMeasuredAt:'2026-09-06T00:00:00Z',rpmSource:'<img onerror=attack()>'}];`);
  h.get('chart').listeners.mousemove({clientX:100});
  const hint = h.get('chartHint');
  assert.match(hint.textContent,/RPM: 0 \(유효 보고값\)/);
  assert.match(hint.textContent,/RPM 측정 시각: 2026-09-06T00:00:00Z/);
  assert.match(hint.textContent,/RPM 출처: <img onerror=attack\(\)>/);
  assert.equal(hint.children.length,0);
});

await check('shipped chart uses RPM quality rather than a leftover numeric value', () => {
  const h = harness();
  h.run(`draw([{timestamp:'2026-09-06T00:00:00Z',rpm:999,rpmStatus:'stale'},{timestamp:'2026-09-06T00:00:01Z',rpm:777,rpmStatus:'invalid'}], {});`);
  assert.ok(!h.calls.some(call => call[0]==='fillText' && String(call[1]).includes('RPM')));
  assert.equal(h.calls.filter(call => call[0]==='arc').length,0);
  h.run(`draw([{timestamp:'2026-09-06T00:00:00Z',rpm:0,rpmStatus:'valid'},{timestamp:'2026-09-06T00:00:01Z',rpm:777,rpmStatus:'invalid'}], {});`);
  assert.ok(h.calls.some(call => call[0]==='fillText' && String(call[1]).includes('RPM (보고값)')));
  assert.equal(h.calls.filter(call => call[0]==='arc').length,1);
});

function modelResult(version = 'M1') {
  return {version,status:'draft',approvalStatus:'pending',approvalRevision:0,registrationDigest:'sha256:abc',deploymentStatus:'not_deployed',artifactVerified:false,artifactChecksum:'sha256:def',submission:{jobId:`job-${version}`},metrics:{f1:0.5},reviewHistory:[]};
}
function modelHarness() {
  const h = harness(); h.context.fixture = modelResult(); h.get('modelStatusFilter').value='draft';
  h.context.respond = async () => clone(h.context.fixture);
  return h;
}
await check('AI result queue filters pages and safely displays submitted identifiers', async () => {
  const h = modelHarness(), row = modelResult('<img onerror=attack()>');
  h.context.respond = async () => ({items:[row],total:15});
  await h.run('loadModelQueue(true)');
  assert.match(h.requests[0].path,/siteId=S1&assetId=A1&status=draft&page=1&size=12/);
  assert.match(textOf(h.get('modelQueue')),/<img onerror=attack/);
  assert.equal(h.get('modelQueue').children[0].children.length,0);
  assert.equal(h.get('modelQueueNext').disabled,false);
});
await check('AI failed filter lookup clears old selectable results and approval controls', async () => {
  const h = modelHarness(); await h.run('selectModelReview("M1")');
  h.context.respond = async () => {throw new Error('offline');}; h.get('modelStatusFilter').value='approved';
  await h.run('loadModelQueue(true)');
  assert.equal(h.run('modelQueueRows.length'),0); assert.equal(h.run('modelReviewRow'),null);
  assert.match(h.get('modelQueue').textContent,/조회 실패/); assert.equal(h.get('modelApprove').disabled,true);
});
await check('AI late queue and detail replies cannot overwrite newer selection or scope', async () => {
  const h = modelHarness(), late = deferred();
  h.context.respond = path => path.endsWith('/M1') ? late.promise : Promise.resolve(modelResult('M2'));
  const older = h.run('selectModelReview("M1")'); await h.run('selectModelReview("M2")');
  late.resolve(modelResult('M1')); await older; assert.equal(h.run('modelReviewRow.version'),'M2');
  const list = deferred(); h.context.respond = () => list.promise;
  const pending = h.run('loadModelQueue()'); h.get('assetSelect').value='A2'; h.run('invalidateScope()');
  list.resolve({items:[modelResult()],total:1}); await pending; assert.equal(h.run('modelQueueRows.length'),0);
});
await check('AI approval needs permission and reason and explicitly remains not deployed', async () => {
  const h = modelHarness(); await h.run('selectModelReview("M1")');
  await h.run('submitModelReview("approve")'); assert.equal(h.requests.length,1);
  assert.match(h.get('modelReviewStatus').textContent,/사유/);
  h.run('permissions=["model:read"]; renderModelReview();');
  assert.equal(h.get('modelApprove').disabled,true); h.get('modelReviewReason').value='reviewed';
  await h.run('submitModelReview("approve")'); assert.equal(h.requests.length,1);
  assert.match(h.get('modelReviewSummary').textContent,/not_deployed/);
});
await check('AI uncertain approval retry reuses its identity and never duplicates after success', async () => {
  const h = modelHarness(); await h.run('selectModelReview("M1")');
  h.get('modelReviewReason').value='evidence checked'; let first=true;
  h.context.respond = async () => {if (first) {first=false;throw new Error('lost response');} return {...modelResult(),status:'approved',approvalStatus:'approved',approvalRevision:1};};
  await h.run('submitModelReview("approve")'); assert.equal(h.get('modelReviewReason').value,'evidence checked');
  await h.run('submitModelReview("approve")');
  const bodies = h.requests.filter(row => row.options.method==='POST').map(row => JSON.parse(row.options.body));
  assert.equal(bodies.length,2); assert.deepEqual(bodies[0],bodies[1]);
  assert.equal(h.run('modelReviewRow.approvalStatus'),'approved'); assert.equal(h.get('modelReviewReason').value,'');
  assert.equal(h.get('modelApprove').disabled,true); assert.match(h.get('modelReviewStatus').textContent,/배포는 수행하지 않았/);
  await h.run('submitModelReview("approve")'); assert.equal(h.requests.length,3);
});
await check('AI write success is not reversed by a failed subsequent list refresh', async () => {
  const h = modelHarness(); await h.run('selectModelReview("M1")'); h.get('modelReviewReason').value='confirmed';
  h.context.respond = async () => ({...modelResult(),status:'rejected',approvalStatus:'rejected',approvalRevision:1});
  await h.run('submitModelReview("reject")'); assert.equal(h.run('pendingModelReview'),null);
  assert.match(h.get('modelReviewStatus').textContent,/반려가 기록/);
  h.context.respond = async () => {throw new Error('list failed');}; await h.run('loadModelQueue()');
  assert.equal(h.get('modelReviewReason').value,''); assert.equal(h.get('modelReject').disabled,true);
});
await check('AI stale review error keeps evidence and request for explicit reload', async () => {
  const h = modelHarness(); await h.run('selectModelReview("M1")'); h.get('modelReviewReason').value='confirmed';
  h.context.respond = async () => {throw new Error('Reload current revision');};
  await h.run('submitModelReview("reject")');
  assert.equal(h.run('modelReviewRow.approvalStatus'),'pending'); assert.equal(h.get('modelReviewReason').value,'confirmed');
  assert.match(h.get('modelReviewStatus').textContent,/Reload/);
});
await check('AI registration evidence and review history are inert text, not executable HTML', async () => {
  const h = modelHarness(); h.context.fixture.reviewHistory=[{reason:'<script>attack()</script>'}];
  await h.run('selectModelReview("M1")');
  assert.match(h.get('modelReviewEvidence').textContent,/<script>attack/); assert.equal(h.get('modelReviewEvidence').children.length,0);
});
await check('AI double-click is coalesced and scope changes are blocked during review', async () => {
  const h = modelHarness(), waiting=deferred(); await h.run('selectModelReview("M1")'); h.run('rememberSelection()');
  h.get('modelReviewReason').value='reviewed'; h.context.respond=()=>waiting.promise;
  const saving=h.run('submitModelReview("approve")'); await h.run('submitModelReview("approve")');
  assert.equal(h.requests.length,2); assert.equal(h.get('modelStatusFilter').disabled,true);
  h.get('assetSelect').value='A2'; assert.equal(h.run('allowSelectionChange()'),false); assert.equal(h.get('assetSelect').value,'A1');
  waiting.resolve({...modelResult(),status:'approved',approvalStatus:'approved',approvalRevision:1}); await saving;
});

await check('AI evidence and draft survive telemetry timers and workspace navigation', async () => {
  const h = modelHarness(); await h.run('selectModelReview("M1")');
  h.get('modelReviewReason').value='still inspecting evidence';
  h.run('currentView="models"; modelQueuePage=2');
  const calls=h.requests.length, generation=h.run('modelReviewGeneration');
  for (const interval of h.intervals) interval();
  h.run('setView("overview"); setView("models"); setView("models")');
  assert.equal(h.requests.length,calls); assert.equal(h.run('modelReviewGeneration'),generation);
  assert.equal(h.run('modelReviewRow.version'),'M1'); assert.equal(h.run('modelQueuePage'),2);
  assert.equal(h.get('modelReviewReason').value,'still inspecting evidence');
  assert.equal(h.get('modelReviewPanel').hidden,false);
});

await check('AI cancelled refresh, page, filter and selection preserve the unsaved reason', async () => {
  const h = modelHarness(); await h.run('selectModelReview("M1")');
  h.get('modelReviewReason').value='unsaved evidence'; h.context.confirm=()=>false;
  h.get('modelStatusFilter').value='approved'; const calls=h.requests.length;
  await h.run('loadModelQueue(true)'); await h.run('loadModelQueue(false,2)');
  await h.run('selectModelReview("M2")'); await h.run('selectModelReview("M1")');
  assert.equal(h.requests.length,calls); assert.equal(h.run('modelReviewRow.version'),'M1');
  assert.equal(h.get('modelReviewReason').value,'unsaved evidence');
  assert.equal(h.get('modelStatusFilter').value,'draft'); assert.equal(h.run('modelQueuePage'),1);
});

const opsDefaults = {measurementIntervalMs:3000,replayBatchSize:4,healthReportIntervalMs:30000};
const opsRow = id => ({id,siteId:'S1',assetId:'A1',mappingStatus:'active',certificateStatus:'registered',firmwareVersion:'test-only'});
function opsCommand(version=1, changes={}) {
  return {version,commandId:'a'.repeat(32),settings:{measurementIntervalMs:6000,replayBatchSize:2,healthReportIntervalMs:30000},state:'pending',result:null,siteId:'S1',assetId:'A1',reason:'fixture reason',requestedBy:'tester',requestedAt:'2026-09-06T00:00:00Z',...changes};
}
function opsConfigResult(id='D1', command=null, scope={}) {
  return {deviceId:id,siteId:'S1',assetId:'A1',version:command?.version || 0,desired:command,lastApplied:null,scopeChanged:false,defaults:opsDefaults,limits:{},...scope};
}
function opsQualityResult(id='D1', hasData=true) {
  const summary = {hasData,windowCount:hasData?1:0,attempts:0,failures:0,retries:0,acknowledged:0,replayAttempts:0,failureRatePct:null,ackLatencyMeanMs:null,ackLatencyMaxMs:null,bufferDepthSampleMean:hasData?0:null,bufferDepthLast:hasData?0:null,bufferDepthMax:hasData?0:null,bufferCapacity:hasData?100:null,bufferDropped:0,observedDurationMs:hasData?60000:0};
  return {deviceId:id,siteId:'S1',assetId:'A1',transport:'http',attribution:'whole_window_at_end',from:'2026-09-06T00:00:00.000Z',to:'2026-09-06T01:00:00.000Z',bucketSeconds:3600,summary,items:[{...summary,from:'2026-09-06T00:00:00.000Z',to:'2026-09-06T01:00:00.000Z',crossBoundaryWindows:0}]};
}
function opsHarness() {
  const h = harness(); h.get('opsQualityRange').value='24'; h.get('opsQualityBucket').value='3600';
  h.context.respond = async path => {
    const id = path.split('/')[3];
    if (path.endsWith('/devices')) return [opsRow('D1')];
    if (path.includes('/communication-quality')) return opsQualityResult(id);
    if (path.endsWith('/configuration/history')) return {items:[],retainedLimit:100};
    if (path.endsWith('/configuration')) return opsConfigResult(id);
    if (path.endsWith('/health')) return {deviceId:id,siteId:'S1',assetId:'A1',health:'online',rssiDbm:0,rebootCount:0,bufferUsagePct:0};
    throw new Error('Unexpected operations request: '+path);
  };
  return h;
}
function opsDraft(h) {
  h.get('opsInterval').value='6000'; h.get('opsReplay').value='2'; h.get('opsConfigReason').value='fixture reason'; h.run('opsDirty=true; opsControls()');
}
await check('operations lists only the selected asset devices and reuses existing APIs', async () => {
  const h=opsHarness(), respond=h.context.respond;
  h.context.respond=path=>path.endsWith('/devices') ? [opsRow('D1'),{...opsRow('FOREIGN'),assetId:'A2'},{...opsRow('OTHER-SITE'),siteId:'S2'}] : respond(path);
  await h.run('loadDeviceOperations()');
  assert.equal(h.run('opsDevices.length'),1); assert.equal(h.run('opsDevice.id'),'D1');
  assert.equal(h.requests.length,5); assert.ok(!h.requests.some(row=>row.options.method));
  assert.match(textOf(h.get('opsHealth')),/0 dBm/);
  assert.equal(h.get('opsConfigPublish').disabled,false);
  assert.match(h.get('opsConfigStatus').textContent,/기본값.*실제 적용값/);
});
await check('operations missing quality stays unknown while measured zero stays valid', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  assert.match(textOf(h.get('opsQualitySummary')),/0회/); assert.match(textOf(h.get('opsQualitySummary')),/ACK 가중 평균\s+미수신/);
  assert.match(textOf(h.get('opsQualitySummary')),/최신 버퍼 깊이 \/ 용량\s+0 \/ 100/);
  h.context.respond=()=>opsQualityResult('D1',false); await h.run('loadOpsQuality()');
  assert.match(h.get('opsQualityStatus').textContent,/정상 여부를 판단할 수 없/);
  assert.match(textOf(h.get('opsQualityRows')),/관측 없음/);
  assert.ok(!textOf(h.get('opsQualitySummary')).includes('실패율0%'));
});
await check('operations quality uses captured UTC range and rejects excessive buckets', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  const request=h.requests.find(row=>row.path.includes('communication-quality'));
  const query=new URL('http://localhost'+request.path).searchParams;
  assert.equal(new Date(query.get('to'))-new Date(query.get('from')),86400000); assert.equal(query.get('bucketSeconds'),'3600');
  h.get('opsQualityRange').value='720'; h.get('opsQualityBucket').value='60'; const calls=h.requests.length;
  await h.run('loadOpsQuality()'); assert.equal(h.requests.length,calls); assert.equal(h.run('opsQuality'),null);
  assert.match(h.get('opsQualityStatus').textContent,/1,000/); assert.equal(h.get('opsQualityNext').disabled,true);
});
await check('operations quality and history pages do not truncate the returned evidence', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  h.context.respond=path=>path.includes('communication-quality') ? {...opsQualityResult(),items:Array.from({length:25},(_,i)=>({...opsQualityResult().items[0],crossBoundaryWindows:i}))} : {items:Array.from({length:25},(_,i)=>opsCommand(25-i)),retainedLimit:100};
  await h.run('loadOpsQuality()'); await h.run('loadOpsHistory()');
  for(let i=0;i<2;i++){h.get('opsQualityNext').listeners.click(); h.get('opsHistoryNext').listeners.click();}
  assert.equal(tableRows(h.get('opsQualityRows').children[0]).length,1);
  assert.equal(tableRows(h.get('opsHistoryRows').children[0]).length,1);
  assert.match(h.get('opsHistoryPage').textContent,/3 \/ 3.*25/);
});
await check('operations failed requests clear old quality without disabling a valid config', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()'); const config=clone(h.run('opsConfig'));
  h.context.respond=()=>{throw new Error('offline');}; await h.run('loadOpsQuality()'); await h.run('loadOpsHistory()');
  assert.equal(h.run('opsQuality'),null); assert.equal(h.get('opsQualityRows').children.length,0);
  assert.equal(h.get('opsHistoryRows').children.length,0); assert.deepEqual(clone(h.run('opsConfig')),config);
  assert.equal(h.get('opsConfigPublish').disabled,false);
});
await check('operations late quality health and config responses cannot cross device selection', async () => {
  const h=opsHarness(), respond=h.context.respond, old=deferred();
  h.context.respond=path=>path.endsWith('/devices') ? [opsRow('D1'),opsRow('D2')] : path.includes('/D1/') ? old.promise : respond(path);
  const pending=h.run('loadDeviceOperations()'); await new Promise(resolve=>setImmediate(resolve));
  await h.run('selectOpsDevice("D2")'); old.resolve(opsConfigResult('D1')); await pending;
  assert.equal(h.run('opsDevice.id'),'D2'); assert.equal(h.run('opsConfig.deviceId'),'D2');
  assert.equal(h.run('opsQuality.deviceId'),'D2'); assert.match(textOf(h.get('opsIdentity')),/D2/);
});
await check('operations readonly inactive and unsupported mappings never issue configuration', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()'); opsDraft(h); const calls=h.requests.length;
  h.run('permissions=["device:read"]; opsControls()'); await h.run('publishOpsConfig()');
  assert.equal(h.requests.length,calls); assert.equal(h.get('opsConfigPublish').disabled,true);
  h.run('permissions=["*"]; opsDevice.mappingStatus="inactive"; opsControls()'); await h.run('publishOpsConfig()');
  assert.equal(h.requests.length,calls); h.run('opsDevice.mappingStatus="active"; opsDevice.id="not supported"; opsControls()');
  assert.equal(h.get('opsConfigPublish').disabled,true);
});
await check('operations publish validates bounds reason confirmation and keeps pending distinct', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()'); opsDraft(h); const calls=h.requests.length;
  h.get('opsReplay').value='2.5'; await h.run('publishOpsConfig()'); assert.equal(h.requests.length,calls);
  opsDraft(h); h.context.confirm=()=>false; await h.run('publishOpsConfig()'); assert.equal(h.requests.length,calls);
  h.context.confirm=()=>true; h.context.respond=async()=>opsCommand(); await h.run('publishOpsConfig()');
  const sent=JSON.parse(h.requests.at(-1).options.body);
  assert.deepEqual(sent,{expectedVersion:0,settings:{measurementIntervalMs:6000,replayBatchSize:2,healthReportIntervalMs:30000},reason:'fixture reason'});
  assert.equal(h.run('opsConfig.lastApplied'),null); assert.match(h.get('opsConfigStatus').textContent,/적용 성공 보고 대기/);
  assert.equal(h.get('opsConfigReason').value,''); assert.equal(h.run('opsDirty'),false);
});
await check('operations double click and lost publication ACK cannot create another command', async () => {
  const h=opsHarness(), reply=deferred(); await h.run('loadDeviceOperations()'); opsDraft(h);
  h.context.respond=()=>reply.promise; const saving=h.run('publishOpsConfig()'); await h.run('publishOpsConfig()');
  assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,1);
  assert.equal(h.get('opsDevice').disabled,true); reply.reject(new Error('ACK lost')); await saving;
  assert.equal(h.run('opsUncertain'),true); assert.equal(h.get('opsConfigPublish').disabled,true);
  await h.run('publishOpsConfig()'); assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,1);
  h.context.respond=()=>opsConfigResult('D1',opsCommand()); await h.run('loadOpsConfig()');
  assert.match(h.get('opsConfigStatus').textContent,/중복 발행하지 않았/); assert.equal(h.get('opsConfigReason').value,'');
  opsDraft(h); await h.run('publishOpsConfig()'); assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,1);
});
await check('operations config conflict preserves draft and explicit reload updates expected version', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()'); opsDraft(h);
  h.context.respond=()=>{throw new Error('version conflict');}; await h.run('publishOpsConfig()');
  assert.equal(h.get('opsConfigReason').value,'fixture reason');
  h.context.respond=()=>opsConfigResult('D1',opsCommand(2,{settings:{measurementIntervalMs:9000,replayBatchSize:3},reason:'another editor'}));
  await h.run('loadOpsConfig()'); assert.equal(h.run('opsConfig.version'),2); assert.equal(h.get('opsInterval').value,'6000');
  h.context.respond=()=>opsCommand(3); await h.run('publishOpsConfig()');
  assert.equal(JSON.parse(h.requests.at(-1).options.body).expectedVersion,2);
});
await check('operations published state survives a late history and following lookup failure', async () => {
  const h=opsHarness(), late=deferred(); await h.run('loadDeviceOperations()'); opsDraft(h);
  h.context.respond=()=>late.promise; const history=h.run('loadOpsHistory()');
  h.context.respond=()=>opsCommand(); await h.run('publishOpsConfig()');
  late.resolve({items:[],retainedLimit:100}); await history; assert.equal(h.run('opsHistory.length'),1);
  h.context.respond=()=>{throw new Error('list failed');}; await h.run('loadOpsHistory()');
  assert.equal(h.run('opsConfig.version'),1); assert.equal(h.get('opsConfigReason').value,'');
  assert.match(h.get('opsConfigStatus').textContent,/저장했습니다/);
});
await check('operations timers and read-only refreshes preserve an in-progress configuration', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()'); opsDraft(h);
  h.run('setView("deviceOps")'); assert.equal(h.get('telemetryPeriodField').hidden,true);
  h.run('setView("overview")'); assert.equal(h.get('telemetryPeriodField').hidden,false);
  h.run('setView("deviceOps")'); const calls=h.requests.length; for(const tick of h.intervals) tick();
  assert.equal(h.requests.length,calls); await h.run('loadOpsHealth()'); await h.run('loadOpsQuality()');
  assert.equal(h.get('opsConfigReason').value,'fixture reason'); assert.equal(h.get('opsInterval').value,'6000');
  h.context.confirm=()=>false; h.get('opsDevice').value='D2'; await h.run('selectOpsDevice("D2")');
  assert.equal(h.get('opsDevice').value,'D1'); assert.equal(h.get('opsConfigReason').value,'fixture reason');
});
await check('operations failed configuration lookup disables writes and keeps other data visible', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  h.context.respond=()=>{throw new Error('forbidden');}; await h.run('loadOpsConfig()');
  assert.equal(h.get('opsConfigPublish').disabled,true); assert.equal(h.run('opsConfig'),null);
  assert.equal(h.run('opsQuality.deviceId'),'D1');
});
await check('operations history and state render untrusted text without executing markup', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  h.context.respond=()=>({items:[opsCommand(1,{reason:'<img onerror=attack()>',requestedBy:'<script>attack()</script>'})],retainedLimit:100});
  await h.run('loadOpsHistory()'); const row=tableRows(h.get('opsHistoryRows').children[0])[0];
  assert.equal(row.children[4].textContent,'<img onerror=attack()>'); assert.equal(row.children[4].children.length,0);
});
await check('operations empty or forbidden device lists cannot retain another asset controls', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()'); h.context.respond=()=>[];
  await h.run('loadDeviceOperations()'); assert.equal(h.run('opsDevice'),null); assert.equal(h.run('opsConfig'),null);
  assert.equal(h.get('opsConfigPublish').disabled,true); assert.equal(h.get('opsDevice').disabled,true);
  h.run('permissions=[]; setView("deviceOps")'); assert.equal(h.run('currentView'),'overview');
});

await check('operations rejects desired and lastApplied with foreign or missing mapping before PUT', async () => {
  for (const field of ['desired','lastApplied']) for (const scope of [{assetId:'A2'},{siteId:'S2'},{assetId:null},{siteId:undefined}]) {
    const h=opsHarness(); await h.run('loadDeviceOperations()'); opsDraft(h);
    const c={...opsConfigResult(),version:2,[field]:opsCommand(2,scope)};
    h.context.respond=()=>c; await h.run('loadOpsConfig()');
    assert.equal(h.run('opsConfig'),null); assert.equal(h.run('opsDevice'),null);
    assert.equal(h.get('opsConfigPublish').disabled,true); assert.equal(h.get('opsDevice').disabled,true);
    assert.match(h.get('opsConfigStatus').textContent,/장치 목록.*다시 선택/);
    assert.equal(h.get('opsConfigReason').value,'fixture reason');
    await h.run('loadOpsConfig()'); await h.run('publishOpsConfig()');
    assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,0);
  }
});
await check('operations remap recovery requires a fresh device list and the new asset selection', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  h.context.respond=()=>opsConfigResult('D1',opsCommand(2,{assetId:'A2'})); await h.run('loadOpsConfig()');
  await h.run('selectOpsDevice("D1")'); assert.equal(h.run('opsDevice'),null);
  h.context.respond=()=>[ {...opsRow('D1'),assetId:'A2'} ]; await h.run('loadDeviceOperations()');
  assert.equal(h.run('opsDevice'),null); assert.equal(h.get('opsConfigPublish').disabled,true);
  h.get('assetSelect').value='A2';
  h.context.respond=path=>path.endsWith('/devices') ? [{...opsRow('D1'),assetId:'A2'}] : path.endsWith('/configuration') ? opsConfigResult('D1',opsCommand(2,{assetId:'A2'}),{assetId:'A2'}) : path.endsWith('/history') ? {items:[],retainedLimit:100} : path.endsWith('/health') ? {deviceId:'D1',siteId:'S1',assetId:'A2'} : {...opsQualityResult(),assetId:'A2'};
  await h.run('loadDeviceOperations()'); assert.equal(h.get('opsConfigPublish').disabled,false);
  opsDraft(h); h.get('opsInterval').value='12000';
  h.context.respond=()=>opsCommand(3,{assetId:'A2',settings:{measurementIntervalMs:12000,replayBatchSize:2}});
  await h.run('publishOpsConfig()'); assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,1);
  assert.equal(h.run('opsConfig.desired.assetId'),'A2');
});
await check('operations foreign health invalidates config even when an older config reply finishes later', async () => {
  for (const healthFirst of [true,false]) {
    const h=opsHarness(), pending=deferred(); await h.run('loadDeviceOperations()');
    h.context.respond=path=>path.endsWith('/configuration') ? pending.promise : {deviceId:'D1',siteId:'S1',assetId:'A2',health:'online'};
    const config=h.run('loadOpsConfig()');
    if (!healthFirst) {pending.resolve(opsConfigResult()); await config;}
    await h.run('loadOpsHealth()');
    if (healthFirst) {pending.resolve(opsConfigResult()); await config;}
    assert.equal(h.run('opsDevice'),null); assert.equal(h.run('opsConfig'),null);
    assert.equal(h.get('opsConfigPublish').disabled,true); await h.run('publishOpsConfig()');
    assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,0);
  }
});
await check('operations scopeChanged with no current commands still allows a correctly selected mapping', async () => {
  const h=opsHarness(), respond=h.context.respond;
  h.context.respond=path=>path.endsWith('/configuration') ? {...opsConfigResult(),version:2,scopeChanged:true} : respond(path);
  await h.run('loadDeviceOperations()'); assert.equal(h.get('opsConfigPublish').disabled,false);
  opsDraft(h); h.context.respond=()=>opsCommand(3); await h.run('publishOpsConfig()');
  assert.equal(JSON.parse(h.requests.at(-1).options.body).expectedVersion,2);
  assert.equal(h.run('opsConfig.desired.assetId'),'A1');
});
await check('operations foreign quality or history also prevents reuse of the previously valid config', async () => {
  for (const route of ['quality','history']) {
    const h=opsHarness(); await h.run('loadDeviceOperations()');
    h.context.respond=()=>route==='quality' ? {...opsQualityResult(),assetId:'A2'} : {items:[opsCommand(2,{siteId:'S2'})],retainedLimit:100};
    await h.run(route==='quality' ? 'loadOpsQuality()' : 'loadOpsHistory()');
    assert.equal(h.run('opsConfig'),null); assert.equal(h.get('opsConfigPublish').disabled,true);
    await h.run('publishOpsConfig()'); assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,0);
  }
});

await check('operations empty commands require authoritative current mapping regardless of scopeChanged', async () => {
  for (const scopeChanged of [true,false]) for (const scope of [{assetId:'A2'},{siteId:'S2'},{assetId:null},{siteId:undefined},{siteId:undefined,assetId:undefined}]) {
    const h=opsHarness(); await h.run('loadDeviceOperations()'); opsDraft(h);
    h.context.respond=()=>({...opsConfigResult('D1',null,scope),scopeChanged});
    await h.run('loadOpsConfig()');
    assert.equal(h.run('opsConfig'),null); assert.equal(h.run('opsDevice'),null);
    assert.equal(h.get('opsConfigPublish').disabled,true); assert.equal(h.get('opsInterval').disabled,true);
    assert.equal(h.get('opsConfigReason').value,'fixture reason');
    assert.match(h.get('opsConfigStatus').textContent,/장치 목록.*다시 선택/);
    await h.run('selectOpsDevice("D1")'); await h.run('publishOpsConfig()');
    assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,0);
  }
});
await check('operations publication stays disabled until empty configuration mapping is verified', async () => {
  for (const matches of [true,false]) {
    const h=opsHarness(), pending=deferred(); await h.run('loadDeviceOperations()'); opsDraft(h);
    h.context.respond=()=>pending.promise; const loading=h.run('loadOpsConfig()');
    assert.equal(h.get('opsConfigPublish').disabled,true); assert.equal(h.run('opsConfig'),null);
    await h.run('publishOpsConfig()'); assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,0);
    pending.resolve(opsConfigResult('D1',null,{assetId:matches?'A1':'A2'})); await loading;
    assert.equal(h.get('opsConfigPublish').disabled,!matches);
    assert.equal(h.get('opsConfigReason').value,'fixture reason');
  }
});

await check('installation snapshots and unknown history render safely without fetching photos', async () => {
  const h=harness(); h.context.respond=path=>{
    const result=detailResponses(path);
    if(path.includes('/anomaly/events/')) result.installationSnapshot={status:'recorded',effectiveAt:'2026-09-07T00:00:00Z',points:[{id:'IP1',position:'<img onerror=attack()>',orientation:'X',mountingMethod:'bolt',photoRefs:['https://example.invalid/private.jpg']}]};
    return result;
  };
  await h.run('selectEvent("E1")');
  assert.match(textOf(h.get('eventDetail')),/<img onerror=attack\(\)>/);
  assert.match(textOf(h.get('eventDetail')),/private.jpg/);
  assert.ok(!h.requests.some(r=>r.path.includes('example.invalid')));
  h.context.respond=detailResponses; await h.run('selectEvent("E2")');
  assert.match(textOf(h.get('eventDetail')),/기록된 과거 설치 정보 없음/);
});
await check('health interval UI rejects bounds and sends the selected integer', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()'); opsDraft(h);
  h.get('opsHealthInterval').value='9999'; await h.run('publishOpsConfig()');
  assert.ok(!h.requests.some(r=>r.options.method==='PUT'));
  const original=h.context.respond;
  h.context.respond=(path,options)=>options.method==='PUT'?{desired:opsCommand(1,{settings:JSON.parse(options.body).settings})}:original(path,options);
  h.get('opsHealthInterval').value='120000'; await h.run('publishOpsConfig()');
  assert.equal(JSON.parse(h.requests.find(r=>r.options.method==='PUT').options.body).settings.healthReportIntervalMs,120000);
});
const analysisList=(changes={})=>({deviceId:'D1',siteId:'S1',assetId:'A1',items:[],requests:[],nextCursor:null,...changes});
await check('waveform request ACK loss reuses identity and successful write survives list failure', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  h.get('opsAnalysisReason').value='inspect sound'; let lose=true;
  h.context.respond=(path,options)=>{
    if(path.endsWith('/requests')) {if(lose) {lose=false;throw new Error('ACK lost');} return {...JSON.parse(options.body),status:'pending'};}
    throw new Error('list failed');
  };
  await h.run('requestOpsWaveform()');
  const first=JSON.parse(h.requests.at(-1).options.body);
  assert.equal(h.get('opsAnalysisReason').value,'inspect sound');
  await h.run('requestOpsWaveform()');
  const posts=h.requests.filter(r=>r.options.method==='POST');
  assert.deepEqual(JSON.parse(posts[1].options.body),first);
  assert.equal(h.get('opsAnalysisReason').value,''); assert.equal(h.run('analysisIntent'),null);
  assert.match(h.get('opsAnalysisStatus').textContent,/요청 저장 완료.*목록 조회 실패/);
  await h.run('requestOpsWaveform()'); assert.equal(h.requests.filter(r=>r.options.method==='POST').length,2);
});
await check('waveform reads reject remapped scope even when the history is empty', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  h.context.respond=()=>analysisList({assetId:'A2'});
  await h.run('loadOpsAnalysis()');
  assert.equal(h.run('opsDevice'),null);
  assert.equal(h.get('opsAnalysisRequest').disabled,true);
});
await check('late waveform list cannot replace a newer request and pagination is reachable', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()'); const late=deferred(); let count=0;
  h.context.respond=()=>++count===1?late.promise:analysisList({requests:[{id:'new',status:'pending',reason:'latest'}],nextCursor:'opaque-next'});
  const old=h.run('loadOpsAnalysis()'); await h.run('loadOpsAnalysis()'); late.resolve(analysisList()); await old;
  assert.match(textOf(h.get('opsAnalysisRows')),/latest/);
  const next=h.get('opsAnalysisRows').children.at(-1);
  h.context.respond=path=>{assert.match(path,/cursor=opaque-next/);return analysisList();};
  await next.listeners.click(); await new Promise(resolve=>setTimeout(resolve,0));
  assert.ok(h.requests.some(r=>r.path.includes('cursor=opaque-next')));
});
await check('raw preview decodes float32 and summaries never expose a waveform button', async () => {
  const h=opsHarness(); await h.run('loadDeviceOperations()');
  const channel={sampleCount:4,sampleRateHz:800,unit:'g',samplesFloat32LE:Buffer.from(new Float32Array([0,1,-1,0]).buffer).toString('base64')};
  const row={id:'a'.repeat(32),deviceId:'D1',siteId:'S1',assetId:'A1',timestamp:'2026-09-07T00:00:00Z',hasWaveform:true,channels:{vibrationX:channel}};
  h.context.respond=path=>path.startsWith('/api/analysis/')?{frame:row}:analysisList({items:[row]});
  await h.run('loadOpsAnalysis()');
  const card=h.get('opsAnalysisRows').children[0];
  await card.children.find(c=>c.tagName==='button').listeners.click();
  assert.ok(h.calls.some(c=>c[0]==='stroke'));
  assert.match(textOf(card),/분석 JSON 저장/);
  h.context.respond=()=>analysisList({items:[{...row,hasWaveform:false}]}); await h.run('loadOpsAnalysis()');
  assert.ok(!h.get('opsAnalysisRows').children[0].children.some(c=>c.tagName==='button'));
});

assert.deepEqual(failures,[],`${failures.length} behavior checks failed`);
console.log(`AI2 dashboard: ${checks} behavior checks passed.`);

export {harness};
