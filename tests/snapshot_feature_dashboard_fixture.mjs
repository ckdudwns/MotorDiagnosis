// Python supplies unmodified persisted API responses, including real model metadata.
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import {harness} from './dashboard_harness.mjs';

const cases=JSON.parse(readFileSync(0,'utf8'));
const text=e=>typeof e==='string'?e:e.textContent+' '+e.children.map(text).join(' ');
assert.equal(cases.length,12);
for (const {response,compatibility} of cases) {
  const h=harness(),scope={siteId:response.siteId,assetId:response.assetId};
  h.context.query=scope;h.context.payload=response;
  h.get('siteSelect').value=scope.siteId;h.get('assetSelect').value=scope.assetId;
  h.context.respond=path=>path.endsWith('/devices')?[{id:response.deviceId,...scope}]:response;
  await h.run('loadSnapshots(query)');
  const rendered=text(h.get('snapshotRows'));
  const description=`${response.eventPolicy.mode} / ${compatibility}`;
  assert.match(h.get('snapshotStatus').textContent,/조회 완료/,description);
  assert.doesNotMatch(rendered,/조회 실패|결과 형식 확인 필요/,description);
  assert.match(rendered,/UTC 00·25·50초|최근 측정 시각/);
  assert.match(rendered,/해당 구간 서버 수신 시각/);
  assert.ok(rendered.includes(response.items[0].window.sensorId));
  assert.ok(rendered.includes(h.run('formatLocalTime(payload.items[0].window.timestamp)')));
  assert.ok(rendered.includes(h.run('formatLocalTime(payload.items[0].receivedAt)')));
  assert.match(rendered,/valid/);
  if (compatibility==='ready') {
    assert.match(rendered,/모델 이상 후보/);
  } else {
    const reason={input_contract_mismatch:'MODEL_INPUT_CONTRACT_MISMATCH',
      scope_mismatch:'MODEL_SCOPE_MISMATCH',not_configured:'MODEL_NOT_CONFIGURED'}[compatibility];
    assert.ok(rendered.includes(reason),description);
    assert.doesNotMatch(rendered,/모델 정상 후보|모델 이상 후보|이상 후보 · 학습 기준 초과/);
    assert.equal(response.inferenceEnabled,false);
    assert.equal(response.affectsAlerts,false);
  }
  assert.ok(h.requests.every(r=>!r.options.method||r.options.method==='GET'));
}
console.log('All 12 persisted feature/model/event-mode combinations retain receipt visibility.');
