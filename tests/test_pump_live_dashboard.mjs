import test from 'node:test';
import assert from 'node:assert/strict';
import {harness,source} from './dashboard_harness.mjs';
import {model,row,result} from './pump_live_fixture.mjs';
const text=e=>typeof e==='string'?e:String(e.textContent)+' '+e.children.map(text).join(' ');
const walk=e=>[e,...e.children.flatMap(walk)];
function card(reply=result()) {const h=harness();h.context.reply=reply;return {h,el:h.run('renderSnapshotCard(reply)')};}
function immediateRow(index,eventType='anomaly_active') {
  const r=row(index);
  Object.assign(r.window,{historySequence:null,periodicSlotEpoch:null});
  Object.assign(r.transmission,{eventType,state:eventType==='recovery'?'NORMAL':'ANOMALY_ACTIVE'});
  r.analysis.forecast={status:'not_applicable',reason:'FORECAST_REQUIRES_SCHEDULED_RECORD'};
  return r;
}
const forecastPanel=el=>walk(el).find(e=>e.className==='live-module forecast');
const forecastCells=el=>walk(el).find(e=>e.tagName==='table').children.find(e=>e.tagName==='tbody').children.map(r=>r.children[2].textContent);

test('latest periodic rejection survives subsequent nonperiodic reports and polling',()=>{
  for(const reason of ['FORECAST_INPUT_OUT_OF_DISTRIBUTION','FORECAST_OUTPUT_OUT_OF_RANGE','FORECAST_OUTPUT_INVALID']) {
    const success=row(24),blocked=row(25);
    Object.assign(blocked.analysis.forecast,{status:'unavailable',reason,features:null});
    blocked.analysis.forecast.guard.inputRobust=123.456;
    const {h,el}=card(result([success]));
    assert.ok(forecastCells(el).every(v=>v!=='—'));
    const records=[blocked,success];
    for(const eventType of [null,'anomaly_start','anomaly_active','recovery']) {
      if(eventType) records.unshift(immediateRow(25+records.length,eventType));
      h.context.reply=result(records);
      const updated=h.run('renderSnapshotCard(reply)'),panel=forecastPanel(updated);
      assert.ok(forecastCells(updated).every(v=>v==='—'));
      assert.ok(text(panel).includes(h.run(`snapshotReason('${reason}')`)));
      assert.match(text(panel),/123\.456/);
      assert.doesNotMatch(text(panel),/예측 완료|최근 정기 이력의 예측 · 현재 보고의 결과 아님/);
      assert.match(text(updated),/학습 기준 초과/);
    }
  }
});

test('latest periodic record is a barrier even when pending, invalid or malformed',()=>{
  for(const mutate of [
    r=>{r.analysis={status:'queued_inference'};},
    r=>{r.analysis={status:'waiting_model',reason:'MODEL_NOT_CONFIGURED'};},
    r=>{r.window.quality='invalid';r.window.reason='fifo_overrun';r.window.features=null;},
    r=>{r.analysis.forecast.features.ku_a_3=-10;},
    r=>{r.analysis.inference.model.preprocessingVersion='old-version';},
    r=>{r.window.profileId='old-profile';}
  ]) {
    const latestPeriodic=row(25);mutate(latestPeriodic);
    const {el}=card(result([immediateRow(26),latestPeriodic,row(24)]));
    assert.ok(forecastCells(el).every(v=>v==='—'));
    assert.doesNotMatch(text(forecastPanel(el)),/예측 완료|최근 정기 이력의 예측 · 현재 보고의 결과 아님/);
  }
});

test('only a newer successful periodic record can replace a periodic rejection',()=>{
  const blocked=row(25);
  Object.assign(blocked.analysis.forecast,{status:'unavailable',reason:'FORECAST_OUTPUT_OUT_OF_RANGE',features:null});
  const success=row(27);success.analysis.forecast.features.cf_a_1=7.12345;
  success.analysis.forecast.guard.inputRobust=2.345;
  // Receipt order must not override actual measurement order.
  blocked.receivedAt='2026-09-14T00:00:00Z';
  const {el}=card(result([blocked,immediateRow(28),row(24),success]));
  assert.equal(forecastCells(el)[0],'7.12345');
  assert.match(text(forecastPanel(el)),/최근 정기 이력의 예측 · 현재 보고의 결과 아님/);
  assert.match(text(forecastPanel(el)),/2\.345/);
  assert.doesNotMatch(text(forecastPanel(el)),/예측값이 허용 범위를 벗어남/);
});

test('scheduled event reports also retain their blocked forecast across later reports',()=>{
  const blocked=row(25);blocked.transmission.eventType='anomaly_start';
  Object.assign(blocked.analysis.forecast,{status:'unavailable',reason:'FORECAST_OUTPUT_OUT_OF_RANGE',features:null});
  const {el}=card(result([immediateRow(26),blocked,row(24)]));
  assert.ok(forecastCells(el).every(v=>v==='—'));
  assert.match(text(forecastPanel(el)),/예측값이 허용 범위를 벗어남/);
});

test('v3 rejects invalid forecast output and does not substitute an older success',()=>{
  for(const reason of ['FORECAST_INPUT_OUT_OF_DISTRIBUTION','FORECAST_OUTPUT_OUT_OF_RANGE']) {
    const current=row(25),older=row(24);
    Object.assign(current.analysis.forecast,{status:'unavailable',reason,features:null});
    const {el}=card(result([current,older])),content=text(el);
    assert.match(content,/예측 불가/);
    const state=walk(el).filter(e=>e.className==='live-state').map(text).join(' ');
    assert.doesNotMatch(state,/예측 완료|최근 정기 이력의 예측/);
    const tbody=walk(el).find(e=>e.tagName==='table').children.find(e=>e.tagName==='tbody');
    assert.ok(tbody.children.every(r=>r.children[2].textContent==='—'));
    assert.match(content,/학습 기준 초과/);
  }
  for(const mutate of [
    r=>r.analysis.forecast.features.cf_a_1=.9,
    r=>r.analysis.forecast.features.ku_a_3=-10,
    r=>delete r.analysis.forecast.guard,
    r=>r.analysis.forecast.guard.inputRobust=99,
    r=>r.analysis.forecast.guard.outputClipped=true]) {
    const r=row(25);mutate(r);const {h,el}=card(result([r,row(24)]));
    assert.equal(h.run('snapshotForecastValid(reply.items[0])'),false);
    assert.doesNotMatch(walk(el).filter(e=>e.className==='live-state').map(text).join(' '),/예측 완료|최근 정기 이력의 예측/);
  }
});

test('v2 historical metadata is still recognized without running the old model',()=>{
  const r=row();r.analysis.inference.model.forecastModel.modelId='pump-summary-experiment-v2';
  r.analysis.forecast.modelId='pump-summary-experiment-v2';delete r.analysis.forecast.guard;
  const reply=result([r]);reply.configuredModel=structuredClone(r.analysis.inference.model);
  const {h}=card(reply);assert.equal(h.run('snapshotForecastValid(reply.items[0])'),true);
});

test('live overview keeps historical models and statistics off the working surface',()=>{
  const h=harness(); h.run('setView("overview")');
  for(const id of ['snapshotPanel','healthPanel','notificationsPanel']) assert.equal(h.get(id).hidden,false);
  for(const id of ['rf66Panel','siteKpis','sitesPanel','modelPanel','modelReviewPanel']) assert.equal(h.get(id).hidden,true);
  assert.equal(h.get('telemetryPeriodField').hidden,true);
  assert.match(source,/정기 이력은 Unix 시각 기준 25초 간격이며 이상 상태에서도 유지/);
});

test('two models show source scope, exact features, ratio, separate 24/13 histories and target time',()=>{
  const {h,el}=card();const content=text(el);
  assert.equal(h.run('snapshotModelValid(reply.configuredModel,reply)'),true);
  assert.equal(h.run('snapshotForecastValid(reply.items[0])'),true);
  for(const phrase of ['센서 2 학습 기준','SENSOR-02','오류 판정 + 예측 연결 완료','24건 / 필요 24건','현재 포함 13건',
    '학습 기준 초과','1.12','예측 완료','사건·알림을 발생시키거나 해제하지 않습니다.']) assert.ok(content.includes(phrase),phrase);
  const table=walk(el).find(e=>e.tagName==='table');const body=table.children.find(e=>e.tagName==='tbody');
  assert.equal(body.children.length,9);for(const key of Object.keys(model.inputContract.features)) assert.ok(body.children[key]);
  assert.equal(h.requests.length,0);
});
test('configured model does not turn pre-binding records into unconfigured runtime',()=>{
  const r=row();r.analysis={status:'waiting_model',reason:'MODEL_NOT_CONFIGURED'};
  const {el}=card(result([r])),content=text(el);
  assert.match(content,/오류 판정 \+ 예측 연결 완료/);assert.match(content,/모델 배정 전 수신 기록/);
  const states=walk(el).filter(e=>e.className==='live-state').map(text).join(' ');
  assert.doesNotMatch(states,/예측 완료|학습 기준 초과/);
});
test('no data, incompatible input, invalid quality and unavailable history stay distinct',()=>{
  assert.match(text(card(result([])).el),/대상 센서 입력 대기/);
  const old=row();old.window.profileId='adxl345-ac-cf-sk-ku-v1';
  assert.match(text(card(result([old])).el),/입력 규격 불일치/);
  const invalid=row();Object.assign(invalid.window,{quality:'invalid',reason:'fifo_overrun',features:null});
  const invalidCard=card(result([invalid])).el;
  assert.match(text(invalidCard),/입력 품질 불량/);
  const tbody=walk(invalidCard).find(e=>e.tagName==='table').children.find(e=>e.tagName==='tbody');
  assert.ok(tbody.children.every(r=>r.children[1].textContent==='—'&&r.children[2].textContent==='—'));
  const insufficient=row();Object.assign(insufficient.analysis,{status:'unavailable',reason:'VERIFIER_REQUIRES_24_PRIOR_RECORDS',evidence:{historicalRecordsUsed:12}});
  const content=text(card(result([insufficient])).el);
  assert.match(content,/12건 \/ 필요 24건/);assert.match(content,/예측 완료/);assert.match(content,/과거 이력 24건 확보 필요/);
});
test('current immediate report can retain an explicitly dated prior periodic forecast',()=>{
  const older=row(24),immediate=immediateRow(25,'anomaly_start');
  Object.assign(immediate.transmission,{eventType:'anomaly_start',state:'ANOMALY_ACTIVE',anomalyCount:3});
  immediate.analysis.forecast={status:'not_applicable',reason:'FORECAST_REQUIRES_SCHEDULED_RECORD'};
  assert.match(text(card(result([immediate,older])).el),/최근 정기 이력의 예측 · 현재 보고의 결과 아님/);
  for(const kind of ['boot','model']) {
    const changed=structuredClone(older);
    if(kind==='boot') changed.window.bootId='old-boot';else changed.analysis.inference.model.preprocessingVersion='old-version';
    const el=card(result([immediate,changed])).el;
    const state=walk(el).filter(e=>e.className==='live-state').map(text).join(' ');
    assert.doesNotMatch(state,/최근 정기 이력의 예측/);
    assert.match(state,kind==='model' ? /이전 모델 설정의 기록/ : /예측 대기·불가/);
  }
});
test('measurement freshness cannot be concealed by a recent receipt or a completed forecast',()=>{
  const r=row();r.receivedAt='2026-09-13T15:00:00Z';const response=result([r]);response.queriedAt='2026-09-13T15:00:02Z';
  const content=text(card(response).el);
  assert.match(content,/최신 측정 지연/);assert.match(content,/마지막 도착: 2초 전/);assert.match(content,/이미 지난 시각/);
});
test('other sensors are not substituted for configured model sensor',()=>{
  const r=row();r.window.sensorId='SENSOR-03';const el=card(result([r])).el;
  assert.match(text(el),/다른 센서의 수신 기록은 있으나/);
  assert.ok(walk(el).filter(e=>e.className==='live-state').every(e=>/입력 대기/.test(e.textContent)));
});
test('history details remain expanded through polling but are scoped by device',()=>{
  const {h,el}=card();const details=walk(el).find(e=>e.tagName==='details');details.open=true;details.listeners.toggle();
  const updated=h.run('renderSnapshotCard(reply)');assert.equal(walk(updated).find(e=>e.tagName==='details').open,true);
  h.context.reply.deviceId='OTHER';const other=h.run('renderSnapshotCard(reply)');assert.equal(walk(other).find(e=>e.tagName==='details').open,false);
});
test('history-model event summary cannot call a cleared reference test confirmed normal',()=>{
  const h=harness();h.context.event={source:'snapshot',snapshotObservation:'recovered',status:'closed',snapshotScore:1.2,
    modelVersion:model.modelVersion,snapshotEvidence:{inference:{model}}};
  assert.match(h.run('eventModelSummary(event)'),/학습 기준 미초과로 사건 해제/);
  assert.doesNotMatch(h.run('eventModelSummary(event)'),/정상 판정으로 복귀/);
});
