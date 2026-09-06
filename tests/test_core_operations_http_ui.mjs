// Run the shipped UI in the existing DOM double, with real localhost HTTP.
// The owning Python test supplies an isolated server and a temporary user session.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {harness} from './test_ai2_dashboard.mjs';

const fixture=JSON.parse(readFileSync(0,'utf8'));
const base=new URL(fixture.baseUrl);
assert.equal(base.protocol,'http:'); assert.equal(base.hostname,'127.0.0.1');
const deviceId='DEV-01-MOT-02', siteId='SITE-01', oldAsset='SITE-01-MOT-02', newAsset='SITE-01-MOT-99';
const root=`/api/devices/${deviceId}`;
async function request(path,options={}) {
  const response=await fetch(new URL(path,base), {...options,headers:{'Content-Type':'application/json','Authorization':`Bearer ${fixture.token}`}});
  const body=await response.json();
  assert.ok(response.ok, `${options.method || 'GET'} ${path}: ${response.status}`);
  return body;
}
const json=(method,body)=>({method,body:JSON.stringify(body)});
const configRequest=(version,interval,reason)=>json('PUT',{expectedVersion:version,settings:{measurementIntervalMs:interval,replayBatchSize:2},reason});

await request(root+'/configuration',configRequest(0,6000,'Initial asset A request'));
const h=harness(); h.get('siteSelect').value=siteId; h.get('assetSelect').value=oldAsset;
h.get('opsQualityRange').value='24'; h.get('opsQualityBucket').value='3600';
h.context.respond=request;
await h.run('loadDeviceOperations()');
assert.equal(h.run('opsConfig.version'),1); assert.equal(h.get('opsConfigPublish').disabled,false);

// Simulate another administrator changing the mapping while the A view stays open.
await request(`/api/sites/${siteId}/assets`,json('POST',{
  id:newAsset,assetCode:'MOT.99',name:'Remap fixture target',assetType:'motor',ratedRpm:1800,installLocation:'Isolated test bay',
  baseline:{status:'ready',capturedAt:new Date().toISOString(),vibrationRmsMmS:1,acousticDb:50,sampleCount:10},
}));
const rollout=await request(`/api/sites/${siteId}/rollout-plan`);
await request(`/api/sites/${siteId}/rollout-plan`,json('PUT',{
  networkProfileId:rollout.networkProfileId,targetAssetIds:[...rollout.targetAssetIds,newAsset],installPriority:rollout.installPriority,note:'Remap regression fixture',
}));
await request(root,json('PATCH',{assetId:newAsset,replacementReason:'Fixture reassignment'}));
const commandB=await request(root+'/configuration',configRequest(1,9000,'Asset B request by another administrator'));
assert.equal(commandB.assetId,newAsset);
const configurationB=await request(root+'/configuration');
assert.equal(configurationB.scopeChanged,false); assert.equal(configurationB.version,2);

await h.run('loadOpsConfig()');
assert.equal(h.get('assetSelect').value,oldAsset); assert.equal(h.run('opsConfig'),null);
assert.equal(h.run('opsDevice'),null); assert.equal(h.get('opsConfigPublish').disabled,true);
assert.match(h.get('opsConfigStatus').textContent,/장치 목록.*다시 선택/);
h.get('opsInterval').value='12000'; h.get('opsReplay').value='2'; h.get('opsConfigReason').value='Stale A must never publish';
await h.run('publishOpsConfig()');
assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,0);
assert.deepEqual(await request(root+'/configuration'),configurationB);

// Recovery uses the actual refreshed device list in the newly selected B view.
h.get('assetSelect').value=newAsset;
await h.run('loadDeviceOperations()'); assert.equal(h.get('opsConfigPublish').disabled,false);
h.get('opsInterval').value='12000'; h.get('opsReplay').value='2'; h.get('opsConfigReason').value='Explicitly selected asset B';
await h.run('publishOpsConfig()');
assert.equal(h.requests.filter(row=>row.options.method==='PUT').length,1);
const final=await request(root+'/configuration');
assert.equal(final.version,3); assert.equal(final.desired.assetId,newAsset);
assert.equal(final.desired.reason,'Explicitly selected asset B');
assert.equal(h.run('opsConfig.desired.commandId'),final.desired.commandId);
console.log('PASS live HTTP remap: stale asset A issued zero PUTs; refreshed asset B issued one request');
