// Execute the shipped inline dashboard against a deterministic DOM/API double.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

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
    constructor(tag = 'div') {this.tagName = tag;}
    addEventListener(name, callback) {this.listeners[name] = callback;}
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
    URL, URLSearchParams, setTimeout, console, setInterval:fn => intervals.push(fn), confirm:() => true, alert:message => {throw new Error(message);},
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
  assert.match(h.get('eventDetail').children.at(-1).textContent,/원본 신호 없음/);
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
  assert.match(h.get('deviceHealth').children[0].textContent,/RSSI 0 dBm.*재부팅 0회.*버퍼 0%.*오류 1건/);
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

assert.deepEqual(failures,[],`${failures.length} behavior checks failed`);
console.log(`AI2 dashboard: ${checks} behavior checks passed.`);
