// Synthetic display cases only. Never sent to the production ingest API.
export const features=Object.fromEntries(['cf','sk','ku'].flatMap((p,j)=>[1,2,3].map((i)=>[`${p}_a_${i}`,j===1 ? -.06*i : 2.31+j+i/10])));
export const model={contractId:'edge-feature-history-model-v1',modelId:'pump-event-verifier-v1',
  modelVersion:'sha256:'+'a'.repeat(64),preprocessingVersion:'pump-dual-adxl25-v1:freshwater_supply_motor2',
  scope:{deviceId:'DEV-01-MOT-02',siteId:'SITE-01',assetId:'SITE-01-MOT-02',sensorId:'SENSOR-02'},
  scoreType:'novelty_reference_ratio',threshold:1,comparison:'>',
  inputMode:'experimental-adxl25',sourceFeatureEquivalenceVerified:false,domainValidated:false,
  inputContract:{adapterId:'adxl345-ac-nine-moments-v1',sourceProfileId:'adxl345-ac-cf-sk-ku-25s-v1',
    shape:[9],features:Object.keys(features),unit:'dimensionless',axes:['X','Y','Z'],sampleRateHz:800,sampleCount:512,
    sourceUnit:'g',meanRemoved:true,momentConvention:'population-pearson',historyIntervalSec:25,historyClock:'unix_epoch_25s',maxWindowEndAgeSec:1},
  forecastModel:{modelId:'pump-summary-experiment-v3',modelVersion:'sha256:'+'b'.repeat(64),stream:'freshwater_supply_motor2',
    windowRows:13,intervalSec:25,horizonSec:300,affectsAlerts:false,operationallyApproved:false}};
export function row(index=24,score=1.12) {
  const slot=1789272000+index*25,timestamp=new Date((slot-.64)*1000).toISOString(),digest='c'.repeat(64),ordinal=index+1;
  return {ordinal,digest,receivedAt:new Date((slot+2)*1000).toISOString(),lateArrival:false,
    window:{...model.scope,schemaVersion:3,bootId:'qa-boot',windowIndex:index*39,timestamp,quality:'valid',reason:null,
      profileId:model.inputContract.sourceProfileId,sampleCount:512,features:{...features},periodicSlotEpoch:slot,historySequence:index,
      startUptimeUs:1000000+index*25000000},
    transmission:{policyId:'edge-feature-history-v1',eventType:'periodic',state:'NORMAL',anomalyCount:0,normalCount:0},
    analysis:{status:'completed',reason:null,score,threshold:1,comparison:'>',scoreType:model.scoreType,verdict:score>1,
      modelVersion:model.modelVersion,affectsAlerts:false,evidence:{historicalRecordsUsed:24},
      inference:{model:structuredClone(model),inputDigest:digest,completedAt:new Date((slot+3)*1000).toISOString()},
      forecast:{...model.forecastModel,status:'completed',reason:null,features:Object.fromEntries(Object.entries(features).map(([k,v])=>[k,v+.12])),
        guard:{policyId:'pump-forecast-reject-v3',inputRobust:1.2,inputRobustLimit:4.24,outputClipped:false},
        basedOnMeasuredAt:timestamp,predictedFor:new Date(Date.parse(timestamp)+300000).toISOString(),targetSlotEpoch:slot+300,
        inputOrdinals:Array.from({length:13},(_,i)=>ordinal-12+i),inputDigests:Array(13).fill(digest)}}};
}
export function result(items=[row()]) {
  return {...model.scope,queriedAt:'2026-09-13T06:50:04Z',configuredModel:structuredClone(model),
    modelCompatibility:{status:'ready',reason:null},inferenceEnabled:true,affectsAlerts:false,eventPolicy:{mode:'events'},items};
}
