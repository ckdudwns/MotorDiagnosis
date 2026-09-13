// Real persisted server results supplied by Python, without translating metadata.
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import {harness} from './dashboard_harness.mjs';
const input=JSON.parse(readFileSync(0,'utf8'));
const cases=Array.isArray(input)?input:input.snapshots;
const text=e=>typeof e==='string'?e:e.textContent+' '+e.children.map(text).join(' ');
for(const response of cases) {
  const h=harness(),scope={siteId:response.siteId,assetId:response.assetId};
  h.context.query=scope;
  h.get('siteSelect').value=scope.siteId;h.get('assetSelect').value=scope.assetId;
  h.context.respond=p=>p.endsWith('/devices')?[{id:response.deviceId,...scope}]:response;
  await h.run('loadSnapshots(query)');
  const content=text(h.get('snapshotRows'));
  assert.match(h.get('snapshotStatus').textContent,/조회 완료/);
  assert.doesNotMatch(content,/조회 실패|결과 형식 확인 필요/);
  assert.match(content,/5분 뒤 특징값 예측/);
  assert.match(content,/25초 등간격 정기 이력 · 이상 중에도 유지/);
  assert.match(content,/학습 원본 특징과의 동일성 미확인/);
  const row=response.items[0],f=row.analysis.forecast;
  if(f?.status==='completed') {
    assert.match(content,/9개 특징의 실측값과 예측값/);
    assert.match(content,/5분 뒤 예측 \(실측값 아님\)/);
    for(const [key,value] of Object.entries(f.features)) {
      assert.ok(content.includes(key));assert.ok(content.includes(String(value)));
    }
    h.context.row=row;
    assert.equal(h.run('snapshotForecastValid(row)'),true);
    for(const mutate of [r=>r.analysis.forecast.modelVersion='sha256:'+'0'.repeat(64),
      r=>r.analysis.forecast.inputDigests[12]='0'.repeat(64),r=>r.window.sensorId='OTHER',
      r=>r.analysis.forecast.predictedFor=r.window.timestamp,r=>r.analysis.forecast.affectsAlerts=true,
      r=>r.analysis.forecast.stream='OTHER',r=>r.window.sampleCount=1,
      r=>r.analysis.forecast.inputOrdinals[0]=0,r=>r.analysis.forecast.inputDigests[0]='bad',
      r=>r.analysis.forecast.inputOrdinals[0]=r.analysis.forecast.inputOrdinals[1],
      r=>r.analysis.forecast.inputOrdinals[0]=r.ordinal,
      r=>r.analysis.forecast.inputOrdinals[0]=r.ordinal+1,
      r=>r.analysis.forecast.inputOrdinals[0]=1.5]) {
      const bad=structuredClone(row);mutate(bad);h.context.row=bad;
      assert.equal(h.run('snapshotForecastValid(row)'),false);
    }
  } else assert.match(content,/예측 대기·불가/);
  assert.ok(h.requests.every(r=>!r.options.method||r.options.method==='GET'));
}
for(const row of input.notifications || []) {
  const h=harness();h.context.notification=row;
  h.run('renderNotifications([notification])');
  const content=text(h.get('notifications'));
  assert.match(content,row.event.snapshotTransition==='closed'
    ? /이력 모델 기준 미초과 · 사건 해제/ : /이력 모델 기준 초과 · 이상 후보/);
  assert.doesNotMatch(content,/정상 복귀|단건 모델 이상 발생/);
}
console.log(`${cases.length} real dual-model responses and forecast provenance checks passed.`);
