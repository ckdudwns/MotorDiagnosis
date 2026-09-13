// Local visual QA only: synthetic data, no API calls, no login, no production writes.
import {createServer} from 'node:http';
import {readFileSync} from 'node:fs';
import {row,result} from './pump_live_fixture.mjs';
const server=createServer((request,response)=>{
  const url=new URL(request.url,'http://localhost');
  const latest=row(),reply=result([latest,row(23,.87)]);
  reply.queriedAt=new Date(Date.parse(latest.receivedAt)+2000).toISOString();
  if(url.searchParams.get('state')==='waiting') {
    latest.analysis={status:'waiting_model',reason:'MODEL_NOT_CONFIGURED'};
    reply.items=[latest];
  }
  if(url.searchParams.get('state')==='invalid') {
    Object.assign(latest.window,{quality:'invalid',features:null,reason:'fifo_overrun'});
    latest.analysis={status:'unavailable',reason:'fifo_overrun'};reply.items=[latest];
  }
  if(url.searchParams.get('state')==='empty') reply.items=[];
  const html=readFileSync(new URL('../motor_diagnosis/web.py',import.meta.url),'utf8').match(/return r"""(<!doctype html>[\s\S]*<\/html>)"""/)[1];
  const injection=`
    api=async()=>{throw new Error('합성 화면 검사: 서버 요청은 차단됩니다.');};
    permissions=['*']; token=''; sites=[{id:'SITE-01',name:'화면 검증용 합성 사이트'}];
    setOptions($('siteSelect'),sites,r=>r.id,r=>r.name);
    setOptions($('assetSelect'),[{id:'SITE-01-MOT-02',name:'합성 모터 / 실제 데이터 아님'}],r=>r.id,r=>r.name);
    $('loginPanel').hidden=true; $('appPanel').hidden=false; setView('overview');
    $('signedInUser').textContent='화면 검증용 합성 데이터 · 실측 결과 아님';
    $('snapshotRows').appendChild(renderSnapshotCard(${JSON.stringify(reply)}));
    $('snapshotStatus').textContent='화면 검증용 합성 데이터 · 모든 API 요청 차단';
    renderNotifications([]);renderHealth([],null);
  `;
  response.writeHead(200,{'content-type':'text/html; charset=utf-8','cache-control':'no-store',
    'content-security-policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; img-src 'none'"});
  response.end(html.replace('</script>',injection+'</script>'));
});
server.listen(0,'127.0.0.1',()=>console.log(`Synthetic QA: http://127.0.0.1:${server.address().port}/`));
