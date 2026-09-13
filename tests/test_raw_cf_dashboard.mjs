import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './dashboard_harness.mjs';
import {row,result} from './pump_live_fixture.mjs';

const text=e=>typeof e==='string'?e:e.textContent+' '+e.children.map(text).join(' ');
function replyV2() {
  const reply=result([row()]),m=reply.configuredModel;
  m.contractId='edge-feature-history-model-v2';
  m.inputMode='experimental-adxl25-raw-cf';
  m.preprocessingVersion='pump-dual-raw-cf-v1:freshwater_supply_motor2';
  Object.assign(m.inputContract,{
    adapterId:'adxl345-raw-cf-centered-moments-v2',sourceProfileId:'adxl345-raw-cf-centered-sk-ku-25s-v2',
    featureDefinitions:{cf:'max(abs(raw))/sqrt(mean(raw**2))',sk:'mean(centered**3)/mean(centered**2)**1.5',
      ku:'mean(centered**4)/mean(centered**2)**2',centered:'raw-mean(raw)'}});
  delete m.inputContract.meanRemoved;
  reply.items[0].window.profileId=m.inputContract.sourceProfileId;
  reply.items[0].analysis.inference.model=structuredClone(m);
  return reply;
}

test('raw-CF contract shows completed detector and forecast with truthful preprocessing',()=>{
  const h=harness();h.context.reply=replyV2();
  assert.equal(h.run('snapshotCompleted(reply.items[0])'),true);
  assert.equal(h.run('snapshotForecastValid(reply.items[0])'),true);
  const content=text(h.run('renderSnapshotCard(reply)'));
  assert.match(content,/CF: 평균 제거 전 · SK\/KU: 평균 제거 후/);
  assert.match(content,/adxl345-raw-cf-centered-sk-ku-25s-v2/);
  assert.match(content,/계산식 동일성 미확인/);
  assert.doesNotMatch(content,/결과 형식 확인 필요/);
});

test('raw-CF model cannot display legacy or inconsistent contracts as completed',()=>{
  for(const mutate of [
    r=>r.items[0].window.profileId='adxl345-ac-cf-sk-ku-25s-v1',
    r=>r.items[0].analysis.inference.model.inputMode='experimental-adxl25',
    r=>r.items[0].analysis.inference.model.inputContract.meanRemoved=true,
    r=>r.items[0].analysis.inference.model.inputContract.featureDefinitions.cf='centered',
    r=>r.items[0].analysis.inference.model.inputContract.featureDefinitions.ku='excess'
  ]) {
    const h=harness();h.context.reply=replyV2();mutate(h.context.reply);
    assert.equal(h.run('snapshotCompleted(reply.items[0])'),false);
    assert.equal(h.run('snapshotForecastValid(reply.items[0])'),false);
  }
});

test('raw-CF forecast rejection is not bypassed by later nonperiodic reports',()=>{
  const reply=replyV2(),blocked=structuredClone(reply.items[0]),extra=structuredClone(blocked);
  blocked.window.timestamp=new Date(Date.parse(blocked.window.timestamp)+25000).toISOString();
  Object.assign(blocked.analysis.forecast,{status:'unavailable',reason:'FORECAST_INPUT_OUT_OF_DISTRIBUTION',features:null});
  extra.window.timestamp=new Date(Date.parse(blocked.window.timestamp)+10000).toISOString();
  Object.assign(extra.window,{historySequence:null,periodicSlotEpoch:null});
  extra.transmission.eventType='anomaly_active';
  extra.analysis.forecast={status:'not_applicable',reason:'FORECAST_REQUIRES_SCHEDULED_RECORD'};
  reply.items.unshift(extra,blocked);
  const h=harness();h.context.reply=reply;
  const card=h.run('renderSnapshotCard(reply)');
  const walk=e=>[e,...e.children.flatMap(walk)];
  const content=text(walk(card).find(e=>e.className==='live-module forecast'));
  assert.match(content,/입력이 학습 기준에서 벗어남/);
  assert.doesNotMatch(content,/예측 완료|최근 정기 이력의 예측 · 현재 보고의 결과 아님/);
});
