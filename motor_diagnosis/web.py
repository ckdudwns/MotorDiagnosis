from __future__ import annotations


def render_page() -> str:
    return r"""<!doctype html>
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
    .toolbar, .grid, .kpis, .login { display:grid; gap:10px; }
    .login { grid-template-columns:1fr 1fr 140px; align-items:end; }
    .toolbar { grid-template-columns:repeat(5, minmax(150px, 1fr)); }
    .grid { grid-template-columns:1fr 1.2fr; }
    .kpis { grid-template-columns:repeat(4, 1fr); }
    .panel, .kpi { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; box-shadow:0 14px 32px rgba(23,33,31,.07); }
    .kpi span, label, th, small { color:var(--muted); font-size:12px; font-weight:700; }
    .kpi strong { display:block; margin-top:6px; font-size:27px; }
    h2 { margin:0 0 12px; font-size:17px; }
    input, select, button, textarea { width:100%; min-height:38px; border:1px solid var(--line); border-radius:7px; padding:8px 10px; font:inherit; }
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
    .normal { background:#e4f4ec; color:var(--teal); }
    .detail { display:grid; gap:10px; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:10px 0; }
    .actions button { width:auto; }
    .table-scroll { overflow:auto; }
    .subgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }
    .panel { min-width:0; }
    button:disabled { cursor:default; opacity:.5; }
    .selected { outline:2px solid var(--teal); }
    .notice { padding:10px; border-left:4px solid var(--teal); white-space:pre-wrap; overflow-wrap:anywhere; }
    .error { border-color:var(--red); color:var(--red); }
    pre { white-space:pre-wrap; overflow-wrap:anywhere; font-size:13px; max-height:320px; overflow:auto; }
    summary { cursor:pointer; font-weight:700; padding:8px 0; }
    label { display:grid; gap:4px; font-size:14px; }
    .wide { grid-column:1 / -1; }
    .history { max-height:300px; overflow:auto; }
    #evidenceChart { height:220px; }
    textarea { resize:vertical; min-height:90px; }
    [hidden] { display:none !important; }
    @media (max-width:900px) { .login,.toolbar,.grid,.kpis { grid-template-columns:1fr; } header { display:grid; } }
  </style>
</head>
<body>
  <header>
    <div><h1>Bind Edge AI Motor Diagnosis</h1><span>실측 모니터링 · 이벤트 검수</span></div>
    <span id="signedInUser"></span>
  </header>
  <main>
    <section class="login panel" id="loginPanel">
      <label>User<input id="username" value="admin" autocomplete="username"></label>
      <label>Password<input id="password" type="password" autocomplete="current-password"></label>
      <button id="loginBtn">Login</button>
    </section>
    <section id="appPanel" hidden>
      <div id="appStatus" class="notice" role="status" aria-live="polite" hidden></div>
      <section class="toolbar panel">
        <label>Site<select id="siteSelect"></select></label>
        <label>Asset<select id="assetSelect"></select></label>
        <label>Period<select id="periodSelect">
          <option value="1">Last 1 hour</option>
          <option value="6">Last 6 hours</option>
          <option value="24" selected>Last 24 hours</option>
        </select></label>
        <button id="refreshBtn">Refresh</button>
        <button class="danger" id="injectBtn" hidden>Inject Anomaly (Demo)</button>
      </section>
      <section class="panel">
        <div class="subgrid">
          <label>조회 시작 (현지 시각)<input id="fromInput" type="datetime-local" step="1"></label>
          <label>조회 종료 (현지 시각)<input id="toInput" type="datetime-local" step="1"></label>
          <label>데이터셋 버전<select id="datasetSelect"><option value="">실시간 실측 데이터</option></select></label>
          <label>내보내기 형식<select id="exportFormat"><option value="csv">CSV</option><option value="xlsx">XLSX</option></select></label>
        </div>
        <div class="actions"><button id="applyRange">기간 적용</button><button id="exportBtn" class="secondary" disabled>선택 조건 내보내기</button></div>
        <small id="exportScope">내보내기는 설비·조회 기간·데이터셋 버전을 사용합니다. 아래 이벤트 목록 필터는 신호 데이터에 적용되지 않습니다.</small>
      </section>
      <section class="kpis">
        <div class="kpi"><span>Visible Sites</span><strong id="siteCount">-</strong></div>
        <div class="kpi"><span>Online Devices</span><strong id="onlineCount">-</strong></div>
        <div class="kpi"><span>Warning Assets</span><strong id="warningAssets">-</strong></div>
        <div class="kpi"><span>Critical Assets</span><strong id="criticalAssets">-</strong></div>
      </section>
      <section class="grid">
        <article class="panel">
          <h2>Site Summary</h2>
          <div class="table-scroll"><table><thead><tr><th>Site</th><th>Region</th><th>Status</th><th>Normal</th><th>Warning</th><th>Critical</th><th>Unreviewed</th><th>Last received</th><th>Devices</th></tr></thead><tbody id="siteRows"></tbody></table></div>
          <div class="actions"><button id="sitesPrev" class="secondary">이전</button><span id="sitesPage"></span><button id="sitesNext" class="secondary">다음</button></div>
        </article>
        <article class="panel">
          <h2 id="chartTitle">Telemetry</h2>
          <canvas id="chart" width="900" height="280"></canvas>
          <small id="chartHint">Hover the chart to inspect a timestamp and raw values.</small>
          <div class="actions"><button id="zoomIn" class="secondary">확대</button><button id="panLeft" class="secondary">이전 구간</button><button id="panRight" class="secondary">다음 구간</button><button id="zoomReset" class="secondary">전체 구간</button></div>
          <small id="chartRange"></small>
        </article>
        <article class="panel">
          <h2>Events</h2>
          <div class="subgrid">
            <label>심각도<select id="severityFilter"><option value="">전체</option><option value="critical">Critical</option><option value="warning">Warning</option><option value="device">Device</option></select></label>
            <label>라벨<select id="eventLabelFilter"><option value="">전체</option><option value="needs_review">Needs review</option><option value="normal_false_positive">Normal / false positive</option><option value="confirmed_anomaly">Confirmed anomaly</option><option value="sensor_issue">Sensor issue</option><option value="repair_completed">Repair completed</option></select></label>
            <label>검수 상태<select id="reviewedFilter"><option value="">전체</option><option value="false">미검수</option><option value="true">검수 완료</option></select></label>
            <label>정렬<select id="eventSort"><option value="unreviewed_desc">미검수 우선</option><option value="occurredAt_desc">최신순</option><option value="occurredAt_asc">오래된순</option><option value="score_desc">최대 점수순</option></select></label>
          </div>
          <div id="events" class="event-list"></div>
          <div class="actions"><button id="eventsPrev" class="secondary">이전</button><span id="eventsPage"></span><button id="eventsNext" class="secondary">다음</button></div>
        </article>
        <article class="panel">
          <h2>Web Notifications</h2>
          <div id="notifications" role="status" aria-live="polite">No notifications.</div>
        </article>
        <article class="panel">
          <h2>AI Baseline Result</h2>
          <div id="modelResult" role="status" aria-live="polite">Loading model metadata.</div>
        </article>
        <article class="panel detail">
          <h2>Event Review</h2>
          <div id="eventDetail">Select an event.</div>
          <canvas id="evidenceChart" width="900" height="220" aria-label="선택 이벤트 전후 신호"></canvas>
          <details><summary>특징·규칙·장치 스냅샷</summary><pre id="eventEvidence"></pre></details>
          <label>Label<select id="labelSelect">
            <option value="needs_review">Needs review</option>
            <option value="normal_false_positive">Normal / false positive</option>
            <option value="confirmed_anomaly">Confirmed anomaly</option>
            <option value="sensor_issue">Sensor issue</option>
            <option value="repair_completed">Repair completed</option>
          </select></label>
          <label>Note<textarea id="noteInput"></textarea></label>
          <label>검수 변경 사유<input id="reviewReason" maxlength="1000"></label>
          <button id="saveReview" disabled>Save Review</button>
          <details><summary>전체 검수 변경 이력</summary><div id="reviewHistory" class="history"></div><button id="reviewsMore" class="secondary" hidden>이력 더 보기</button></details>
          <h2>원인·점검·조치 메모</h2>
          <div id="notesStatus" role="status" aria-live="polite"></div>
          <button id="notesRefresh" class="secondary" disabled>메모 목록 새로고침</button>
          <div id="eventNotes"></div>
          <label>분류<select id="noteCategory"><option value="root_cause">원인</option><option value="inspection">점검</option><option value="action">조치</option></select></label>
          <label>개별 메모<textarea id="memoText" maxlength="2000"></textarea></label>
          <label>첨부 참조 (한 줄에 하나)<textarea id="attachmentRefs" placeholder="https:// 또는 survey:// 참조"></textarea></label>
          <div class="actions"><button id="saveNote" disabled>메모 등록</button><button id="cancelNote" class="secondary">편집 취소</button></div>
          <div id="noteHistory" class="history"></div>
        </article>
        <article class="panel wide"><h2>장치·서비스 상태</h2><div id="deviceHealth" class="table-scroll"></div><div id="serviceHealth"></div></article>
        <article class="panel wide" id="managementPanel">
          <h2>운영 관리</h2>
          <div class="subgrid"><label>관리 항목<select id="managementKind"></select></label><label>항목 선택<select id="managementRecord"></select></label></div>
          <div class="actions"><button id="managementLoad" class="secondary">목록 새로고침</button><button id="managementNew" class="secondary" hidden>신규 등록</button><button id="managementDelete" class="danger" hidden>삭제</button></div>
          <div id="managementStatus" class="notice" role="status"></div>
          <form id="managementForm"><div id="managementFields" class="subgrid"></div><label>변경 사유<input id="managementReason" maxlength="1000" required></label><button id="managementSave" type="submit" hidden>변경 저장</button></form>
          <details><summary>현재 등록 정보·이력</summary><pre id="managementDetail"></pre></details>
          <section id="auditPanel" hidden><h2>감사 기록</h2><div class="subgrid"><label>작업 종류<input id="auditAction"></label><label>대상 종류<input id="auditTarget"></label><label>작성자 ID<input id="auditActor"></label></div><div class="actions"><button id="auditLoad" class="secondary">조회</button><button id="auditPrev" class="secondary">이전</button><span id="auditPage"></span><button id="auditNext" class="secondary">다음</button></div><div id="auditRows" class="history"></div></section>
        </article>
      </section>
    </section>
  </main>
  <script>
    let token = "", sites = [], events = [], siteSummaries = [], selectedEventId = null, latestPoints = [], latestUnits = {}, renderGeneration = 0, assetGeneration = 0;
    let permissions = [], eventPageNumber = 1, sitePageNumber = 1, eventTotal = 0;
    let detailGeneration = 0, selectedDetail = null, noteRows = [], editingNoteId = null, reviewPage = 1;
    let reviewDirty = false, memoDirty = false, chartViewport = null, chartSelectionAt = null;
    let allPoints = [], chartEvents = [], currentRule = null, activeWindow = null, lastQuery = null;
    const PAGE_SIZE = 12;
    let managementGeneration = 0, managementRows = [], managementRow = null, managementInputs = [], managementCreating = false, managementDirty = false, managementScope = null;
    let auditPageNumber = 1, auditGeneration = 0;
    let reviewSaving = false, noteSaving = false, managementSaving = false, noteHistoryGeneration = 0;
    let noteListGeneration = 0;
    const CHART_PAD = 34;
    const $ = (id) => document.getElementById(id);

    async function api(path, options = {}) {
      const headers = {...(options.headers || {})};
      if (token) headers.authorization = `Bearer ${token}`;
      const res = await fetch(path, {...options, headers});
      const type = res.headers.get("content-type") || "";
      const body = type.includes("json") ? await res.json() : await res.text();
      if (!res.ok) throw new Error(body?.error?.message || `${path} ${res.status}`);
      return body;
    }

    async function login() {
      const response = await api("/api/auth/login", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({username: $("username").value, password: $("password").value})
      });
      token = response.session.token;
      permissions = response.rolePolicy.permissions;
      $("signedInUser").textContent = `${response.user.name} · ${response.rolePolicy.name}`;
      $("loginPanel").hidden = true;
      $("appPanel").hidden = false;
      await load();
    }

    async function load() {
      const boot = await api("/api/bootstrap");
      sites = boot.sites;
      events = boot.events;
      $("injectBtn").hidden = !boot.demoEnabled;
      setOptions($("siteSelect"), sites, item => item.id, item => item.name);
      setupManagement();
      if (can("dataset:read")) {
        const datasets = await apiPages("/api/datasets");
        setOptions($("datasetSelect"), [{id:"", name:"실시간 실측 데이터"}, ...datasets.filter(item => item.source?.type === "internal")], item => item.id, item => item.id ? `${item.name || item.id} · ${item.id}` : item.name);
      }
      if (!sites.length) { statusMessage("조회 가능한 사이트가 없습니다."); return; }
      await renderAssets();
      await render();
    }

    function can(permission) { return permissions.includes("*") || permissions.includes(permission); }
    function statusMessage(message, error = false) {
      $("appStatus").hidden = !message;
      $("appStatus").className = error ? "notice error" : "notice";
      $("appStatus").textContent = message;
    }
    async function act(callback) {
      try { await callback(); } catch (error) { statusMessage(error.message, true); }
    }
    function jsonOptions(method, payload) {
      return {method, headers:{"content-type":"application/json"}, body:JSON.stringify(payload)};
    }
    async function apiPages(path) {
      const rows = [];
      for (let page = 1; ; page++) {
        const result = await api(`${path}${path.includes("?") ? "&" : "?"}page=${page}&size=200`);
        rows.push(...result.items);
        if (!result.items.length || rows.length >= result.total) return rows;
      }
    }
    function windowForSelection() {
      if (!activeWindow) {
        const to = new Date();
        activeWindow = {from:new Date(to.getTime() - Number($("periodSelect").value || 24) * 3600000).toISOString(), to:to.toISOString()};
      }
      return activeWindow;
    }
    function selectionQuery() {
      const site = selectedSite(), assetId = $("assetSelect").value;
      if (!site || !assetId) return null;
      return {siteId:site.id, assetId, ...windowForSelection()};
    }
    function scopeParams(query) { return new URLSearchParams(query).toString(); }
    function eventFilterParams() {
      return new URLSearchParams({severity:$("severityFilter").value, label:$("eventLabelFilter").value, reviewed:$("reviewedFilter").value, sort:$("eventSort").value || "unreviewed_desc", page:eventPageNumber, size:PAGE_SIZE}).toString();
    }

    function selectedSite() {
      return sites.find(s => s.id === $("siteSelect").value) || sites[0];
    }

    async function renderAssets(requestGeneration = assetGeneration) {
      const site = selectedSite();
      if (!site) return false;
      const siteId = site.id;
      const assets = await api(`/api/sites/${site.id}/assets`);
      if (requestGeneration !== assetGeneration || siteId !== selectedSite().id) return false;
      setOptions($("assetSelect"), assets, item => item.id, item => item.name);
      $("assetSelect").disabled = !assets.length;
      return true;
    }

    async function render() {
      const requestGeneration = ++renderGeneration;
      const query = selectionQuery();
      if (!query) { statusMessage("조회 가능한 사이트 또는 설비가 없습니다."); return; }
      if (JSON.stringify(query) !== JSON.stringify(lastQuery)) {lastQuery = null; $("exportBtn").disabled = true;}
      const site = selectedSite();
      const assetId = query.assetId, from = query.from, to = query.to;
      const [telem, summaries, eventPage, alertPage, modelPage, rule, devices, dependencies] = await Promise.all([
        api(`/api/telemetry?${scopeParams(query)}`),
        api("/api/dashboard/sites-summary"),
        api(`/api/events?siteId=${site.id}&assetId=${assetId}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&${eventFilterParams()}`),
        api(`/api/alerts?siteId=${site.id}&channel=web&status=sent&size=10`),
        api(`/api/model-versions?siteId=${site.id}&assetId=${assetId}&status=draft&size=1`),
        can("anomaly-rule:read") ? api(`/api/anomaly/rules/${encodeURIComponent(assetId)}`) : null,
        can("device:read") ? api(`/api/sites/${encodeURIComponent(site.id)}/devices`).then(rows => Promise.all(rows.map(device => api(`/api/devices/${encodeURIComponent(device.id)}/health`)))) : [],
        can("service-health:read") ? api("/api/health/dependencies").catch(error => ({error:error.message})) : null,
      ]);
      if (requestGeneration !== renderGeneration) return;
      const lastPage = Math.max(1, Math.ceil(eventPage.total / PAGE_SIZE));
      if (eventPageNumber > lastPage) {eventPageNumber = lastPage; return render();}
      lastQuery = query;
      $("exportBtn").disabled = !can("export:read");
      siteSummaries = summaries;
      events = eventPage.items;
      eventTotal = eventPage.total;
      currentRule = rule;
      renderNotifications(alertPage.items);
      renderModelResult(modelPage.items);
      renderHealth(devices, dependencies);
      $("siteCount").textContent = siteSummaries.length;
      $("onlineCount").textContent = siteSummaries.reduce((n, s) => n + s.onlineDevices, 0);
      $("warningAssets").textContent = siteSummaries.reduce((n, s) => n + s.warningAssets, 0);
      $("criticalAssets").textContent = siteSummaries.reduce((n, s) => n + s.criticalAssets, 0);
      renderSiteRows();
      renderEvents();
      renderPager("events", eventPageNumber, eventTotal);
      $("chartTitle").textContent = `${site.name} / ${assetId}`;
      allPoints = telem.points;
      chartEvents = events;
      draw(telem.points, telem.units, events);
      // Mark every event in the chart's period, independently of list pagination.
      act(async () => {
        const allEvents = await apiPages(`/api/events?${scopeParams({siteId:query.siteId,assetId:query.assetId})}&sort=occurredAt_asc`);
        if (requestGeneration !== renderGeneration) return;
        chartEvents = allEvents;
        redrawChart();
      });
    }

    function renderPager(prefix, page, total) {
      const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      $(prefix + "Page").textContent = `${page} / ${pages} · ${total}건`;
      $(prefix + "Prev").disabled = page <= 1;
      $(prefix + "Next").disabled = page >= pages;
    }
    function renderHealth(devices, dependencies) {
      const panel = $("deviceHealth");
      panel.replaceChildren();
      if (!devices.length) panel.textContent = "조회 가능한 장치가 없습니다.";
      for (const health of devices) {
        const box = document.createElement("details");
        const title = document.createElement("summary");
        title.textContent = `${health.deviceId || health.id} · ${health.health || health.status || "상태 미수신"}`;
        const metrics = document.createElement("p");
        metrics.textContent = `RSSI ${health.rssiDbm ?? "-"} dBm · 재부팅 ${health.rebootCount ?? "-"}회 · 버퍼 ${health.bufferUsagePct ?? "-"}% · 센서 ${health.sensorHealth || "미수신"} · 오류 ${(health.activeSensorFaults || []).length}건`;
        const values = document.createElement("pre");
        values.textContent = JSON.stringify(health, null, 2);
        box.append(title, values); panel.append(metrics, box);
      }
      const servicePanel = $("serviceHealth"); servicePanel.replaceChildren();
      if (!dependencies || dependencies.error) servicePanel.textContent = dependencies?.error || "서비스 상태 조회 권한이 없습니다.";
      else {
        const values = document.createElement("pre"); values.textContent = JSON.stringify(dependencies, null, 2); servicePanel.appendChild(values);
      }
    }

    function renderNotifications(rows) {
      const panel = $("notifications");
      panel.replaceChildren();
      if (!rows.length) panel.textContent = "No notifications.";
      rows.forEach(row => {
        const item = document.createElement("p");
        item.textContent = `${row.isTest ? "[TEST] " : ""}${row.event.isSynthetic ? "[SYNTHETIC] " : ""}${row.event.title} · ${new Date(row.deliveredAt).toLocaleString()}`;
        panel.appendChild(item);
      });
    }

    function renderModelResult(rows) {
      const panel = $("modelResult");
      panel.replaceChildren();
      const model = rows[0];
      if (!model) {
        panel.textContent = "No model metadata is registered for this asset. AI-1 reconstruction error is not mapped to anomalyScore in this MVP.";
        return;
      }
      const title = document.createElement("b");
      title.textContent = `${model.version} · ${model.deploymentStatus || "not_deployed"}`;
      panel.appendChild(title);
      appendModelField(panel, "Evaluation scope", "Reported metrics are not deployment or field-generalization evidence.");
      appendModelField(panel, "Artifact", model.artifactVerified ? "verified" : "reference only; not verified");
      appendModelField(panel, "Baseline version", model.baselineVersion);
      appendModelField(panel, "Baseline features", model.baselineSnapshot?.features);
      appendModelField(panel, "Metrics", model.metrics || {});
      appendModelField(panel, "Known error cases", model.errorCases || []);
      appendModelField(panel, "Domain gap", model.domainGap);
      appendModelField(panel, "Field calibration", model.fieldCalibrationPlan);
      appendModelField(panel, "Limitations", model.limitations);
    }

    function appendModelField(panel, label, value) {
      if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return;
      const row = document.createElement("p");
      const name = document.createElement("b");
      name.textContent = `${label}: `;
      const text = Array.isArray(value) ? value.join("; ") : typeof value === "object" ? JSON.stringify(value) : String(value);
      row.append(name, text);
      panel.appendChild(row);
    }

    function setOptions(select, rows, valueOf, labelOf) {
      select.replaceChildren(...rows.map(row => {
        const option = document.createElement("option");
        option.value = valueOf(row);
        option.textContent = labelOf(row);
        return option;
      }));
    }

    function renderSiteRows() {
      const body = $("siteRows");
      sitePageNumber = Math.min(sitePageNumber, Math.max(1, Math.ceil(siteSummaries.length / PAGE_SIZE)));
      renderPager("sites", sitePageNumber, siteSummaries.length);
      body.replaceChildren(...siteSummaries.slice((sitePageNumber - 1) * PAGE_SIZE, sitePageNumber * PAGE_SIZE).map(site => {
        const row = document.createElement("tr");
        for (const value of [site.siteName, site.region, site.status, site.normalAssets, site.warningAssets, site.criticalAssets, site.unreviewedEvents, site.lastReceivedAt || "-", `${site.onlineDevices}/${site.totalDevices}`]) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.appendChild(cell);
        }
        return row;
      }));
    }

    function renderEvents() {
      const list = $("events");
      list.replaceChildren(...events.map(event => {
        const button = document.createElement("button");
        button.className = event.id === selectedEventId ? "event selected" : "event";
        button.dataset.id = event.id;
        const title = document.createElement("b");
        title.textContent = `${event.id} - ${event.title}`;
        const meta = document.createElement("div");
        const pill = document.createElement("span");
        pill.className = `pill ${["warning", "critical", "device"].includes(event.severity) ? event.severity : ""}`;
        pill.textContent = event.label;
        meta.append(pill, ` ${formatLocalTime(event.occurredAt)} - ${event.score}`);
        button.append(title, document.createElement("br"), meta);
        return button;
      }));
      if (!events.length) list.textContent = "선택 조건에 맞는 이벤트가 없습니다.";
    }

    function clearEventSelection() {
      selectedEventId = null;
      detailGeneration += 1;
      noteListGeneration += 1;
      noteRows = [];
      selectedDetail = null;
      reviewDirty = false;
      reviewPage = 1;
      $("eventDetail").textContent = "Select an event.";
      $("labelSelect").value = "needs_review";
      $("noteInput").value = "";
      $("reviewReason").value = "";
      $("eventEvidence").textContent = "";
      $("reviewHistory").replaceChildren();
      $("eventNotes").replaceChildren();
      $("notesStatus").textContent = "";
      $("notesRefresh").disabled = true;
      $("reviewsMore").hidden = true;
      $("evidenceChart").getContext("2d").clearRect(0, 0, 900, 220);
      cancelNote();
      $("saveReview").disabled = true;
      for (const id of ["labelSelect","noteInput","reviewReason","memoText","noteCategory","attachmentRefs"]) $(id).disabled = true;
    }

    function formatLocalTime(timestamp) {
      const value = new Date(timestamp);
      if (Number.isNaN(value.getTime())) return "-";
      return new Intl.DateTimeFormat("ko-KR", {
        dateStyle: "short",
        timeStyle: "medium",
      }).format(value);
    }

    function finiteNumber(value) {
      if (!["number", "string"].includes(typeof value) || (typeof value === "string" && !value.trim())) return null;
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function chartTimeRange(points) {
      const firstAt = new Date(points[0]?.timestamp).getTime();
      const lastAt = new Date(points[points.length - 1]?.timestamp).getTime();
      return Number.isFinite(firstAt) && Number.isFinite(lastAt) && lastAt >= firstAt ? {firstAt, lastAt} : null;
    }

    function chartXForPoint(point, index, pointCount, width, pad, timeRange) {
      const timestamp = new Date(point.timestamp).getTime();
      if (timeRange && Number.isFinite(timestamp)) {
        const ratio = timeRange.firstAt === timeRange.lastAt ? 1 : (timestamp - timeRange.firstAt) / (timeRange.lastAt - timeRange.firstAt);
        return pad + (width - pad * 2) * ratio;
      }
      return pad + (width - pad * 2) * index / Math.max(1, pointCount - 1);
    }

    function chartPointIndexAtRatio(points, ratio) {
      const timeRange = chartTimeRange(points);
      if (!timeRange) return Math.round(ratio * (points.length - 1));
      const target = timeRange.firstAt + (timeRange.lastAt - timeRange.firstAt) * ratio;
      return points.reduce((best, point, index) => {
        const currentAt = new Date(point.timestamp).getTime();
        const bestAt = new Date(points[best].timestamp).getTime();
        return Math.abs(currentAt - target) < Math.abs(bestAt - target) ? index : best;
      }, 0);
    }

    function chartPlotRatio(clientX, bounds, canvasWidth) {
      const canvasX = (clientX - bounds.left) * canvasWidth / bounds.width;
      return Math.min(1, Math.max(0, (canvasX - CHART_PAD) / (canvasWidth - CHART_PAD * 2)));
    }

    function draw(points, units, chartEvents = [], canvasId = "chart", rule = currentRule) {
      const mainChart = canvasId === "chart";
      if (mainChart) {
        if (chartViewport) points = points.filter(point => {
          const at = new Date(point.timestamp).getTime();
          return at >= chartViewport.firstAt && at <= chartViewport.lastAt;
        });
        latestPoints = points;
        latestUnits = units;
      }
      const c = $(canvasId), ctx = c.getContext("2d"), w = c.width, h = c.height, pad = CHART_PAD;
      ctx.clearRect(0,0,w,h); ctx.fillStyle = "#fff"; ctx.fillRect(0,0,w,h);
      if (!points.length) {
        ctx.fillStyle = "#66716d"; ctx.font = "16px Segoe UI";
        ctx.fillText("No telemetry is available for the selected period.", pad, h / 2);
        if (mainChart) {$("chartHint").textContent = "No telemetry is available for the selected site, asset, and period."; $("chartRange").textContent = "";}
        return;
      }
      const timeRange = chartTimeRange(points);
      if (mainChart) $("chartRange").textContent = timeRange ? `${formatLocalTime(timeRange.firstAt)} — ${formatLocalTime(timeRange.lastAt)}` : "";
      ctx.strokeStyle = "#d8ded9"; ctx.lineWidth = 1;
      for (let i=0;i<=4;i++){ const y=pad+(h-pad*2)/4*i; ctx.beginPath(); ctx.moveTo(pad,y); ctx.lineTo(w-pad,y); ctx.stroke(); }
      const series = [
        ["anomaly score", "#c2413b", p => finiteNumber(p.anomalyScore ?? p.score), 0, 100],
        [units.vibrationRmsRaw ? "vibration raw RMS" : "demo vibration (mm/s RMS)", "#14796f", p => finiteNumber(p.vibrationRmsRaw ?? p.vibration), null, null],
        [units.acousticRmsRaw ? "acoustic raw RMS" : "demo acoustic (dB)", "#4453a8", p => finiteNumber(p.acousticRmsRaw ?? p.acoustic), null, null],
        ["RPM", "#b7791f", p => finiteNumber(p.rpm), null, null],
      ];
      let legendX = pad;
      for (const [name,color,map,fixedMin,fixedMax] of series) {
        const values = points.map(map).filter(value => value !== null);
        if (!values.length) continue;
        const min = fixedMin ?? Math.min(...values);
        const max = fixedMax ?? Math.max(...values);
        const range = max - min || 1;
        ctx.strokeStyle = color; ctx.lineWidth = name === "anomaly score" ? 3 : 2; ctx.beginPath();
        let started = false;
        points.forEach((p,i)=>{
          const value = map(p);
          if (value === null) { started = false; return; }
          const x = chartXForPoint(p, i, points.length, w, pad, timeRange);
          const y = pad + (h-pad*2)*(1-(value-min)/range);
          if (started) ctx.lineTo(x,y); else { ctx.moveTo(x,y); started = true; }
        });
        ctx.stroke();
        if (values.length === 1) {
          const pointIndex = points.findIndex(point => map(point) !== null);
          const value = map(points[pointIndex]);
          const x = chartXForPoint(points[pointIndex], pointIndex, points.length, w, pad, timeRange);
          const y = pad + (h-pad*2)*(1-(value-min)/range);
          ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
        }
        ctx.fillStyle = color; ctx.font = "12px Segoe UI"; ctx.fillText(name, legendX, 18); legendX += ctx.measureText(name).width + 16;
      }
      drawEventMarkers(ctx, points, chartEvents, w, h, pad, timeRange);
      if (rule?.active !== false) {
        const threshold = finiteNumber(rule?.scoreThreshold), hysteresis = finiteNumber(rule?.hysteresis);
        for (const [label, value] of [["진입", threshold], ["종료", threshold !== null && hysteresis !== null ? threshold - hysteresis : null]]) {
          if (value === null) continue;
          const y = pad + (h - pad * 2) * (1 - value / 100);
          ctx.strokeStyle = "#b7791f"; ctx.setLineDash([6, 3]);
          ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(w - pad, y); ctx.stroke(); ctx.setLineDash([]);
          ctx.fillStyle = "#8b5e13"; ctx.fillText(`${label} ${value} · ${rule.durationSec}s`, pad + 4, y - 4);
        }
      }
      if (mainChart && chartSelectionAt !== null && timeRange && chartSelectionAt >= timeRange.firstAt && chartSelectionAt <= timeRange.lastAt) {
        const x = chartXForPoint({timestamp:chartSelectionAt}, 0, 1, w, pad, timeRange);
        ctx.strokeStyle = "#17211f"; ctx.beginPath(); ctx.moveTo(x, pad); ctx.lineTo(x, h - pad); ctx.stroke();
      }
      if (mainChart) $("chartHint").textContent = "Signals are independently scaled for comparison. Hover for raw values and score evidence.";
    }

    function redrawChart() { draw(allPoints, latestUnits, chartEvents); }
    function zoomChart(factor, direction = 0) {
      const full = chartTimeRange(allPoints);
      if (!full || full.firstAt === full.lastAt) return;
      const current = chartViewport || full;
      const span = Math.min(full.lastAt - full.firstAt, Math.max(1, (current.lastAt - current.firstAt) * factor));
      const center = direction ? (current.firstAt + current.lastAt) / 2 + direction * span / 2 : (chartSelectionAt ?? (current.firstAt + current.lastAt) / 2);
      const firstAt = Math.max(full.firstAt, Math.min(full.lastAt - span, center - span / 2));
      chartViewport = {firstAt, lastAt:firstAt + span};
      redrawChart();
    }

    function drawEventMarkers(ctx, points, chartEvents, width, height, pad, timeRange) {
      if (!timeRange) return;
      for (const event of chartEvents) {
        const occurredAt = new Date(event.occurredAt).getTime();
        const duration = finiteNumber(event.durationSec) ?? 0;
        const endAt = event.endAt ? new Date(event.endAt).getTime() : event.status === "open" ? timeRange.lastAt : occurredAt + duration * 1000;
        if (!Number.isFinite(occurredAt) || !Number.isFinite(endAt) || endAt < timeRange.firstAt || occurredAt > timeRange.lastAt) continue;
        const span = timeRange.lastAt - timeRange.firstAt || 1;
        const startX = pad + (width - pad * 2) * (Math.max(occurredAt, timeRange.firstAt) - timeRange.firstAt) / span;
        const endX = pad + (width - pad * 2) * (Math.min(endAt, timeRange.lastAt) - timeRange.firstAt) / span;
        ctx.fillStyle = event.id === selectedEventId ? "rgba(20,121,111,.18)" : "rgba(194,65,59,.08)";
        ctx.fillRect(startX, pad, Math.max(1, endX - startX), height - pad * 2);
        if (occurredAt < timeRange.firstAt) continue;
        const ratio = timeRange.firstAt === timeRange.lastAt ? 1 : (occurredAt - timeRange.firstAt) / (timeRange.lastAt - timeRange.firstAt);
        const x = pad + (width - pad * 2) * ratio;
        ctx.strokeStyle = "#c2413b"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(x, pad); ctx.lineTo(x, height - pad); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = "#c2413b"; ctx.beginPath(); ctx.arc(x, pad + 8, 4, 0, Math.PI * 2); ctx.fill();
      }
    }

    async function selectEvent(eventId, force = false) {
      if (!force && (reviewDirty || memoDirty) && !confirm("저장하지 않은 편집을 버리고 이벤트를 변경할까요?")) return;
      clearEventSelection();
      selectedEventId = eventId;
      const generation = detailGeneration;
      const notesGeneration = ++noteListGeneration;
      $("eventDetail").textContent = "이벤트 상세를 불러오는 중입니다.";
      renderEvents();
      const [response, reviews, notes] = await Promise.all([
        api(`/api/anomaly/events/${encodeURIComponent(eventId)}`),
        api(`/api/events/${encodeURIComponent(eventId)}/reviews?page=1&size=20`),
        api(`/api/events/${encodeURIComponent(eventId)}/notes`),
      ]);
      if (generation !== detailGeneration || selectedEventId !== eventId) return;
      selectedDetail = response;
      const event = response.event;
      const detail = $("eventDetail");
      detail.replaceChildren();
      const title = document.createElement("b");
      title.textContent = event.title;
      detail.append(title, document.createElement("br"), `${formatLocalTime(event.occurredAt)} · ${event.duration ?? "-"} · 점수 ${event.maxScore ?? event.score ?? "-"}`, document.createElement("br"), event.note || "");
      const missing = document.createElement("p");
      missing.className = "notice";
      missing.textContent = response.context.rawDataMissing ? "원본 신호 없음: 보관기간 만료 또는 미수신. 보존된 특징·버전만 표시합니다." : `전후 신호 ${response.context.points.length}건 · 출처 ${response.context.source}`;
      detail.appendChild(missing);
      $("eventEvidence").textContent = JSON.stringify({context:{from:response.context.from, to:response.context.to, source:response.context.source}, featureSnapshot:response.featureSnapshot, appliedRule:response.appliedRule, modelVersion:response.modelVersion, deviceSnapshot:response.deviceSnapshot}, null, 2);
      draw(response.context.points, response.context.units, [event], "evidenceChart", response.appliedRule);
      $("labelSelect").value = event.label;
      $("noteInput").value = event.note || "";
      $("saveReview").disabled = reviewSaving || !can("event:review");
      $("saveNote").disabled = noteSaving || !can("event:review");
      for (const id of ["labelSelect","noteInput","reviewReason","memoText","noteCategory","attachmentRefs"]) $(id).disabled = !can("event:review");
      renderReviewHistory(reviews);
      if (notesGeneration === noteListGeneration) {
        renderNotes(notes);
        $("notesRefresh").disabled = false;
      }
      redrawChart();
    }
    function renderReviewHistory(page, append = false) {
      if (!append) $("reviewHistory").replaceChildren();
      for (const item of page.items) {
        const row = document.createElement("pre");
        row.textContent = JSON.stringify(item, null, 2);
        $("reviewHistory").appendChild(row);
      }
      if (!page.total) $("reviewHistory").textContent = "검수 변경 이력이 없습니다.";
      $("reviewsMore").hidden = page.page * page.size >= page.total;
    }
    function cancelNote() {
      editingNoteId = null; memoDirty = false;
      noteHistoryGeneration++;
      $("memoText").value = ""; $("attachmentRefs").value = "";
      $("noteHistory").replaceChildren();
      $("saveNote").textContent = "메모 등록";
      $("saveNote").disabled = noteSaving || !selectedEventId || !can("event:review");
    }
    function renderNotes(response) {
      noteRows = response.items;
      const panel = $("eventNotes"); panel.replaceChildren();
      if (!noteRows.length) panel.textContent = "등록된 개별 메모가 없습니다.";
      for (const note of noteRows) {
        const row = document.createElement("section");
        const text = document.createElement("p");
        text.textContent = `${response.latestByCategory[note.category]?.id === note.id ? "[최신] " : ""}${note.category} · ${note.author?.name || ""} · ${formatLocalTime(note.updatedAt)}\n${note.text}`;
        row.appendChild(text);
        for (const ref of note.attachmentRefs) {
          const reference = document.createElement("p"); reference.textContent = ref; row.appendChild(reference);
        }
        for (const action of ["history", ...(can("event:review") ? ["edit", "delete"] : [])]) {
          const button = document.createElement("button");
          button.className = "secondary";
          button.textContent = {history:"변경 이력", edit:"수정", delete:"삭제"}[action];
          button.addEventListener("click", () => act(() => noteAction(note.id, action)));
          row.appendChild(button);
        }
        panel.appendChild(row);
      }
    }
    async function refreshNotes(eventId = selectedEventId, generation = detailGeneration, completedMessage = "") {
      if (!eventId || generation !== detailGeneration || eventId !== selectedEventId) return;
      const requestGeneration = ++noteListGeneration;
      const current = () => generation === detailGeneration && eventId === selectedEventId && requestGeneration === noteListGeneration;
      // A list refresh is a read-only recovery step, never a retry of the write.
      noteRows = []; $("eventNotes").replaceChildren();
      $("notesRefresh").disabled = true;
      $("notesStatus").className = "notice";
      $("notesStatus").textContent = completedMessage + "메모 목록을 불러오는 중입니다.";
      try {
        const notes = await api(`/api/events/${encodeURIComponent(eventId)}/notes`);
        if (!current()) return;
        renderNotes(notes);
        $("notesStatus").textContent = completedMessage;
      } catch (error) {
        if (!current()) return;
        $("notesStatus").className = "notice error";
        $("notesStatus").textContent = completedMessage + `목록 새로고침 실패: ${error.message}. 메모 목록 새로고침으로 다시 조회하세요.`;
      } finally {
        if (current()) $("notesRefresh").disabled = false;
      }
    }
    async function noteAction(noteId, action) {
      const eventId = selectedEventId, generation = detailGeneration;
      const note = noteRows.find(item => item.id === noteId);
      if (!note || !eventId) return;
      if (action === "edit") {
        if (memoDirty && !confirm("편집 중인 메모를 버릴까요?")) return;
        noteHistoryGeneration++; $("noteHistory").replaceChildren();
        editingNoteId = noteId;
        $("noteCategory").value = note.category; $("memoText").value = note.text;
        $("attachmentRefs").value = note.attachmentRefs.join("\n");
        $("saveNote").textContent = "메모 수정 저장"; memoDirty = false;
      } else if (action === "history") {
        const historyGeneration = ++noteHistoryGeneration;
        const history = await api(`/api/events/${encodeURIComponent(eventId)}/notes/${encodeURIComponent(noteId)}/history`);
        if (generation !== detailGeneration || historyGeneration !== noteHistoryGeneration) return;
        $("noteHistory").textContent = JSON.stringify(history, null, 2);
      } else if (action === "delete" && can("event:review") && confirm("이 메모를 삭제할까요? 변경 이력은 보존됩니다.")) {
        await api(`/api/events/${encodeURIComponent(eventId)}/notes/${encodeURIComponent(noteId)}`, {method:"DELETE"});
        if (generation !== detailGeneration) return;
        if (editingNoteId === noteId) cancelNote();
        await refreshNotes(eventId, generation, "메모 삭제 완료. ");
      }
    }

    const field = (key, label, type = "text", required = false, initial = "") => ({key, label, type, required, initial});
    const MANAGEMENT = {
      site: {label:"사이트", permission:"site", create:true, remove:true,
        list:q => "/api/sites", path:(q,r) => `/api/sites${r ? "/" + encodeURIComponent(r.id) : ""}`,
        fields:[field("id","사이트 ID","identity",true), field("name","사이트명","text",true), field("code","사이트 코드"), field("region","지역"), field("address","주소"), field("timezone","시간대","text",false,"Asia/Seoul"), field("operationStatus","운영 상태",["active","inactive"])]},
      asset: {label:"설비", permission:"asset", create:true, remove:true,
        list:q => `/api/sites/${encodeURIComponent(q.siteId)}/assets`, path:(q,r) => `/api/sites/${encodeURIComponent(q.siteId)}/assets${r ? "/" + encodeURIComponent(r.id) : ""}`,
        fields:[field("assetCode","설비 코드","text",true), field("name","설비명","text",true), field("type","설비 종류","text",false,"motor"), field("ratedRpm","정격 RPM","number",true), field("installLocation","설치 위치"), field("operationStatus","운영 상태",["active","inactive"]), field("baseline.status","기준선 상태",["draft","ready"]), field("baseline.capturedAt","기준선 측정 UTC 시각 (RFC3339)"), field("baseline.vibrationRmsMmS","보정 기준선 진동 (mm/s RMS)","number"), field("baseline.acousticDb","보정 기준선 음향 (dB)","number"), field("baseline.sampleCount","기준선 표본 수","number")]},
      device: {label:"장치", permission:"device", create:true, remove:true,
        list:q => `/api/sites/${encodeURIComponent(q.siteId)}/devices`, path:(q,r) => r ? `/api/devices/${encodeURIComponent(r.id)}` : `/api/sites/${encodeURIComponent(q.siteId)}/devices`,
        fields:[field("id","장치 ID","identity",true), field("assetId","연결 설비 ID","text",true), field("mappingStatus","매핑 상태",["inactive","active"]), field("firmwareVersion","펌웨어 버전"), field("sensorChannels","센서 채널 (한 줄에 하나)","list"), field("certificateId","인증서 ID","text",true), field("certificateFingerprint","인증서 지문","text",true), field("certificateStatus","인증서 상태","text",false,"registered")]},
      install: {label:"설치정보", permission:"install-point", create:true,
        list:q => `/api/assets/${encodeURIComponent(q.assetId)}/install-points`, path:(q,r) => `/api/assets/${encodeURIComponent(q.assetId)}/install-points${r ? "/" + encodeURIComponent(r.id) : ""}`,
        fields:[field("position","설치 위치","text",true), field("orientation","센서 방향","text",true), field("mountingMethod","고정 방법","text",true), field("acousticDirection","음향 방향"), field("ambientNoiseSources","주변 소음원 (한 줄에 하나)","list"), field("photoRefs","설치 사진 참조 (한 줄에 하나)","list",true), field("active","활성 상태","boolean",false,true)]},
      network: {label:"사이트 통신망", permission:"network-profile", method:"PUT", singleton:true,
        list:q => `/api/sites/${encodeURIComponent(q.siteId)}/network-profile`, path:q => `/api/sites/${encodeURIComponent(q.siteId)}/network-profile`,
        fields:[field("networkProfileId","통신 프로파일 ID","text",true)]},
      rollout: {label:"장치 도입 대상", permission:"rollout", method:"PUT", singleton:true, replace:true,
        list:q => `/api/sites/${encodeURIComponent(q.siteId)}/rollout-plan`, path:q => `/api/sites/${encodeURIComponent(q.siteId)}/rollout-plan`,
        fields:[field("networkProfileId","통신 프로파일 ID","text",true), field("targetAssetIds","대상 설비 ID (한 줄에 하나)","list",true), field("installPriority","설치 우선순위","text",true), field("note","도입 메모")]},
      rule: {label:"설비 이상 규칙", permission:"anomaly-rule", method:"PUT", singleton:true,
        list:q => `/api/anomaly/rules/${encodeURIComponent(q.assetId)}`, path:q => `/api/anomaly/rules/${encodeURIComponent(q.assetId)}`,
        fields:[field("scoreThreshold","진입 점수","number",true), field("durationSec","지속시간 (초)","number",true), field("hysteresis","히스테리시스","number",true), field("mergeWindowSec","병합 간격 (초)","number",true), field("active","활성 상태","boolean")]},
      policy: {label:"알림 정책", permission:"alert-policy", method:"PUT",
        list:q => "/api/alerts/policies", path:(q,r) => `/api/alerts/policies/${encodeURIComponent(r.id)}`,
        fields:[field("severity","심각도",["warning","critical"]), field("siteIds","사이트 ID (한 줄에 하나)","list"), field("assetIds","설비 ID (한 줄에 하나)","list"), field("channels","채널: web / email / webhook (한 줄에 하나)","list",true), field("recipients","수신자 (한 줄에 하나)","list",true), field("workHours.start","업무 시작","time",true), field("workHours.end","업무 종료","time",true), field("cooldownSec","재발송 대기 (초)","number",true), field("enabled","활성 상태","boolean")]},
      parameter: {label:"운영 파라미터", permission:"parameter", method:"PUT",
        list:q => "/api/parameters", path:(q,r) => `/api/parameters/${encodeURIComponent(r.key)}`, fields:[]},
    };
    function setupManagement() {
      const kinds = Object.entries(MANAGEMENT).filter(([key, config]) => can(config.permission + ":read"));
      setOptions($("managementKind"), kinds, row => row[0], row => row[1].label);
      $("managementPanel").hidden = !kinds.length && !can("audit-log:read");
      $("auditPanel").hidden = !can("audit-log:read");
    }
    function invalidateManagement() {
      managementGeneration++; auditGeneration++;
      managementRows = []; managementRow = null; managementScope = null; managementDirty = false;
      $("managementFields").replaceChildren(); $("managementRecord").replaceChildren();
      $("managementDetail").textContent = ""; $("auditRows").replaceChildren();
      $("managementSave").hidden = true; $("managementDelete").hidden = true; $("managementNew").hidden = true;
      $("managementStatus").textContent = "선택한 사이트·설비의 목록을 불러오세요.";
    }
    async function loadManagement() {
      if (managementDirty && !confirm("저장하지 않은 관리 항목 편집을 버릴까요?")) return;
      const kind = $("managementKind").value, config = MANAGEMENT[kind];
      const query = {siteId:$("siteSelect").value,assetId:$("assetSelect").value};
      if (!config || !can(config.permission + ":read")) return;
      invalidateManagement();
      if (["asset","device","network","rollout"].includes(kind) && !query.siteId) throw new Error("먼저 사이트를 선택하거나 등록하세요.");
      if (["install","rule"].includes(kind) && !query.assetId) throw new Error("먼저 설비를 선택하거나 등록하세요.");
      const generation = managementGeneration;
      $("managementStatus").textContent = "목록을 불러오는 중입니다.";
      const result = await api(config.list(query));
      if (generation !== managementGeneration) return;
      managementScope = {kind, query};
      managementRows = config.singleton ? [result] : result;
      setOptions($("managementRecord"), managementRows, row => row.id || row.key || query.assetId, row => row.name || row.key || row.id || config.label);
      $("managementNew").hidden = !config.create || !can(config.permission + ":write");
      editManagement(false);
    }
    function editManagement(creating) {
      if (!managementScope) return;
      managementGeneration++;
      const config = MANAGEMENT[managementScope.kind];
      managementCreating = creating;
      managementRow = creating ? null : managementRows.find(row => (row.id || row.key || managementScope.query.assetId) === $("managementRecord").value);
      const panel = $("managementFields"); panel.replaceChildren(); managementInputs = [];
      $("managementDetail").textContent = managementRow ? JSON.stringify(managementRow, null, 2) : "";
      const writable = can(config.permission + ":write") && (creating || Boolean(managementRow)) && managementRow?.mutable !== false;
      let fields = config.fields;
      if (managementScope.kind === "parameter" && managementRow) fields = [field("value",managementRow.key,managementRow.type === "boolean" ? "boolean" : "number",true)];
      for (const definition of fields) {
        if (!creating && definition.type === "identity") continue;
        // The existing create-install API always creates an active installation.
        if (creating && managementScope.kind === "install" && definition.key === "active") continue;
        const original = definition.key.split(".").reduce((obj,key) => obj?.[key], managementRow) ?? definition.initial;
        const choices = Array.isArray(definition.type) ? definition.type : definition.type === "boolean" ? ["false","true"] : null;
        const input = document.createElement(choices ? "select" : definition.type === "list" ? "textarea" : "input");
        if (choices) setOptions(input, choices, item => item, item => item);
        else input.type = ["number","time"].includes(definition.type) ? definition.type : "text";
        if (definition.type === "number") input.step = managementRow?.type === "integer" ? "1" : "any";
        input.value = definition.type === "list" ? (Array.isArray(original) ? original.join("\n") : "") : String(original);
        if (choices && !choices.includes(input.value)) input.value = choices[0];
        input.required = definition.required; input.disabled = !writable;
        input.addEventListener("input", () => {managementDirty = true;});
        const label = document.createElement("label"); label.textContent = definition.label; label.appendChild(input); panel.appendChild(label);
        managementInputs.push({definition,input,original:input.value});
      }
      managementDirty = false;
      $("managementReason").value = ""; $("managementReason").disabled = !writable;
      $("managementSave").hidden = !writable; $("managementDelete").hidden = !writable || creating || !config.remove;
      $("managementStatus").textContent = managementRow?.mutable === false ? `읽기 전용: ${managementRow.managedBy || "장치"}에서 관리하는 설정입니다.` : creating ? "신규 등록" : !managementRow ? "등록된 항목이 없습니다." : writable ? "변경 사항을 입력하고 사유와 함께 저장하세요." : "조회 전용 권한입니다.";
    }
    function managementPayload() {
      const payload = {};
      for (const {definition,input,original} of managementInputs) {
        if (!managementCreating && input.value === original && !MANAGEMENT[managementScope.kind].replace) continue;
        if (managementCreating && !input.value.trim() && !definition.required) continue;
        let value = input.value;
        if (definition.type === "number") {
          value = finiteNumber(value);
          if (value === null) throw new Error(`${definition.label}: 숫자를 입력하세요.`);
        } else if (definition.type === "boolean") value = value === "true";
        else if (definition.type === "list") value = value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
        const [parent, child] = definition.key.split(".");
        if (child) payload[parent] = {...managementRow?.[parent], ...payload[parent], [child]:value};
        else payload[parent] = value;
      }
      return payload;
    }
    async function saveManagement() {
      if (!managementScope || managementSaving) return;
      const {kind,query} = managementScope, config = MANAGEMENT[kind];
      if (!can(config.permission + ":write") || managementRow?.mutable === false) return;
      const payload = managementPayload();
      if (!Object.keys(payload).length) {$("managementStatus").textContent = "변경 사항이 없습니다."; return;}
      const reason = $("managementReason").value.trim();
      if (!reason) throw new Error("변경 사유를 입력하세요.");
      payload.reason = reason;
      const generation = managementGeneration;
      const draft = JSON.stringify(managementInputs.map(({input}) => input.value));
      managementSaving = true;
      $("managementSave").disabled = true;
      try {
        await api(config.path(query, managementRow), jsonOptions(managementCreating ? "POST" : config.method || "PATCH", payload));
        if (generation !== managementGeneration) return;
        if (JSON.stringify(managementInputs.map(({input}) => input.value)) !== draft || $("managementReason").value.trim() !== reason) {
          statusMessage("이전 입력을 저장했습니다. 이어서 편집한 내용은 아직 저장되지 않았습니다."); return;
        }
        managementDirty = false;
        if (kind === "site" || kind === "asset") {
          await refreshMasterSelection();
          if (generation !== managementGeneration) {await render(); return;}
        }
        await loadManagement(); await render(); statusMessage("운영 설정을 저장했습니다.");
      } finally {managementSaving = false; $("managementSave").disabled = false;}
    }
    async function refreshMasterSelection() {
      const generation = managementGeneration, selectedSiteId = $("siteSelect").value, selectedAssetId = $("assetSelect").value;
      const rows = await api("/api/sites");
      if (generation !== managementGeneration) return;
      sites = rows;
      setOptions($("siteSelect"), sites, item => item.id, item => item.name);
      if (sites.some(item => item.id === selectedSiteId)) $("siteSelect").value = selectedSiteId;
      if (sites.length) {
        const assets = await api(`/api/sites/${encodeURIComponent(selectedSite().id)}/assets`);
        if (generation !== managementGeneration) return;
        setOptions($("assetSelect"), assets, item => item.id, item => item.name);
        if (assets.some(item => item.id === selectedAssetId)) $("assetSelect").value = selectedAssetId;
      } else $("assetSelect").replaceChildren();
      $("assetSelect").disabled = !$("assetSelect").value;
      if ($("siteSelect").value !== selectedSiteId || $("assetSelect").value !== selectedAssetId) invalidateScope();
      rememberSelection();
    }
    async function deleteManagement() {
      if (!managementScope || !managementRow) return;
      const {kind,query} = managementScope, config = MANAGEMENT[kind];
      if (!config.remove || !can(config.permission + ":write") || !confirm(`${managementRow.id} 항목을 삭제할까요? 연결된 이력이 있으면 서버에서 거절됩니다.`)) return;
      const generation = managementGeneration;
      await api(config.path(query, managementRow), {method:"DELETE"});
      if (generation !== managementGeneration) return;
      managementDirty = false;
      if (kind === "site" || kind === "asset") await refreshMasterSelection();
      await loadManagement();
      await render(); statusMessage("삭제했습니다. 선택 목록을 갱신했습니다.");
    }
    async function loadAudit() {
      if (!can("audit-log:read")) return;
      const generation = ++auditGeneration;
      const query = new URLSearchParams({page:auditPageNumber,size:PAGE_SIZE,action:$("auditAction").value,targetType:$("auditTarget").value,actorId:$("auditActor").value});
      const result = await api(`/api/audit-logs?${query}`);
      if (generation !== auditGeneration) return;
      $("auditRows").textContent = result.items.length ? JSON.stringify(result.items, null, 2) : "감사 기록이 없습니다.";
      renderPager("audit", result.page, result.total);
    }

    let acceptedSelection = {};
    const SELECTION_CONTROLS = ["siteSelect","assetSelect","periodSelect","severityFilter","eventLabelFilter","reviewedFilter","eventSort"];
    function rememberSelection() { acceptedSelection = Object.fromEntries(SELECTION_CONTROLS.map(id => [id,$(id).value])); }
    function allowSelectionChange() {
      if (!(reviewDirty || memoDirty || managementDirty) || confirm("저장하지 않은 편집을 버리고 조회 조건을 변경할까요?")) return true;
      for (const [id,value] of Object.entries(acceptedSelection)) $(id).value = value;
      return false;
    }
    async function reloadEventSelection() {
      // Controls already show the new filter/page; old rows must not remain selectable.
      events = []; eventTotal = 0; renderEvents();
      $("events").textContent = "이벤트 목록을 불러오는 중입니다.";
      $("eventsPage").textContent = "조회 중";
      $("eventsPrev").disabled = true; $("eventsNext").disabled = true;
      const requestGeneration = renderGeneration + 1;
      try {await render();}
      catch (error) {
        if (requestGeneration !== renderGeneration) return;
        $("events").textContent = "이벤트 목록 조회에 실패했습니다. 새로고침으로 다시 조회하세요.";
        $("eventsPage").textContent = "조회 실패";
        throw error;
      }
    }
    function invalidateScope(resetWindow = false) {
      clearEventSelection();
      renderGeneration += 1;
      lastQuery = null; $("exportBtn").disabled = true;
      eventPageNumber = 1;
      chartViewport = null; chartSelectionAt = null;
      allPoints = []; chartEvents = []; currentRule = null; redrawChart();
      events = []; eventTotal = 0; renderEvents(); renderPager("events", 1, 0);
      $("chartTitle").textContent = "선택 조건을 불러오는 중입니다.";
      $("deviceHealth").replaceChildren(); $("serviceHealth").replaceChildren();
      renderNotifications([]); renderModelResult([]);
      allPoints = []; chartEvents = []; latestUnits = {}; currentRule = null;
      events = []; renderEvents(); redrawChart();
      if (resetWindow) {activeWindow = null; $("fromInput").value = ""; $("toInput").value = "";}
      invalidateManagement();
    }
    document.addEventListener("click", (event) => {
      const row = event.target.closest(".event[data-id]");
      if (row) act(() => selectEvent(row.dataset.id));
    });
    $("loginBtn").addEventListener("click", () => login().then(rememberSelection).catch(error => alert(error.message)));
    $("siteSelect").addEventListener("change", async () => {
      if (!allowSelectionChange()) return;
      invalidateScope(true);
      $("assetSelect").replaceChildren(); $("assetSelect").disabled = true;
      const assetRequestGeneration = ++assetGeneration;
      renderGeneration += 1;
      await act(async () => {
        if (!await renderAssets(assetRequestGeneration)) return;
        rememberSelection(); await render();
      });
    });
    $("assetSelect").addEventListener("change", async () => {
      if (!allowSelectionChange()) return;
      invalidateScope(); rememberSelection(); await act(render);
    });
    $("periodSelect").addEventListener("change", async () => {
      if (!allowSelectionChange()) return;
      invalidateScope(true); rememberSelection(); await act(render);
    });
    $("refreshBtn").addEventListener("click", () => act(async () => {
      if (!$("fromInput").value && !chartViewport) activeWindow = null;
      await render();
    }));
    $("applyRange").addEventListener("click", () => act(async () => {
      const from = new Date($("fromInput").value), to = new Date($("toInput").value);
      if (!Number.isFinite(from.getTime()) || !Number.isFinite(to.getTime()) || from > to) throw new Error("시작·종료 시각을 올바르게 입력하세요.");
      if (!allowSelectionChange()) return;
      invalidateScope(); activeWindow = {from:from.toISOString(),to:to.toISOString()};
      await render();
    }));
    for (const id of ["severityFilter","eventLabelFilter","reviewedFilter","eventSort"]) $(id).addEventListener("change", () => act(async () => {
      if (!allowSelectionChange()) return;
      clearEventSelection(); eventPageNumber = 1; rememberSelection(); await reloadEventSelection();
    }));
    for (const [id,direction] of [["eventsPrev",-1],["eventsNext",1]]) $(id).addEventListener("click", () => act(async () => {
      if (!allowSelectionChange()) return;
      eventPageNumber = Math.max(1, Math.min(Math.ceil(eventTotal / PAGE_SIZE), eventPageNumber + direction));
      clearEventSelection(); await reloadEventSelection();
    }));
    for (const [id,direction] of [["sitesPrev",-1],["sitesNext",1]]) $(id).addEventListener("click", () => {sitePageNumber = Math.max(1,sitePageNumber + direction); renderSiteRows();});
    for (const id of ["labelSelect","noteInput","reviewReason"]) $(id).addEventListener("input", () => {reviewDirty = true;});
    for (const id of ["memoText","noteCategory","attachmentRefs"]) $(id).addEventListener("input", () => {memoDirty = true;});
    $("injectBtn").addEventListener("click", () => act(async () => {
      await api("/api/demo/inject-anomaly", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({siteId:$("siteSelect").value, assetId:$("assetSelect").value})});
      if (!$("fromInput").value) activeWindow = null;
      await render();
    }));
    $("saveReview").addEventListener("click", () => act(async () => {
      if (!selectedEventId || !selectedDetail || !can("event:review") || reviewSaving) return;
      if (memoDirty) throw new Error("먼저 편집 중인 개별 메모를 저장하거나 취소하세요.");
      const eventId = selectedEventId, generation = detailGeneration, reason = $("reviewReason").value.trim();
      if (!reason) throw new Error("검수 변경 사유를 입력하세요.");
      const payload = {label:$("labelSelect").value,note:$("noteInput").value,reason};
      reviewSaving = true;
      $("saveReview").disabled = true;
      try {
        await api(`/api/events/${encodeURIComponent(eventId)}/review`, jsonOptions("POST", payload));
        if (generation !== detailGeneration) return;
        if (memoDirty || noteSaving || $("labelSelect").value !== payload.label || $("noteInput").value !== payload.note || $("reviewReason").value.trim() !== reason) {
          statusMessage("이전 검수 입력을 저장했습니다. 이어서 편집한 내용은 아직 저장되지 않았습니다."); return;
        }
        reviewDirty = false; await selectEvent(eventId, true); await render(); statusMessage("검수 내용을 저장했습니다.");
      } finally {reviewSaving = false; $("saveReview").disabled = !selectedDetail || !can("event:review");}
    }));
    $("saveNote").addEventListener("click", () => act(async () => {
      if (!selectedEventId || !selectedDetail || !can("event:review") || noteSaving) return;
      const text = $("memoText").value.trim();
      if (!text) throw new Error("개별 메모 내용을 입력하세요.");
      const eventId = selectedEventId, generation = detailGeneration, noteId = editingNoteId;
      const payload = {category:$("noteCategory").value, text, attachmentRefs:$("attachmentRefs").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean)};
      const draft = [$("memoText").value,$("noteCategory").value,$("attachmentRefs").value];
      noteSaving = true;
      $("saveNote").disabled = true;
      try {
        await api(`/api/events/${encodeURIComponent(eventId)}/notes${noteId ? "/" + encodeURIComponent(noteId) : ""}`, jsonOptions(noteId ? "PATCH" : "POST", payload));
        if (generation !== detailGeneration) return;
        // Confirm the write before GET: a failed refresh must not resubmit this draft.
        const unchanged = editingNoteId === noteId && JSON.stringify(draft) === JSON.stringify([$("memoText").value,$("noteCategory").value,$("attachmentRefs").value]);
        if (unchanged) cancelNote();
        statusMessage(unchanged ? "메모를 저장했습니다." : "이전 메모 입력을 저장했습니다. 이어서 편집한 내용은 아직 저장되지 않았습니다.");
        await refreshNotes(eventId, generation, "메모 저장 완료. ");
      } finally {noteSaving = false; $("saveNote").disabled = !selectedDetail || !can("event:review");}
    }));
    $("cancelNote").addEventListener("click", () => {if (!memoDirty || confirm("메모 편집을 취소할까요?")) cancelNote();});
    $("notesRefresh").addEventListener("click", () => act(() => refreshNotes()));
    $("reviewsMore").addEventListener("click", () => act(async () => {
      const eventId = selectedEventId, generation = detailGeneration, page = reviewPage + 1;
      $("reviewsMore").disabled = true;
      try {
        const response = await api(`/api/events/${encodeURIComponent(eventId)}/reviews?page=${page}&size=20`);
        if (generation !== detailGeneration) return;
        reviewPage = page; renderReviewHistory(response, true);
      } finally {$("reviewsMore").disabled = false;}
    }));
    function exportQuery() {
      if (!lastQuery || lastQuery.siteId !== $("siteSelect").value || lastQuery.assetId !== $("assetSelect").value) throw new Error("현재 선택 조건을 먼저 조회하세요.");
      const query = {...lastQuery, format:$("exportFormat").value || "csv"};
      if (chartViewport) {query.from = new Date(chartViewport.firstAt).toISOString(); query.to = new Date(chartViewport.lastAt).toISOString();}
      if ($("datasetSelect").value) query.datasetId = $("datasetSelect").value;
      return query;
    }
    $("exportBtn").addEventListener("click", () => act(async () => {
      if (!can("export:read")) return;
      const query = exportQuery();
      const response = await fetch(`/api/datasets/export?${scopeParams(query)}`, {headers:{authorization:`Bearer ${token}`}});
      if (!response.ok) {
        const error = await response.json(); throw new Error(error.error?.message || `Export failed: ${response.status}`);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a"); link.href = url;
      link.download = `${query.siteId}_${query.assetId}.${query.format}`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }));
    $("zoomIn").addEventListener("click", () => zoomChart(0.5));
    $("panLeft").addEventListener("click", () => zoomChart(1, -1));
    $("panRight").addEventListener("click", () => zoomChart(1, 1));
    $("zoomReset").addEventListener("click", () => {chartViewport = null; chartSelectionAt = null; redrawChart();});
    $("chart").addEventListener("click", event => {
      if (!latestPoints.length) return;
      const chart = $("chart"), ratio = chartPlotRatio(event.clientX, chart.getBoundingClientRect(), chart.width);
      chartSelectionAt = new Date(latestPoints[chartPointIndexAtRatio(latestPoints, ratio)].timestamp).getTime(); redrawChart();
    });
    $("managementLoad").addEventListener("click", () => act(loadManagement));
    $("managementKind").addEventListener("change", () => act(async () => {
      if (managementDirty && !confirm("저장하지 않은 관리 항목 편집을 버릴까요?")) {$("managementKind").value = managementScope.kind; return;}
      managementDirty = false; await loadManagement();
    }));
    $("managementRecord").addEventListener("change", () => {
      if (managementDirty && !confirm("저장하지 않은 편집을 버릴까요?")) {$("managementRecord").value = managementRow?.id || managementRow?.key || ""; return;}
      editManagement(false);
    });
    $("managementNew").addEventListener("click", () => {if (!managementDirty || confirm("편집을 버리고 신규 등록할까요?")) editManagement(true);});
    $("managementForm").addEventListener("submit", event => {event.preventDefault(); act(saveManagement);});
    $("managementReason").addEventListener("input", () => {managementDirty = true;});
    $("managementDelete").addEventListener("click", () => act(deleteManagement));
    $("auditLoad").addEventListener("click", () => {auditPageNumber = 1; act(loadAudit);});
    $("auditPrev").addEventListener("click", () => {auditPageNumber = Math.max(1,auditPageNumber - 1); act(loadAudit);});
    $("auditNext").addEventListener("click", () => {auditPageNumber++; act(loadAudit);});
    $("chart").addEventListener("mousemove", (event) => {
      if (!latestPoints.length) return;
      const chart = $("chart");
      const bounds = chart.getBoundingClientRect();
      const ratio = chartPlotRatio(event.clientX, bounds, chart.width);
      const index = chartPointIndexAtRatio(latestPoints, ratio);
      const point = latestPoints[index];
      const vibration = finiteNumber(point.vibrationRmsRaw ?? point.vibration);
      const acoustic = finiteNumber(point.acousticRmsRaw ?? point.acoustic);
      const score = finiteNumber(point.anomalyScore ?? point.score);
      const vibrationLabel = latestUnits.vibrationRmsRaw ? "vibration raw RMS" : "demo vibration (mm/s RMS)";
      const acousticLabel = latestUnits.acousticRmsRaw ? "acoustic raw RMS" : "demo acoustic (dB)";
      $("chartHint").textContent = `${point.timestamp} · ${vibrationLabel}: ${vibration ?? "-"} · ${acousticLabel}: ${acoustic ?? "-"} · RPM: ${point.rpm ?? "-"} · score: ${score ?? "unavailable"} (${point.anomalyStatus ?? "-"})`;
    });
    setInterval(() => {
      if (!token || !selectedSite() || !$("assetSelect").value) return;
      if (!$("fromInput").value && !chartViewport) activeWindow = null;
      act(render);
    }, 5000);
  </script>
</body>
</html>"""
