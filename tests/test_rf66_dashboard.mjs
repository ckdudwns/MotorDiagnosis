import test from 'node:test';
import assert from 'node:assert/strict';
import {harness, source} from './dashboard_harness.mjs';

const text = e => typeof e === 'string' ? e : e.textContent + e.children.map(text).join(' ');
const version = 'sha256:' + 'a'.repeat(64);
const model = {modelType:'random_forest',modelVersion:version,featureProfileId:'mcc5-vibration-800hz-spectral66-v1',threshold:.7,comparison:'>',mode:'shadow',affectsAlerts:false};
const item = (analysis={}) => ({window:{deviceId:'D1',siteId:'S1',assetId:'A1',timestamp:'2026-09-08T00:00:00Z',quality:'valid'},
  analysis:{...model,status:'completed',score:0,verdict:false,...analysis}});
const result = (items=[],configuredModel=model) => ({deviceId:'D1',siteId:'S1',assetId:'A1',configuredModel,items});
function fixture() {
  const h=harness();
  h.context.devices=[{deviceId:'D1',siteId:'S1',assetId:'A1'}];
  h.context.query={siteId:'S1',assetId:'A1'};
  h.context.respond=()=>result();
  return h;
}
const load=h=>h.run('loadRF66(devices,query)');

test('RF66 overview markup and source remain safe',()=>{
  assert.match(source,/id="rf66Panel"/);
  assert.ok(!source.includes('.innerHTML'));
  const h=fixture(); h.run('setView("overview")');
  assert.equal(h.get('rf66Panel').hidden,false);
  h.run('setView("events")');
  assert.equal(h.get('rf66Panel').hidden,true);
});
test('configured RF66 with no input is waiting, not normal',async()=>{
  const h=fixture(); await load(h);
  const value=text(h.get('rf66Rows'));
  assert.match(value,/원시 구간 입력 대기/);
  assert.match(value,/RF66 \(Random Forest\)/);
  assert.match(value,/완료된 판정 없음/);
  assert.doesNotMatch(value,/정상 후보/);
  assert.equal(h.requests[0].path,'/api/devices/D1/raw-vibration-windows');
  assert.equal(h.requests[0].options.headers.authorization,'Bearer test-token');
});
test('zero score and strict threshold retain exact values and verdicts',async()=>{
  const h=fixture();h.context.respond=()=>result([item(),item({score:.7}),item({score:.8,verdict:true})]);
  await load(h);
  const value=text(h.get('rf66Rows'));
  assert.match(value,/정상 후보/);assert.match(value,/이상 후보/);
  assert.match(value,/ 0 /);assert.match(value,/0.7/);
  assert.match(value,/고장 확률 아님/);
});
test('queued waiting unavailable unknown and malformed predictions never become normal',async()=>{
  for(const patch of [{status:'queued'},{status:'waiting_model'},{status:'unavailable'},{status:'unknown'},
    {score:null},{score:'0'},{score:true},{score:NaN},{score:Infinity},{score:-1},{score:1.1},
    {threshold:null},{verdict:null},{verdict:true,score:.7},{modelType:'dense_autoencoder'},
    {modelVersion:null},{affectsAlerts:true}]) {
    const h=fixture();h.context.respond=()=>result([item(patch)]);await load(h);
    const card=h.get('rf66Rows').children[0];
    assert.doesNotMatch(text(card.children[2])+text(card.children.at(-1)),/정상 후보|이상 후보/,JSON.stringify(patch));
  }
});
test('invalid input quality cannot produce a completed verdict',async()=>{
  const h=fixture(), row=item();row.window.quality='clipped';h.context.respond=()=>result([row]);
  await load(h); assert.match(text(h.get('rf66Rows')),/판정 불가/);
  assert.doesNotMatch(text(h.get('rf66Rows')),/정상 후보/);
});
test('off model preserves explicitly historical output',async()=>{
  const h=fixture();h.context.respond=()=>result([item()],null);await load(h);
  const value=text(h.get('rf66Rows'));
  assert.match(value,/모델 미설정/);assert.match(value,/과거 모델 결과/);assert.match(value, new RegExp(version));
});
test('replacement keeps result version and newest raw gap separate from last verdict',async()=>{
  const h=fixture(), old=item({modelVersion:'sha256:'+'b'.repeat(64)});
  const newest=item({status:'queued'});newest.window.timestamp='2026-09-08T00:01:00Z';
  h.context.respond=()=>result([old,newest]);await load(h);
  const value=text(h.get('rf66Rows'));
  assert.match(value,/처리 대기/);assert.match(value,/과거 모델 결과/);
  assert.match(value,/현재 실시간 수신을 보장하지 않습니다/);
});
test('all three permissions are required and empty device selection makes no request',async()=>{
  for(const permission of ['device:read','telemetry:read','model:read']) {
    const h=fixture();h.context.perms=['device:read','telemetry:read','model:read'].filter(p=>p!==permission);
    h.run('permissions=perms');await load(h);
    assert.equal(h.requests.length,0);assert.match(h.get('rf66Status').textContent,/권한/);
  }
  const h=fixture();h.context.devices=[{deviceId:'D2',siteId:'S2',assetId:'A2'}];await load(h);
  assert.equal(h.requests.length,0);
});
test('outer and inner foreign mappings are rejected without displaying scores',async()=>{
  for(const level of ['outer','inner']) for(const key of ['deviceId','siteId','assetId']) {
    const h=fixture(), r=result([item()]);(level==='outer'?r:r.items[0].window)[key]='OTHER';
    h.context.respond=()=>r;await load(h);
    assert.match(text(h.get('rf66Rows')),/조회 실패/);
    assert.doesNotMatch(text(h.get('rf66Rows')),/정상 후보/);
  }
});
test('late success or failure cannot replace newer results or cross a selection',async()=>{
  for(const reject of [false,true]) {
    const h=fixture();let resolve,fail;
    const pending=new Promise((yes,no)=>{resolve=yes;fail=no;});
    h.context.respond=()=>pending;const first=load(h);
    h.context.respond=()=>result();await load(h);
    const before=text(h.get('rf66Rows'));
    if(reject) fail(new Error('old failure'));else resolve(result([item()]));
    await first;assert.equal(text(h.get('rf66Rows')),before);
  }
  const h=fixture();let resolve;h.context.respond=()=>new Promise(yes=>resolve=yes);
  const pending=load(h);h.get('assetSelect').value='A2';h.run('invalidateScope()');
  resolve(result([item()]));await pending;assert.equal(h.get('rf66Rows').children.length,0);
});
test('session changes block in-flight responses',async()=>{
  const h=fixture();let resolve;h.context.respond=()=>new Promise(yes=>resolve=yes);
  const pending=load(h);h.run('token="new-session";clearRF66()');resolve(result([item()]));
  await pending;assert.equal(h.get('rf66Rows').children.length,0);
});
test('API errors clear old results and remain separate from input waiting',async()=>{
  const h=fixture();h.context.respond=()=>result([item()]);await load(h);
  h.context.respond=()=>{throw new Error('401 expired');};await load(h);
  const value=text(h.get('rf66Rows'));
  assert.match(value,/조회 실패/);assert.doesNotMatch(value,/정상 후보|원시 구간 입력 대기/);
});
test('unsafe reasons are rendered as text and malformed model metadata is rejected',async()=>{
  const h=fixture();h.context.respond=()=>result([item({status:'unavailable',reason:'<img onerror=attack()>'})]);
  await load(h);assert.match(text(h.get('rf66Rows')),/<img onerror=attack\(\)>/);
  h.context.respond=()=>result([],{...model,comparison:'>='});await load(h);
  assert.match(text(h.get('rf66Rows')),/설정 응답 확인/);
});
test('only five recent windows render while input times sort newest first',async()=>{
  const h=fixture(), items=Array.from({length:8},(_,i)=>{
    const row=item();row.window.timestamp='2026-09-08T00:0'+i+':00Z';return row;
  });
  h.context.respond=()=>result(items);await load(h);
  const card=h.get('rf66Rows').children[0], table=card.children.at(-1).children[0];
  assert.equal(table.children.find(e=>e.tagName==='tbody').children.length,5);
  assert.match(text(card),/최근 판정 대상 구간/);
});
