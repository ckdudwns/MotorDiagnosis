// Run only from test_snapshot_dashboard.py; input is an actual persisted API response.
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import {harness} from './dashboard_harness.mjs';

const payload=JSON.parse(readFileSync(0,'utf8')),h=harness();
const text=e=>typeof e==='string' ? e : e.textContent+' '+e.children.map(text).join(' ');
h.context.payload=payload;
h.context.respond=path=>{
  if(path.startsWith('/api/anomaly/events/')) return payload.detail;
  if(path.includes('/reviews?')) return payload.reviews;
  if(path.endsWith('/notes')) return payload.notes;
  throw new Error('Unexpected request: '+path);
};
h.run('events=[payload.detail.event]');
await h.run('selectEvent(payload.detail.event.id)');
assert.match(text(h.get('eventDetail')),/단건 모델 발생 점수 0.8/);
assert.match(text(h.get('eventDetail')),/정상 판정으로 복귀/);
assert.match(text(h.get('evidenceSummary')),/서버 이상 1건 발생/);
assert.match(text(h.get('evidenceSummary')),/> 0.5 \(test_error\)/);
const evidence=JSON.parse(h.get('eventEvidence').textContent);
assert.equal(evidence.recoveryEvidence.score,0);
assert.equal(evidence.featureSnapshot.score,.8);
assert.equal(evidence.recoveryEvidence.digest,payload.snapshots.items[0].digest);
assert.doesNotMatch(text(h.get('eventDetail')),/RF66 확률|점수 null/);
h.run('renderNotifications(payload.alerts)');
assert.equal(h.get('notifications').children.length,2);
assert.match(text(h.get('notifications')),/단건 모델 이상 발생/);
assert.match(text(h.get('notifications')),/단건 모델 정상 복귀/);
const card=h.run('renderSnapshotCard(payload.snapshots)');
assert.match(text(card),/알림 허용/);
assert.match(text(card),/모델 정상 후보/);
assert.match(text(card),/사건 발생 여부는 이벤트 검수에서 확인/);
assert.ok(h.requests.every(r=>!r.options.method || r.options.method==='GET'));
console.log('Persisted snapshot incident, recovery evidence and both web notifications render correctly.');
