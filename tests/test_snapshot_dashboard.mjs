import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {harness, source} from './dashboard_harness.mjs';

const text = e => typeof e === 'string' ? e : e.textContent + ' ' + e.children.map(text).join(' ');
const scope = {deviceId:'D1',siteId:'S1',assetId:'A1'};
const measured = '2026-09-12T00:00:00Z';
const at = seconds => new Date(Date.parse(measured) + seconds * 1000).toISOString();
const metadata = () => ({contractId:'single-snapshot-anomaly-v1',modelId:'test-only',modelVersion:'v1',
  preprocessingVersion:'p1',scoreType:'test_error',threshold:.5,comparison:'>',scope:{...scope},
  inputContract:{adapterId:'adxl345-xyz-g-unmodified-v1',sourceProfileId:'adxl345-800hz-xyz-counts-v1',
    shape:[512,3],axes:['X','Y','Z'],unit:'g',sampleRateHz:800,gPerCount:.0039,meanRemoved:false}});
const transmission = reason => {
  const rule = {normal_periodic:['NORMAL','periodic',300,0,0],anomaly_enter:['ANOMALY_ACTIVE','immediate',0,3,0],
    anomaly_periodic:['ANOMALY_ACTIVE','periodic',10,4,0],normal_recovered:['NORMAL','immediate',0,0,5]}[reason];
  return {policyId:'edge-state-snapshot-v1',reason,state:rule[0],mode:rule[1],intervalSec:rule[2],anomalyCount:rule[3],normalCount:rule[4]};
};
const item = (analysis={}) => ({window:{...scope,bootId:'boot-one',windowIndex:100,sampleCount:512,
    timestamp:measured,quality:'valid'},transmission:transmission('normal_periodic'),
  receivedAt:at(2),digest:'a'.repeat(64),lateArrival:false,inferenceJob:null,
  analysis:{status:'waiting_model',reason:'MODEL_NOT_CONFIGURED',score:null,threshold:null,verdict:null,
    inputPreparation:{status:'ready'},affectsAlerts:false,...analysis}});
const completed = (score=0,model=metadata()) => {
  const verdict = model.comparison === '>' ? score > model.threshold : model.comparison === '>=' ? score >= model.threshold
    : model.comparison === '<' ? score < model.threshold : score <= model.threshold;
  return item({status:'completed',reason:null,score,verdict,threshold:model.threshold,comparison:model.comparison,
    scoreType:model.scoreType,modelVersion:model.modelVersion,
    inference:{model,inputDigest:'a'.repeat(64),completedAt:at(3)}});
};
const result = (items=[],configuredModel=null) => ({...scope,queriedAt:at(4),configuredModel,items,affectsAlerts:false});
function fixture() {
  const h = harness(); h.context.query={siteId:'S1',assetId:'A1'};
  h.context.devices=[{id:'D1',siteId:'S1',assetId:'A1'}];
  h.context.reply=()=>result();
  h.context.respond=path => path === '/api/sites/S1/devices' ? h.context.devices : h.context.reply(path);
  return h;
}
const load = h => h.run('loadSnapshots(query)');
const value = h => text(h.get('snapshotRows'));
const defer = () => {let resolve,reject; const promise=new Promise((yes,no)=>{resolve=yes;reject=no;}); return {promise,resolve,reject};};
const settle = () => new Promise(resolve=>setImmediate(resolve));

function verifierMetadata() {
  return {...metadata(),contractId:'history-event-verifier-v1',modelId:'pump-event-verifier-v1',
    scoreType:'novelty_reference_ratio',threshold:1,scope:{...scope,sensorId:'SENSOR-02'},
    inputContract:{adapterId:'pump-nine-source-features-v1',sourceProfileId:'pump-cf-sk-ku-summary-v1',
      shape:[9],features:['cf_a_1','cf_a_2','cf_a_3','sk_a_1','sk_a_2','sk_a_3','ku_a_1','ku_a_2','ku_a_3'],
      unit:'source_feature_values',historyRows:24,historyIntervalSec:25,currentEventMaxDelaySec:50}};
}
function verifierRow(score=0) {
  const m=verifierMetadata(),row=completed(score,m);
  Object.assign(row.window,{sensorId:'SENSOR-02',profileId:m.inputContract.sourceProfileId,
    features:Object.fromEntries(m.inputContract.features.map(k=>[k,0]))});
  row.analysis.evidence={historicalRecordsUsed:24};
  row.transmission={...transmission('normal_periodic'),policyId:'pump-verifier-history-v1',reason:'history_periodic',intervalSec:25};
  return row;
}
test('verifier ratio and baseline outcome never claim fault probability or confirmed normal',async()=>{
  for (const score of [0,1,1.01,40]) {
    const h=fixture(),row=verifierRow(score);
    h.context.reply=()=>({...result([row],verifierMetadata()),eventPolicy:{mode:'events'}});await load(h);
    assert.match(value(h),score>1 ? /이상 후보 · 학습 기준 초과/ : /학습 기준 미초과 · 정상 확정 아님/);
    assert.ok(value(h).includes(`${score} / > 1 (novelty_reference_ratio)`));
    assert.match(value(h),/25초 정기 이력/);assert.match(value(h),/24건 \/ 필요 24건/);
    assert.match(value(h),/SENSOR-02/);assert.match(value(h),/고장 확률 아님/);
    assert.doesNotMatch(value(h),/모델 정상 후보|서버 이상 1건으로 발생/);
  }
});
test('verifier incomplete history and sensor mismatch never produce a prediction',async()=>{
  const h=fixture(),row=verifierRow(2);
  row.window.sensorId='SENSOR-03';h.context.reply=()=>result([row],verifierMetadata());await load(h);
  assert.match(value(h),/결과 형식 확인 필요/);assert.doesNotMatch(value(h),/이상 후보 · 학습 기준 초과/);
  row.window.sensorId='SENSOR-02';row.analysis.status='unavailable';row.analysis.reason='VERIFIER_REQUIRES_24_PRIOR_RECORDS';
  row.analysis.evidence.historicalRecordsUsed=3;await load(h);
  assert.match(value(h),/25초 과거 이력 24건 확보 필요/);assert.match(value(h),/3건 \/ 필요 24건/);
});
test('verifier freshness uses 25 seconds and recovery notification is not a normal guarantee',async()=>{
  const h=fixture(),row=verifierRow(0);
  h.context.reply=()=>({...result([row],verifierMetadata()),queriedAt:at(86)});await load(h);
  assert.match(value(h),/최신 측정 지연.*예상 25초/);
  h.context.event={source:'snapshot',snapshotTransition:'closed',title:'이력 검증',
    snapshotEvidence:{inference:{model:verifierMetadata()}}};
  h.run('renderNotifications([{event,deliveredAt:"2026-09-12T00:00:00Z"}])');
  assert.match(text(h.get('notifications')),/이력 모델 기준 미초과 · 사건 해제/);
  assert.doesNotMatch(text(h.get('notifications')),/정상 복귀/);
});

test('overview has an independent new panel and preserves historical RF66',()=>{
  assert.match(source,/id="snapshotPanel"/); assert.match(source,/id="rf66Panel"/);
  assert.ok(!source.includes('.innerHTML'));
  const h=fixture();h.run('setView("overview")');assert.equal(h.get('snapshotPanel').hidden,false);
  h.run('setView("events")');assert.equal(h.get('snapshotPanel').hidden,true);
  assert.match(source,/새 단건 결과는 기존 통계 점수·RF66 과거 이력과 별개/);
});

function featureMetadata() {
  return {...metadata(),contractId:'edge-feature-event-v1',scope:{...scope,sensorId:'SENSOR-02'},
    inputContract:{adapterId:'adxl345-ac-nine-moments-v1',sourceProfileId:'adxl345-ac-cf-sk-ku-v1',
      shape:[9],features:verifierMetadata().inputContract.features,unit:'dimensionless',axes:['X','Y','Z'],
      sampleRateHz:800,sampleCount:512,sourceUnit:'g',meanRemoved:true,momentConvention:'population-pearson'}};
}
function featureRow(eventType='periodic') {
  const row=item(),m=featureMetadata();
  Object.assign(row.window,{schemaVersion:2,sensorId:'SENSOR-02',profileId:m.inputContract.sourceProfileId,
    reason:null,features:Object.fromEntries(m.inputContract.features.map(k=>[k,2]))});
  const [state,a,n]={periodic:['NORMAL',0,0],anomaly_start:['ANOMALY_ACTIVE',3,0],
    anomaly_active:['ANOMALY_ACTIVE',4,0],recovery:['NORMAL',0,5]}[eventType];
  row.transmission={policyId:'edge-feature-snapshot-v1',eventType,state,anomalyCount:a,normalCount:n};
  return row;
}
test('feature-only schedule and board events render without raw or a fabricated prediction',async()=>{
  for(const [event,label] of [['periodic','UTC 00·25·50초'],['anomaly_start','이상 진입 · 즉시 전송'],
    ['anomaly_active','이상 유지 · 10초 전송 / 정기 중단'],['recovery','정상 복귀 · 즉시 전송']]) {
    const h=fixture();h.context.reply=()=>result([featureRow(event)]);await load(h);
    assert.ok(value(h).includes(label));assert.match(value(h),/모델 대기/);
    assert.doesNotMatch(value(h),/전송 정책 확인 필요|모델 이상 후보|모델 정상 후보|warming_up/);
  }
  assert.match(source,/25·25·10초 간격/);
});
test('incompatible history model and invalid feature records have explicit non-normal reasons',async()=>{
  const h=fixture(),row=featureRow();
  row.analysis.status='unavailable';row.analysis.reason='MODEL_INPUT_CONTRACT_MISMATCH';
  h.context.reply=()=>({...result([row],verifierMetadata()),
    modelCompatibility:{status:'input_contract_mismatch',reason:'MODEL_INPUT_CONTRACT_MISMATCH'}});
  await load(h);assert.match(value(h),/입력 규격 불일치/);assert.match(value(h),/판정 불가/);
  assert.doesNotMatch(value(h),/이상 후보 · 학습 기준 초과|모델 정상 후보/);
  row.window.quality='invalid';row.window.reason='timeout';row.window.features=null;row.analysis.reason='timeout';
  await load(h);assert.match(value(h),/센서 수집 시간 초과/);
});
test('compatible future feature adapter requires sensor scope and finite measured features',async()=>{
  const h=fixture(),row=featureRow('anomaly_start'),m=featureMetadata();
  row.analysis=completed(.8,m).analysis;h.context.reply=()=>result([row],m);await load(h);
  assert.match(value(h),/모델 이상 후보/);assert.doesNotMatch(value(h),/결과 형식 확인 필요/);
  row.window.sensorId='SENSOR-03';await load(h);
  assert.match(value(h),/결과 형식 확인 필요/);assert.doesNotMatch(value(h),/모델 이상 후보/);
  row.window.sensorId='SENSOR-02';row.window.features.cf_a_1=null;await load(h);
  assert.match(value(h),/결과 형식 확인 필요/);
});
test('snapshot event modes and processing are separate from immutable model results',async()=>{
  for (const [mode,label] of [['shadow','비교만'],['events','알림 꺼짐'],['alerts','알림 허용']]) {
    const h=fixture(),row=completed(1);row.eventProcessing={status:'processed',reason:null};
    h.context.reply=()=>({...result([row],metadata()),eventPolicy:{mode},affectsAlerts:mode==='alerts'});
    await load(h);
    assert.ok(value(h).includes(label));assert.match(value(h),/모델 이상 후보/);
    assert.match(value(h),/사건 발생 여부는 이벤트 검수에서 확인/);
    assert.match(value(h),/서버 이상 1건으로 발생/);
    assert.ok(h.requests.every(r=>!r.options.method || r.options.method==='GET'));
  }
});
test('snapshot events and notifications distinguish open, recovery and unknown',()=>{
  const h=fixture();
  h.context.event={id:'SNAPSHOT-TEST',title:'단건 모델 이상 후보',source:'snapshot',occurredAt:measured,
    snapshotScore:.8,score:null,modelVersion:'model-v2',snapshotObservation:'unknown',status:'open',severity:'critical',label:'needs_review'};
  h.run('events=[event];renderEvents()');
  assert.match(text(h.get('events')),/발생 점수 0.8/);assert.match(text(h.get('events')),/관측 불명/);
  assert.doesNotMatch(text(h.get('events')),/null|RF66/);
  h.run('renderNotifications([{event:{...event,snapshotTransition:"open"},deliveredAt:event.occurredAt},{event:{...event,snapshotTransition:"closed"},deliveredAt:event.occurredAt}])');
  assert.match(text(h.get('notifications')),/단건 모델 이상 발생/);
  assert.match(text(h.get('notifications')),/단건 모델 정상 복귀/);
});
test('snapshot manual closure uses its own authorized endpoint and never RF66',async()=>{
  const h=fixture();h.context.event={id:'SNAPSHOT-TEST',source:'snapshot',status:'open'};
  const controls=h.run('rf66ResolutionControls(event,()=>true)'),[reason,button,status]=controls.children;
  await button.listeners.click();assert.equal(h.requests.length,0);
  reason.value='모델 변경으로 수동 점검 종료';h.context.respond=()=>({status:'closed'});
  await button.listeners.click();await button.listeners.click();
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].path,'/api/events/SNAPSHOT-TEST/snapshot-resolve');
  assert.equal(h.requests[0].options.method,'POST');assert.match(status.textContent,/저장 완료/);
  h.run('permissions=[]');assert.equal(h.run('rf66ResolutionControls(event,()=>true)').children.length,0);
  h.run('permissions=["*"]');const stale=h.run('rf66ResolutionControls(event,()=>false)');
  stale.children[0].value='점검';await stale.children[1].listeners.click();assert.equal(h.requests.length,1);
});
test('empty input and no model are distinct, never a normal verdict',async()=>{
  const h=fixture();await load(h);
  assert.match(value(h),/교체 모델 미설정/);assert.match(value(h),/새 단건 입력 대기/);
  assert.doesNotMatch(value(h),/모델 정상 후보|모델 이상 후보/);
  h.context.reply=()=>result([item()]);await load(h);
  assert.match(value(h),/모델 대기 · 이 구간에 모델 미배정/);
  assert.match(value(h),/완료된 모델 판정 없음/);
  assert.doesNotMatch(value(h),/새 단건 입력 대기/);
  assert.equal(h.requests[1].path,'/api/devices/D1/periodic-snapshots');
  assert.equal(h.requests[1].options.headers.authorization,'Bearer test-token');
  assert.ok(h.requests.every(r=>!r.options.method || r.options.method==='GET'));
});
test('all four report reasons are independent of the server verdict',async()=>{
  for (const reason of ['normal_periodic','anomaly_enter','anomaly_periodic','normal_recovered']) {
    for (const score of [0,1]) {
      const h=fixture(),row=completed(score);row.transmission=transmission(reason);
      h.context.reply=()=>result([row],metadata());await load(h);
      assert.ok(value(h).includes(score ? '모델 이상 후보' : '모델 정상 후보'));
      assert.ok(value(h).includes(reason.startsWith('anomaly') ? 'ANOMALY_ACTIVE' : 'NORMAL'));
      assert.match(value(h),/보드 보고/);assert.doesNotMatch(value(h),/3구간 이상 확인|고장 확률/);
    }
  }
});
test('finite unbounded scores, zero, equality and all comparison directions are preserved',async()=>{
  for (const comparison of ['>','>=','<','<=']) for (const score of [-2,0,.5,3]) {
    const h=fixture(),m={...metadata(),comparison},row=completed(score,m);
    h.context.reply=()=>result([row],m);await load(h);
    assert.ok(value(h).includes(`${score} / ${comparison} 0.5 (test_error)`));
    assert.ok(value(h).includes(row.analysis.verdict ? '모델 이상 후보' : '모델 정상 후보'));
  }
});
test('queue, unavailable, invalid or unknown results cannot become normal',async()=>{
  for (const patch of [{status:'queued'},{status:'waiting_model'},{status:'unavailable'},{status:'other'},
    {score:null},{score:'0'},{score:true},{score:NaN},{score:Infinity},{threshold:null},{threshold:'0.5'},
    {verdict:null},{verdict:1},{verdict:true},{modelVersion:'different'},{scoreType:'different'},
    {comparison:'>='},{reason:'unexpected'},{affectsAlerts:true},{inference:null}]) {
    const h=fixture(),row=completed();Object.assign(row.analysis,patch);
    h.context.reply=()=>result([row],metadata());await load(h);
    assert.doesNotMatch(value(h),/모델 정상 후보|모델 이상 후보/,JSON.stringify(patch));
  }
  for (const patch of [{quality:'fifo_overrun'},{sampleCount:15}]) {
    const h=fixture(),row=completed();Object.assign(row.window,patch);
    h.context.reply=()=>result([row],metadata());await load(h);assert.match(value(h),/판정 불가/);
  }
});
test('queued inference distinguishes running, waiting and unavailable adapter binding',async()=>{
  for (const [job,label] of [[{status:'queued',runtimeAvailable:true},'모델 추론 대기'],
    [{status:'running',runtimeAvailable:true},'모델 추론 중'],
    [{status:'running',runtimeAvailable:false},'해당 모델 연결 중지']]) {
    const h=fixture(),row=item({status:'queued_inference',reason:null});row.inferenceJob=job;
    h.context.reply=()=>result([row],metadata());await load(h);assert.ok(value(h).includes(label));
    assert.doesNotMatch(value(h),/모델 정상 후보|모델 이상 후보/);
  }
});
test('freshness uses server clock and measured time with the operational grace, not receipt',async()=>{
  for (const [reason,boundary] of [['normal_periodic',360],['normal_recovered',360],['anomaly_enter',70],['anomaly_periodic',70]]) {
    const h=fixture(),row=item();row.transmission=transmission(reason);row.receivedAt=at(1000);
    h.context.reply=()=>({...result([row]),queriedAt:at(boundary)});await load(h);assert.match(value(h),/예상 보고 간격 내/);
    h.context.reply=()=>({...result([row]),queriedAt:at(boundary+1)});await load(h);assert.match(value(h),/최신 측정 지연/);
    assert.match(value(h),/온라인\/오프라인 판정이 아닙니다/);
  }
  const h=fixture();h.context.reply=()=>({...result([item()]),queriedAt:at(-6)});await load(h);assert.match(value(h),/미래/);
  h.context.reply=()=>({...result([item()]),queriedAt:null});await load(h);assert.match(value(h),/확인 불가/);
});
test('older protocol never fabricates board NORMAL and unknown policy has no freshness guarantee',async()=>{
  const h=fixture(),row=item();row.transmission={policyId:'periodic-single-v1',mode:'periodic',intervalSec:300};
  h.context.reply=()=>result([row]);await load(h);assert.match(value(h),/미보고 \(이전 5분 정책\)/);
  assert.doesNotMatch(value(h),/보드 정상 상태/);
  for (const patch of [{policyId:'unknown'},{normalCount:3,anomalyCount:3},{state:'ANOMALY_ACTIVE'},{intervalSec:0}]) {
    row.transmission={...transmission('normal_periodic'),...patch};await load(h);
    assert.match(value(h),/전송 정책 확인 필요/);assert.doesNotMatch(value(h),/예상 보고 간격 내/);
  }
});
test('past model results are labelled even if only preprocessing or threshold changed',async()=>{
  for (const model of [null,{...metadata(),modelVersion:'v2'},{...metadata(),preprocessingVersion:'p2'},
    {...metadata(),threshold:.6}]) {
    const h=fixture();h.context.reply=()=>result([completed()],model);await load(h);
    assert.match(value(h),/이전 모델 설정의 결과/);assert.match(value(h),/test-only · v1/);
  }
});
test('latest waiting input cannot be disguised by an older completed or late-arriving result',async()=>{
  const h=fixture(),old=completed(1),latest=item();old.receivedAt=at(100);old.lateArrival=true;
  latest.window.timestamp=at(50);latest.window.windowIndex=200;
  h.context.reply=()=>result([old,latest],metadata());await load(h);
  const card=h.get('snapshotRows').children[0],current=card.children.find(child=>text(child).includes('최근 구간 서버 처리'));
  assert.match(text(current),/모델 대기/);
  assert.doesNotMatch(text(current),/모델 이상 후보/);assert.match(value(h),/늦은 도착/);
  assert.match(value(h),/대상 측정/);assert.match(value(h),/현재 상태 보장 아님/);
});
test('device and telemetry permissions are required, model-read is not spuriously required',async()=>{
  for (const permissions of [[],['device:read'],['telemetry:read']]) {
    const h=fixture();h.context.perms=permissions;h.run('permissions=perms');await load(h);
    assert.equal(h.requests.length,0);assert.match(h.get('snapshotStatus').textContent,/권한/);
  }
  const h=fixture();h.run('permissions=["device:read","telemetry:read"]');await load(h);assert.equal(h.requests.length,2);
});
test('no selection, no matching devices and non-overview views never issue window queries',async()=>{
  const h=fixture();await h.run('loadSnapshots(null)');assert.equal(h.requests.length,0);
  h.context.devices=[{id:'D2',siteId:'S2',assetId:'A2'}];await load(h);assert.equal(h.requests.length,1);
  h.run('setView("events")');await load(h);assert.equal(h.requests.length,1);
});
test('foreign response identity and embedded model scopes are rejected',async()=>{
  for (const level of ['outer','window']) for (const key of Object.keys(scope)) {
    const h=fixture(),r=result([completed()],metadata());(level==='outer'?r:r.items[0].window)[key]='OTHER';
    h.context.reply=()=>r;await load(h);assert.match(value(h),/조회 실패/);assert.doesNotMatch(value(h),/모델 정상 후보/);
  }
  for (const level of ['configured','stored']) {
    const h=fixture(),r=result([completed()],metadata());
    (level==='configured'?r.configuredModel:r.items[0].analysis.inference.model).scope.assetId='OTHER';
    h.context.reply=()=>r;await load(h);assert.doesNotMatch(value(h),/모델 정상 후보/);
  }
});
test('malformed metadata, identity digests and timestamps never produce predictions',async()=>{
  for (const mutate of [r=>r.configuredModel.inputContract.shape=[12,9],r=>r.configuredModel.threshold='0.5',
    r=>r.configuredModel.comparison='==',r=>r.configuredModel.modelId='<img>',r=>r.items[0].window.timestamp=null,
    r=>r.items[0].receivedAt=null,r=>delete r.configuredModel,r=>r.affectsAlerts=true]) {
    const h=fixture(),r=result([completed()],metadata());mutate(r);h.context.reply=()=>r;await load(h);
    assert.match(value(h),/조회 실패/);assert.doesNotMatch(value(h),/모델 정상 후보/);
  }
  const h=fixture(),row=completed();row.digest='b'.repeat(64);h.context.reply=()=>result([row]);await load(h);
  assert.match(value(h),/결과 형식 확인 필요/);
});
test('unsafe reasons are literal text and individual device failures preserve other cards',async()=>{
  const h=fixture();h.context.devices.push({id:'D2',siteId:'S1',assetId:'A1'});
  h.context.reply=path=>{if (path.includes('D2')) throw new Error('404 <img src=x>');
    return result([item({status:'unavailable',reason:'<img onerror=attack()>'})]);};
  await load(h);assert.match(value(h),/<img onerror=attack\(\)>/);assert.match(value(h),/D2 · 단건 조회 실패/);
  assert.match(h.get('snapshotStatus').textContent,/1개 장치 조회 실패/);
});
test('failed refresh removes previously successful results, not shown as empty input',async()=>{
  const h=fixture();h.context.reply=()=>result([completed()],metadata());await load(h);
  h.context.reply=()=>{throw new Error('401 expired');};await load(h);
  assert.match(value(h),/조회 실패/);assert.doesNotMatch(value(h),/모델 정상 후보|새 단건 입력 대기/);
  h.context.respond=()=>{throw new Error('503 device list');};await load(h);
  assert.equal(h.get('snapshotRows').children.length,0);assert.match(h.get('snapshotStatus').textContent,/조회 실패/);
});
test('slow polls are coalesced and eventually render instead of being starved',async()=>{
  const h=fixture(),d=defer();h.context.reply=()=>d.promise;
  const first=load(h);await settle();await load(h);assert.equal(h.requests.length,2);
  d.resolve(result([item()]));await first;assert.match(value(h),/모델 대기/);
});
test('hung reads time out, abort, and release the guard for a later retry',async()=>{
  const h=fixture(),timeouts=[];h.context.setTimeout=fn=>{timeouts.push(fn);return timeouts.length;};
  h.context.clearTimeout=()=>{};h.context.reply=()=>new Promise(()=>{});
  const pending=load(h);await settle();timeouts.at(-1)();await pending;
  assert.match(value(h),/조회 제한 시간 초과/);assert.equal(h.requests.at(-1).options.signal.aborted,true);
  h.context.reply=()=>result([item()]);await load(h);assert.match(value(h),/모델 대기/);
});
test('late success and failure cannot cross scope, session, permission or view changes',async()=>{
  for (const reject of [false,true]) for (const change of [
    '$("assetSelect").value="A2";invalidateScope()', '$("siteSelect").value="S2";invalidateScope()',
    'token="new-session";clearSnapshots()', 'permissions=[];clearSnapshots()', 'setView("events")']) {
    const h=fixture(),d=defer();h.context.reply=()=>d.promise;const pending=load(h);await settle();h.run(change);
    if (reject) d.reject(new Error('old failure'));else d.resolve(result([completed()]));
    await pending;assert.equal(h.get('snapshotRows').children.length,0);
  }
});
test('invalidated old requests cannot overwrite newer snapshots or release their busy guard',async()=>{
  const h=fixture(),a=defer(),b=defer();h.context.reply=()=>a.promise;
  const first=load(h);await settle();h.run('clearSnapshots()');h.context.reply=()=>b.promise;
  const second=load(h);await settle();a.resolve(result([completed()]));await first;
  await load(h);assert.equal(h.requests.length,4);
  b.resolve(result([item()]));await second;assert.match(value(h),/모델 대기/);assert.doesNotMatch(value(h),/모델 정상 후보/);
});
test('new panel refresh works when all legacy dashboard APIs fail',async()=>{
  const h=fixture();h.context.reply=path=>{if(path.endsWith('/periodic-snapshots')) return result([item()]); throw new Error('legacy unavailable');};
  await assert.rejects(h.run('render()'),/legacy unavailable/);await settle();assert.match(value(h),/모델 대기/);
  assert.equal(h.intervals.length,1);
  h.intervals[0]();await settle();assert.equal(h.requests.filter(r=>r.path.endsWith('/periodic-snapshots')).length,2);
});
test('entering overview triggers refresh immediately',async()=>{
  const h=fixture();h.run('setView("events");setView("overview")');await settle();
  assert.equal(h.requests.filter(r=>r.path.endsWith('/periodic-snapshots')).length,1);
});
test('up to twenty measured rows are ordered and rendered independently of the time filter',async()=>{
  const h=fixture(),rows=Array.from({length:22},(_,i)=>{const r=item();r.window.timestamp=at(i);r.window.windowIndex=i;return r;});
  h.context.reply=()=>result(rows);h.context.query.from='2030-01-01';await load(h);
  const card=h.get('snapshotRows').children[0],table=card.children.at(-1).children[0];
  const rendered=table.children.find(e=>e.tagName==='tbody').children;assert.equal(rendered.length,20);
  assert.match(text(rendered[0]),/#21/);assert.match(text(rendered.at(-1)),/#2/);
  assert.ok(!h.requests.at(-1).path.includes('?'));
});

test('real Python store response renders without translating its model or transmission contract',
  {skip:!process.argv.includes('--store-fixture')},async()=>{
    const payload=JSON.parse(readFileSync(0,'utf8')),h=fixture();
    h.context.query={siteId:payload.siteId,assetId:payload.assetId};
    h.get('siteSelect').value=payload.siteId;h.get('assetSelect').value=payload.assetId;
    h.context.respond=path=>path.endsWith('/devices') ? [{id:payload.deviceId,siteId:payload.siteId,assetId:payload.assetId}] : payload;
    await load(h);assert.match(value(h),/모델 이상 후보/);assert.match(value(h),/모델 대기/);
    assert.match(value(h),/판정 불가/);assert.match(value(h),/입력 준비 대기/);
    assert.doesNotMatch(value(h),/조회 실패|결과 형식 확인 필요/);
  });
