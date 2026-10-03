import test from 'node:test';
import assert from 'node:assert/strict';
import {harness, source} from './dashboard_harness.mjs';
import {row, result} from './pump_live_fixture.mjs';

const clone = value => JSON.parse(JSON.stringify(value));
const text = element => typeof element === 'string' ? element
  : String(element.textContent || '') + ' ' + element.children.map(text).join(' ');
const defer = () => {
  let resolve;
  const promise = new Promise(yes => {resolve = yes;});
  return {promise, resolve};
};
const query = {siteId:'S1', assetId:'A1', from:'2026-09-30T00:00:00Z', to:'2026-10-01T00:00:00Z'};
const event = id => ({id, title:`Inspection ${id}`, occurredAt:'2026-09-30T12:00:00Z',
  label:'needs_review', severity:'warning', status:'open'});
const page = items => ({items, page:1, size:12, total:items.length});

function fixture() {
  const h = harness();
  h.run('assetCatalog=[{id:"A1",name:"Motor 1"},{id:"A2",name:"Motor 2"}]');
  h.get('csvAsset').value = 'A1';
  h.get('csvPeriod').value = '24';
  return h;
}

function captureDownloads(h, respond) {
  const requests = [], blobs = [];
  h.context.fetch = async (url, options) => {
    requests.push({url, options});
    return respond(url, options);
  };
  h.context.URL = class extends URL {
    static createObjectURL(blob) {blobs.push(blob); return 'blob:measurements-test';}
    static revokeObjectURL() {}
  };
  return {requests, blobs};
}

test('primary navigation contains only equipment and incident history', () => {
  const mainNav = [...source.matchAll(/<nav\b[^>]*>([\s\S]*?)<\/nav>/g)]
    .find(match => match[1].includes('id="navOverview"'))?.[1];
  assert.ok(mainNav, 'equipment navigation is present in the actual HTML');
  const userNavigation = mainNav.replace(/<details\b[\s\S]*?<\/details>/g, '');
  const buttons = [...userNavigation.matchAll(/<button\b[^>]*id="([^"]+)"[^>]*aria-pressed="[^"]+"[^>]*>([\s\S]*?)<\/button>/g)];
  assert.deepEqual(buttons.map(match => match[1]), ['navOverview', 'navEvents']);
  assert.match(buttons[0][2], /설비 현황/);
  assert.match(buttons[1][2], /이상·조치 이력/);
  assert.match(source, /<details\b[^>]*id="adminMenu"[^>]*\bhidden\b/);
  for (const id of ['siteSelect', 'assetSelect', 'csvAsset', 'csvPeriod', 'csvStatus', 'memoText']) {
    assert.match(source, new RegExp(`\\bid="${id}"`), `${id} must exist; the DOM double cannot check markup`);
  }
});

test('administrator entry requires an administrative capability, not monitoring reads', () => {
  const h = fixture();
  for (const permissions of [[], ['event:read'], ['site:read','asset:read','device:read','telemetry:read','model:read']]) {
    h.context.testPermissions = permissions;
    h.run('permissions=testPermissions');
    assert.equal(h.run('hasAdminAccess()'), false);
  }
  for (const permission of ['*','audit-log:read','site:write','asset:write','device:write','user:write','model:review']) {
    h.context.testPermissions = [permission];
    h.run('permissions=testPermissions');
    assert.equal(h.run('hasAdminAccess()'), true, permission);
  }
});

test('measurement export requires export, telemetry and device access together', () => {
  const h = fixture();
  const required = ['export:read','telemetry:read','device:read'];
  for (const missing of required) {
    h.context.testPermissions = required.filter(permission => permission !== missing);
    h.run('permissions=testPermissions');
    assert.equal(h.run('canExportMeasurements()'), false, missing);
  }
  for (const permissions of [required, ['*']]) {
    h.context.testPermissions = permissions;
    h.run('permissions=testPermissions');
    assert.equal(h.run('canExportMeasurements()'), true);
  }
});

test('CSV range uses the chosen known asset and exact allowed time span', () => {
  const h = fixture();
  h.get('csvAsset').value = 'A2';
  for (const hours of [1,24,168]) {
    h.get('csvPeriod').value = String(hours);
    const q = clone(h.run('measurementExportQuery()'));
    assert.equal(q.siteId, 'S1');
    assert.equal(q.assetId, 'A2');
    assert.ok(Number.isFinite(Date.parse(q.from)) && Number.isFinite(Date.parse(q.to)));
    assert.equal(Date.parse(q.to) - Date.parse(q.from), hours * 3_600_000);
    assert.deepEqual(Object.keys(q).sort(), ['assetId','from','siteId','to']);
  }
});

test('CSV range rejects unknown assets, missing selection and unsupported periods', () => {
  for (const asset of ['', 'NOT-IN-CATALOG']) {
    const h = fixture();
    h.get('csvAsset').value = asset;
    assert.throws(() => h.run('measurementExportQuery()'));
  }
  for (const hours of ['', '0', '-1', '6', '169', 'Infinity', 'not-a-number']) {
    const h = fixture();
    h.get('csvPeriod').value = hours;
    assert.throws(() => h.run('measurementExportQuery()'), undefined, hours);
  }
  const h = fixture();
  h.get('siteSelect').value = '';
  assert.throws(() => h.run('measurementExportQuery()'));
});

test('CSV download uses only the measured snapshot endpoint with the authenticated transport', async () => {
  const h = fixture();
  const csv = '\ufefftimestamp,asset,cf_a_1\r\n2026-09-30T12:00:00Z,A1,2.41\r\n';
  const {requests, blobs} = captureDownloads(h, () => new Response(csv, {headers:{'content-type':'text/csv; charset=utf-8'}}));
  await h.run('downloadMeasurements()');
  assert.equal(requests.length, 1);
  const url = new URL(requests[0].url, 'http://local');
  assert.equal(url.origin, 'http://local');
  assert.ok(requests[0].url.startsWith('/api/'));
  assert.equal(url.pathname, '/api/periodic-snapshots/export');
  assert.equal(url.searchParams.get('siteId'), 'S1');
  assert.equal(url.searchParams.get('assetId'), 'A1');
  assert.equal(Date.parse(url.searchParams.get('to')) - Date.parse(url.searchParams.get('from')), 24 * 3_600_000);
  const options = requests[0].options;
  assert.equal(new Headers(options.headers).get('authorization'), 'Bearer test-token');
  assert.equal(options.credentials, 'omit');
  assert.equal(options.redirect, 'error');
  assert.equal(options.referrerPolicy, 'no-referrer');
  assert.equal(blobs.length, 1);
  assert.deepEqual([...new Uint8Array(await blobs[0].arrayBuffer())], [...new TextEncoder().encode(csv)]);
});

test('unauthorized CSV download does not contact either export endpoint', async () => {
  const h = fixture();
  h.run('permissions=["telemetry:read","device:read"]');
  const {requests, blobs} = captureDownloads(h, () => {throw new Error('must not fetch');});
  await h.run('downloadMeasurements()');
  assert.equal(requests.length, 0);
  assert.equal(blobs.length, 0);
});

test('CSV errors are visible, never downloaded and never fall back to legacy export', async () => {
  for (const response of [
    () => new Response('<html>Sign in</html>', {headers:{'content-type':'text/html'}}),
    () => new Response('{"error":{"message":"Export denied"}}', {status:403,headers:{'content-type':'application/json'}}),
  ]) {
    const h = fixture();
    const {requests, blobs} = captureDownloads(h, response);
    await h.run('downloadMeasurements()');
    assert.equal(requests.length, 1);
    assert.equal(blobs.length, 0);
    assert.ok(h.get('csvStatus').textContent.trim(), 'a rejected response must have a user-visible explanation');
    assert.ok(requests.every(request => new URL(request.url, 'http://local').pathname === '/api/periodic-snapshots/export'));
  }
});

test('CSV ignores a completed response after any export scope change', async () => {
  for (const change of [
    h => h.run('token="different-session"'),
    h => {h.get('siteSelect').value = 'S2';},
    h => {h.get('assetSelect').value = 'A2';},
    h => {h.get('csvAsset').value = 'A2';},
    h => {h.get('csvPeriod').value = '1';},
  ]) {
    const h = fixture(), pendingResponse = defer();
    const {requests, blobs} = captureDownloads(h, () => pendingResponse.promise);
    const pending = h.run('downloadMeasurements()');
    assert.equal(requests.length, 1);
    change(h);
    pendingResponse.resolve(new Response('timestamp,asset\r\nold,A1\r\n', {headers:{'content-type':'text/csv'}}));
    await pending;
    assert.equal(blobs.length, 0, 'a stale response must never create a download');
  }
});

test('CSV blocks duplicate clicks while its original request is pending', async () => {
  const h = fixture(), pendingResponse = defer();
  const {requests, blobs} = captureDownloads(h, () => pendingResponse.promise);
  const first = h.run('downloadMeasurements()');
  await h.run('downloadMeasurements()');
  assert.equal(requests.length, 1);
  pendingResponse.resolve(new Response('timestamp,asset\r\nnew,A1\r\n', {headers:{'content-type':'text/csv'}}));
  await first;
  assert.equal(blobs.length, 1);
});

test('CSV rechecks the selection after the response body finishes loading', async () => {
  const h = fixture(), body = defer(), bodyStarted = defer();
  const {blobs} = captureDownloads(h, () => ({
    ok:true,
    headers:new Headers({'content-type':'text/csv'}),
    blob() {bodyStarted.resolve(); return body.promise;},
  }));
  const pending = h.run('downloadMeasurements()');
  await bodyStarted.promise;
  h.get('csvAsset').value = 'A2';
  body.resolve(new Blob(['timestamp,asset\r\nold,A1\r\n'], {type:'text/csv'}));
  await pending;
  assert.equal(blobs.length, 0);
});

test('operating history renders with event-only access and has no legacy dependencies', async () => {
  const h = fixture();
  h.run('permissions=["event:read"];currentView="events"');
  h.context.query = query;
  h.context.respond = path => {
    assert.ok(path.startsWith('/api/events?'), `unexpected dependency: ${path}`);
    return page([event('E1')]);
  };
  await h.run('loadOperatingHistory(query)');
  assert.equal(h.requests.length, 1);
  assert.equal(h.run('eventTotal'), 1);
  assert.match(text(h.get('events')), /Inspection E1/);
});

test('operating history respects missing read permission before requesting data', async () => {
  const h = fixture();
  h.run('permissions=[];currentView="events"');
  h.context.query = query;
  await h.run('loadOperatingHistory(query)');
  assert.equal(h.requests.length, 0);
});

test('newer history selection wins even when the earlier response arrives last', async () => {
  const h = fixture(), previous = defer();
  h.run('currentView="events"');
  h.context.query = query;
  h.context.respond = path => new URL(path, 'https://example.test').searchParams.get('assetId') === 'A1'
    ? previous.promise : page([event('NEW')]);
  const pending = h.run('loadOperatingHistory(query)');
  h.get('assetSelect').value = 'A2';
  h.context.nextQuery = {...query, assetId:'A2'};
  await h.run('loadOperatingHistory(nextQuery)');
  previous.resolve(page([event('OLD')]));
  await pending;
  assert.deepEqual(clone(h.run('events.map(row=>row.id)')), ['NEW']);
  assert.match(text(h.get('events')), /Inspection NEW/);
  assert.doesNotMatch(text(h.get('events')), /Inspection OLD/);
});

test('history does not commit a response from a replaced session or site', async () => {
  for (const change of [
    h => h.run('token="new-session"'),
    h => {h.get('siteSelect').value = 'S2';},
    h => {h.get('assetSelect').value = 'A2';},
  ]) {
    const h = fixture(), response = defer();
    h.run('currentView="events"');
    h.context.query = query;
    h.context.respond = () => response.promise;
    const pending = h.run('loadOperatingHistory(query)');
    change(h);
    response.resolve(page([event('STALE')]));
    await pending;
    assert.ok(!h.run('events.some(row=>row.id === "STALE")'));
    assert.doesNotMatch(text(h.get('events')), /Inspection STALE/);
  }
});

test('read-only monitoring users see neither administrator entry nor admin workspaces', () => {
  const h = fixture();
  h.run('permissions=["event:read","device:read","telemetry:read","model:read"];setView("overview")');
  assert.equal(h.get('adminMenu').hidden, true);
  for (const view of ['management','deviceOps','models']) {
    h.run(`setView("${view}")`);
    assert.equal(h.run('currentView'), 'overview');
  }
  h.run('permissions=["*"];setView("overview")');
  assert.equal(h.get('adminMenu').hidden, false);
});

test('compact current verdict respects valid model evidence and folds the technical content', () => {
  const h = fixture();
  for (const [score, expectedClass] of [[.65,'verdict ready'], [1.12,'verdict anomaly']]) {
    const r = row(24,score), reply = result([r]);
    reply.queriedAt = new Date(Date.parse(r.window.timestamp) + 1000).toISOString();
    h.context.reply = reply;
    const before = JSON.stringify(reply);
    const card = h.run('renderOperatingSnapshotCard(reply)');
    assert.equal(card.className, 'operating-card');
    const verdict = card.children.find(child => String(child.className).startsWith('verdict '));
    assert.equal(verdict.className, expectedClass);
    const detail = card.children.find(child => child.tagName === 'details');
    assert.ok(detail, 'numeric and technical details remain reachable');
    assert.equal(Boolean(detail.open), false);
    assert.match(text(detail), /측정값·예측 보기/);
    assert.match(text(detail), /예측 완료/);
    assert.equal(JSON.stringify(reply), before, 'presentation does not rewrite server evidence');
    assert.match(text(card), /알림 꺼짐/);
  }
});

test('compact summary never promotes stale, incompatible or incomplete data to a current verdict', () => {
  for (const mutate of [
    reply => {reply.queriedAt = new Date(Date.parse(reply.items[0].window.timestamp) + 86_000).toISOString();},
    reply => {reply.queriedAt = new Date(Date.parse(reply.items[0].window.timestamp) - 6000).toISOString();},
    reply => {reply.items[0].window.quality = 'invalid';},
    reply => {reply.items[0].window.sensorId = 'OTHER';},
    reply => {reply.items[0].analysis.status = 'queued_inference';},
    reply => {reply.items[0].analysis.inference.model.preprocessingVersion = 'previous-model';},
    reply => {reply.inferenceEnabled = false;},
    reply => {reply.items = [];},
  ]) {
    const h = fixture(), r = row(24,.65), reply = result([r]);
    reply.queriedAt = new Date(Date.parse(r.window.timestamp) + 1000).toISOString();
    mutate(reply);
    h.context.reply = reply;
    const card = h.run('renderOperatingSnapshotCard(reply)');
    const verdict = card.children.find(child => String(child.className).startsWith('verdict '));
    assert.equal(verdict.className, 'verdict unavailable');
    assert.doesNotMatch(verdict.textContent, /기준 미초과|정상|이상 후보 감지/);
  }
});

test('single action memo supplies its audit reason and marks an unchanged save complete', async () => {
  const h = fixture();
  h.context.reloads = [];
  h.context.renderCalls = 0;
  h.run('selectedEventId="E1";selectedDetail={};reviewDirty=true;selectEvent=async(...args)=>{reloads.push(args)};render=async()=>{renderCalls++}');
  h.get('labelSelect').value = 'repair_completed';
  h.get('noteInput').value = '  현장 점검 후 조치를 마쳤습니다.  ';
  h.get('reviewReason').value = '';
  h.context.respond = () => ({});
  await h.get('saveReview').listeners.click();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].path, '/api/events/E1/review');
  assert.equal(h.requests[0].options.method, 'POST');
  const payload = JSON.parse(h.requests[0].options.body);
  assert.equal(payload.note, '  현장 점검 후 조치를 마쳤습니다.  ');
  assert.equal(payload.reason, '현장 점검 후 조치를 마쳤습니다.');
  assert.equal(payload.label, 'repair_completed');
  assert.equal(h.run('reviewDirty'), false);
  assert.equal(h.run('reviewSaving'), false);
  assert.deepEqual(clone(h.context.reloads), [['E1',true]]);
  assert.equal(h.context.renderCalls, 1);
  assert.match(h.get('appStatus').textContent, /저장했습니다/);
  assert.doesNotMatch(h.get('appStatus').textContent, /아직 저장되지/);
});

test('editing the action memo during its save keeps the newer draft dirty', async () => {
  const h = fixture(), response = defer();
  h.context.reloads = [];
  h.run('selectedEventId="E1";selectedDetail={};reviewDirty=true;selectEvent=async(...args)=>{reloads.push(args)};render=async()=>{}');
  h.get('labelSelect').value = 'confirmed_anomaly';
  h.get('noteInput').value = '첫 점검 내용';
  h.get('reviewReason').value = '';
  h.context.respond = () => response.promise;
  const pending = h.get('saveReview').listeners.click();
  assert.equal(h.requests.length, 1);
  h.get('noteInput').value = '저장 중에 추가한 점검 내용';
  h.run('reviewDirty=true');
  response.resolve({});
  await pending;
  assert.equal(JSON.parse(h.requests[0].options.body).reason, '첫 점검 내용');
  assert.equal(h.get('noteInput').value, '저장 중에 추가한 점검 내용');
  assert.equal(h.run('reviewDirty'), true);
  assert.equal(h.run('reviewSaving'), false);
  assert.deepEqual(h.context.reloads, []);
  assert.match(h.get('appStatus').textContent, /아직 저장되지 않았습니다/);
});
