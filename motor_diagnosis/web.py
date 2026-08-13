from __future__ import annotations


def render_page() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bind Edge AI Motor Diagnosis</title>
  <style>
    :root { color-scheme: light; --ink:#17211f; --muted:#66716d; --line:#d8ded9; --bg:#f4f6f3; --panel:#fff; --teal:#14796f; --red:#c2413b; --amber:#b7791f; --blue:#2472a3; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Segoe UI, Malgun Gothic, sans-serif; background:var(--bg); color:var(--ink); letter-spacing:0; }
    header { display:flex; justify-content:space-between; gap:16px; align-items:center; padding:18px 22px; background:#17231f; color:white; }
    header h1 { margin:0; font-size:21px; }
    header span { color:#b8c9c2; font-size:13px; }
    main { padding:18px; display:grid; gap:14px; }
    .toolbar, .grid, .kpis { display:grid; gap:10px; }
    .toolbar { grid-template-columns:repeat(4, minmax(150px, 1fr)); }
    .grid { grid-template-columns:1fr 1.2fr; }
    .kpis { grid-template-columns:repeat(4, 1fr); }
    .panel, .kpi { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; box-shadow:0 14px 32px rgba(23,33,31,.07); }
    .kpi span, label, th, small { color:var(--muted); font-size:12px; font-weight:700; }
    .kpi strong { display:block; margin-top:6px; font-size:27px; }
    h2 { margin:0 0 12px; font-size:17px; }
    select, button, textarea { width:100%; min-height:38px; border:1px solid var(--line); border-radius:7px; padding:8px 10px; font:inherit; }
    button { background:var(--teal); color:white; border-color:var(--teal); font-weight:800; cursor:pointer; }
    button.secondary { background:white; color:var(--ink); border-color:var(--line); }
    button.danger { background:#f9e6e4; color:var(--red); border-color:#efc7c2; }
    table { width:100%; border-collapse:collapse; }
    th, td { border-bottom:1px solid var(--line); padding:9px; text-align:left; vertical-align:middle; }
    canvas { width:100%; height:280px; border:1px solid var(--line); border-radius:8px; background:white; }
    .event-list { display:grid; gap:9px; }
    .event { border:1px solid var(--line); border-radius:8px; padding:10px; background:white; text-align:left; color:var(--ink); }
    .pill { display:inline-flex; border-radius:999px; padding:3px 8px; font-size:12px; font-weight:800; background:#edf0f6; color:var(--blue); }
    .critical { background:#fbe6e4; color:var(--red); }
    .warning { background:#fff0d9; color:var(--amber); }
    .device { background:#e8ecf7; color:#4453a8; }
    .ok { background:#e4f4ec; color:var(--teal); }
    .detail { display:grid; gap:10px; }
    textarea { resize:vertical; min-height:90px; }
    @media (max-width:900px) { .toolbar,.grid,.kpis { grid-template-columns:1fr; } header { display:grid; } }
  </style>
</head>
<body>
  <header>
    <div><h1>Bind Edge AI 모터 센서</h1><span>1주차 백엔드 기준정보 API 프로토타입</span></div>
    <button class="secondary" id="exportBtn">CSV 내보내기</button>
  </header>
  <main>
    <section class="toolbar panel">
      <label>발전소<select id="siteSelect"></select></label>
      <label>설비<select id="assetSelect"></select></label>
      <button id="refreshBtn">새로고침</button>
      <button class="danger" id="injectBtn">이상 주입</button>
    </section>
    <section class="kpis">
      <div class="kpi"><span>대상 발전소</span><strong id="siteCount">-</strong></div>
      <div class="kpi"><span>온라인 장치</span><strong id="onlineCount">-</strong></div>
      <div class="kpi"><span>미확인 이벤트</span><strong id="openEvents">-</strong></div>
      <div class="kpi"><span>네트워크 유형</span><strong id="networkTypes">-</strong></div>
    </section>
    <section class="grid">
      <article class="panel">
        <h2>사이트 요약</h2>
        <table><thead><tr><th>발전소</th><th>망 유형</th><th>상태</th><th>장치</th></tr></thead><tbody id="siteRows"></tbody></table>
      </article>
      <article class="panel">
        <h2 id="chartTitle">실시간 신호</h2>
        <canvas id="chart" width="900" height="280"></canvas>
      </article>
      <article class="panel">
        <h2>이벤트 목록</h2>
        <div id="events" class="event-list"></div>
      </article>
      <article class="panel detail">
        <h2>이벤트 검토</h2>
        <div id="eventDetail">이벤트를 선택하세요.</div>
        <label>라벨<select id="labelSelect"><option>확인 필요</option><option>정상/오탐</option><option>이상 확인</option><option>정비 완료</option></select></label>
        <label>메모<textarea id="noteInput"></textarea></label>
        <button id="saveReview">검토 저장</button>
      </article>
    </section>
  </main>
  <script>
    let sites = [], events = [], networkProfiles = [], selectedEventId = null;
    const $ = (id) => document.getElementById(id);
    async function api(path, options) {
      const res = await fetch(path, options);
      const type = res.headers.get("content-type") || "";
      const body = type.includes("json") ? await res.json() : await res.text();
      if (!res.ok) throw new Error(body?.error?.message || path + " " + res.status);
      return body;
    }
    async function load() {
      const boot = await api("/api/bootstrap");
      sites = boot.sites;
      events = boot.events;
      networkProfiles = boot.networkProfiles;
      $("siteSelect").innerHTML = sites.map(s => `<option value="${s.id}">${s.name}</option>`).join("");
      $("networkTypes").textContent = networkProfiles.length;
      await renderAssets();
      await render();
    }
    function selectedSite() { return sites.find(s => s.id === $("siteSelect").value) || sites[0]; }
    async function renderAssets() {
      const site = selectedSite();
      const assets = await api(`/api/sites/${site.id}/assets`);
      $("assetSelect").innerHTML = assets.map(a => `<option value="${a.id}">${a.name}</option>`).join("");
    }
    async function render() {
      const site = selectedSite();
      const assetId = $("assetSelect").value;
      const telem = await api(`/api/telemetry?siteId=${site.id}&assetId=${assetId}`);
      events = await api("/api/events");
      $("siteCount").textContent = sites.length;
      $("onlineCount").textContent = sites.reduce((n, s) => n + s.onlineDevices, 0);
      $("openEvents").textContent = events.filter(e => e.label === "확인 필요").length + "건";
      $("siteRows").innerHTML = sites.slice(0, 12).map(s => `<tr><td>${s.name}</td><td>${s.networkType || s.network}</td><td>${s.status}</td><td>${s.onlineDevices}/${s.totalDevices}</td></tr>`).join("");
      $("events").innerHTML = events.map(e => `<button class="event" data-id="${e.id}"><b>${e.id} · ${e.title}</b><br><span class="pill ${e.severity}">${e.label}</span> ${e.time} · ${e.score}점</button>`).join("");
      $("chartTitle").textContent = site.name + " / " + assetId;
      draw(telem.points);
    }
    function draw(points) {
      const c = $("chart"), ctx = c.getContext("2d"), w = c.width, h = c.height, pad = 34;
      ctx.clearRect(0,0,w,h); ctx.fillStyle = "#fff"; ctx.fillRect(0,0,w,h);
      ctx.strokeStyle = "#d8ded9"; ctx.lineWidth = 1;
      for (let i=0;i<=4;i++){ const y=pad+(h-pad*2)/4*i; ctx.beginPath(); ctx.moveTo(pad,y); ctx.lineTo(w-pad,y); ctx.stroke(); }
      const series = [["score","#c2413b",p=>p.score],["vibration","#14796f",p=>Math.min(100,(p.vibrationRmsMmS || p.vibration)*22)],["acoustic","#4453a8",p=>Math.min(100,(p.acousticDb || p.acoustic)*1.15)]];
      for (const [name,color,map] of series) {
        ctx.strokeStyle = color; ctx.lineWidth = name === "score" ? 3 : 2; ctx.beginPath();
        points.forEach((p,i)=>{ const x=pad+(w-pad*2)*i/(points.length-1); const y=pad+(h-pad*2)*(1-map(p)/100); i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
        ctx.stroke();
      }
    }
    document.addEventListener("click", async (e) => {
      const row = e.target.closest("[data-id]");
      if (!row) return;
      selectedEventId = row.dataset.id;
      const event = events.find(item => item.id === selectedEventId);
      $("eventDetail").innerHTML = `<b>${event.title}</b><br>${event.duration} · ${event.score}점<br>${event.note}`;
      $("labelSelect").value = event.label;
      $("noteInput").value = event.note;
    });
    $("siteSelect").addEventListener("change", async () => { await renderAssets(); await render(); });
    $("assetSelect").addEventListener("change", render);
    $("refreshBtn").addEventListener("click", render);
    $("injectBtn").addEventListener("click", async () => {
      const event = await api("/api/demo/inject-anomaly", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({siteId:$("siteSelect").value, assetId:$("assetSelect").value})});
      selectedEventId = event.id; await render();
    });
    $("saveReview").addEventListener("click", async () => {
      if (!selectedEventId) return alert("이벤트를 먼저 선택하세요.");
      await api(`/api/events/${selectedEventId}/review`, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({label:$("labelSelect").value, note:$("noteInput").value})});
      await render();
    });
    $("exportBtn").addEventListener("click", () => { location.href = `/api/export?siteId=${$("siteSelect").value}&assetId=${$("assetSelect").value}`; });
    load().catch(error => alert(error.message));
  </script>
</body>
</html>"""

