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
    textarea { resize:vertical; min-height:90px; }
    [hidden] { display:none !important; }
    @media (max-width:900px) { .login,.toolbar,.grid,.kpis { grid-template-columns:1fr; } header { display:grid; } }
  </style>
</head>
<body>
  <header>
    <div><h1>Bind Edge AI Motor Diagnosis</h1><span>Week 2 monitoring dashboard · raw-signal anomaly-score draft</span></div>
    <button class="secondary" id="exportBtn">Export CSV</button>
  </header>
  <main>
    <section class="login panel" id="loginPanel">
      <label>User<input id="username" value="admin" autocomplete="username"></label>
      <label>Password<input id="password" type="password" autocomplete="current-password"></label>
      <button id="loginBtn">Login</button>
    </section>
    <section id="appPanel" hidden>
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
      <section class="kpis">
        <div class="kpi"><span>Visible Sites</span><strong id="siteCount">-</strong></div>
        <div class="kpi"><span>Online Devices</span><strong id="onlineCount">-</strong></div>
        <div class="kpi"><span>Warning Assets</span><strong id="warningAssets">-</strong></div>
        <div class="kpi"><span>Critical Assets</span><strong id="criticalAssets">-</strong></div>
      </section>
      <section class="grid">
        <article class="panel">
          <h2>Site Summary</h2>
          <table><thead><tr><th>Site</th><th>Region</th><th>Status</th><th>Normal</th><th>Warning</th><th>Critical</th><th>Unreviewed</th><th>Last received</th><th>Devices</th></tr></thead><tbody id="siteRows"></tbody></table>
        </article>
        <article class="panel">
          <h2 id="chartTitle">Telemetry</h2>
          <canvas id="chart" width="900" height="280"></canvas>
          <small id="chartHint">Hover the chart to inspect a timestamp and raw values.</small>
        </article>
        <article class="panel">
          <h2>Events</h2>
          <div id="events" class="event-list"></div>
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
          <label>Label<select id="labelSelect">
            <option value="needs_review">Needs review</option>
            <option value="normal_false_positive">Normal / false positive</option>
            <option value="confirmed_anomaly">Confirmed anomaly</option>
            <option value="sensor_issue">Sensor issue</option>
            <option value="repair_completed">Repair completed</option>
          </select></label>
          <label>Note<textarea id="noteInput"></textarea></label>
          <button id="saveReview" disabled>Save Review</button>
        </article>
      </section>
    </section>
  </main>
  <script>
    let token = "", sites = [], events = [], siteSummaries = [], selectedEventId = null, latestPoints = [], latestUnits = {}, renderGeneration = 0, assetGeneration = 0;
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
      await renderAssets();
      await render();
    }

    function selectedSite() {
      return sites.find(s => s.id === $("siteSelect").value) || sites[0];
    }

    async function renderAssets(requestGeneration = assetGeneration) {
      const site = selectedSite();
      const siteId = site.id;
      const assets = await api(`/api/sites/${site.id}/assets`);
      if (requestGeneration !== assetGeneration || siteId !== selectedSite().id) return false;
      setOptions($("assetSelect"), assets, item => item.id, item => item.name);
      return true;
    }

    async function render() {
      const requestGeneration = ++renderGeneration;
      const site = selectedSite();
      const assetId = $("assetSelect").value;
      const periodHours = Number($("periodSelect").value);
      const from = new Date(Date.now() - periodHours * 60 * 60 * 1000).toISOString();
      const [telem, summaries, eventPage, alertPage, modelPage] = await Promise.all([
        api(`/api/telemetry?siteId=${site.id}&assetId=${assetId}&from=${encodeURIComponent(from)}`),
        api("/api/dashboard/sites-summary"),
        api(`/api/events?siteId=${site.id}&assetId=${assetId}&from=${encodeURIComponent(from)}`),
        api(`/api/alerts?siteId=${site.id}&channel=web&status=sent&size=10`),
        api(`/api/model-versions?siteId=${site.id}&assetId=${assetId}&status=draft&size=1`),
      ]);
      if (requestGeneration !== renderGeneration) return;
      siteSummaries = summaries;
      events = eventPage.items;
      renderNotifications(alertPage.items);
      renderModelResult(modelPage.items);
      if (!events.some(event => event.id === selectedEventId)) clearEventSelection();
      $("siteCount").textContent = siteSummaries.length;
      $("onlineCount").textContent = siteSummaries.reduce((n, s) => n + s.onlineDevices, 0);
      $("warningAssets").textContent = siteSummaries.reduce((n, s) => n + s.warningAssets, 0);
      $("criticalAssets").textContent = siteSummaries.reduce((n, s) => n + s.criticalAssets, 0);
      renderSiteRows();
      renderEvents();
      $("chartTitle").textContent = `${site.name} / ${assetId}`;
      draw(telem.points, telem.units, events);
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
      body.replaceChildren(...siteSummaries.slice(0, 12).map(site => {
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
        button.className = "event";
        button.dataset.id = event.id;
        const title = document.createElement("b");
        title.textContent = `${event.id} - ${event.title}`;
        const meta = document.createElement("div");
        const pill = document.createElement("span");
        pill.className = `pill ${event.severity}`;
        pill.textContent = event.label;
        meta.append(pill, ` ${formatLocalTime(event.occurredAt)} - ${event.score}`);
        button.append(title, document.createElement("br"), meta);
        return button;
      }));
    }

    function clearEventSelection() {
      selectedEventId = null;
      $("eventDetail").textContent = "Select an event.";
      $("labelSelect").value = "needs_review";
      $("noteInput").value = "";
      $("saveReview").disabled = true;
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

    function draw(points, units, chartEvents = []) {
      latestPoints = points;
      latestUnits = units;
      const c = $("chart"), ctx = c.getContext("2d"), w = c.width, h = c.height, pad = 34;
      ctx.clearRect(0,0,w,h); ctx.fillStyle = "#fff"; ctx.fillRect(0,0,w,h);
      if (!points.length) {
        ctx.fillStyle = "#66716d"; ctx.font = "16px Segoe UI";
        ctx.fillText("No telemetry is available for the selected period.", pad, h / 2);
        $("chartHint").textContent = "No telemetry is available for the selected site, asset, and period.";
        return;
      }
      const timeRange = chartTimeRange(points);
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
      $("chartHint").textContent = "Signals are independently scaled for comparison. Hover for raw values and score evidence.";
    }

    function drawEventMarkers(ctx, points, chartEvents, width, height, pad, timeRange) {
      if (!timeRange) return;
      for (const event of chartEvents) {
        const occurredAt = new Date(event.occurredAt).getTime();
        if (!Number.isFinite(occurredAt) || occurredAt < timeRange.firstAt || occurredAt > timeRange.lastAt) continue;
        const ratio = timeRange.firstAt === timeRange.lastAt ? 1 : (occurredAt - timeRange.firstAt) / (timeRange.lastAt - timeRange.firstAt);
        const x = pad + (width - pad * 2) * ratio;
        ctx.strokeStyle = "#c2413b"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(x, pad); ctx.lineTo(x, height - pad); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = "#c2413b"; ctx.beginPath(); ctx.arc(x, pad + 8, 4, 0, Math.PI * 2); ctx.fill();
      }
    }

    document.addEventListener("click", async (e) => {
      const row = e.target.closest("[data-id]");
      if (!row) return;
      selectedEventId = row.dataset.id;
      const event = events.find(item => item.id === selectedEventId);
      if (!event) {
        clearEventSelection();
        return;
      }
      const detail = $("eventDetail");
      detail.replaceChildren();
      const title = document.createElement("b");
      title.textContent = event.title;
      detail.append(title, document.createElement("br"), `${formatLocalTime(event.occurredAt)} · ${event.duration} - ${event.score}`, document.createElement("br"), event.note);
      $("labelSelect").value = event.label;
      $("noteInput").value = event.note;
      $("saveReview").disabled = false;
    });

    $("loginBtn").addEventListener("click", () => login().catch(error => alert(error.message)));
    $("siteSelect").addEventListener("change", async () => {
      clearEventSelection();
      const assetRequestGeneration = ++assetGeneration;
      renderGeneration += 1;
      if (!await renderAssets(assetRequestGeneration)) return;
      await render();
    });
    $("assetSelect").addEventListener("change", async () => {
      clearEventSelection();
      renderGeneration += 1;
      await render();
    });
    $("periodSelect").addEventListener("change", async () => {
      clearEventSelection();
      renderGeneration += 1;
      await render();
    });
    $("refreshBtn").addEventListener("click", render);
    $("injectBtn").addEventListener("click", async () => {
      await api("/api/demo/inject-anomaly", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({siteId:$("siteSelect").value, assetId:$("assetSelect").value})});
      await render();
    });
    $("saveReview").addEventListener("click", async () => {
      if (!selectedEventId) return alert("Select an event first.");
      await api(`/api/events/${selectedEventId}/review`, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({label:$("labelSelect").value, note:$("noteInput").value})});
      await render();
    });
    $("exportBtn").addEventListener("click", async () => {
      const response = await fetch(`/api/export?siteId=${$("siteSelect").value}&assetId=${$("assetSelect").value}`, {headers:{authorization:`Bearer ${token}`}});
      if (!response.ok) return alert(`Export failed: ${response.status}`);
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `${$("siteSelect").value}_${$("assetSelect").value}.csv`;
      link.click();
      URL.revokeObjectURL(link.href);
    });
    $("chart").addEventListener("mousemove", (event) => {
      if (!latestPoints.length) return;
      const bounds = $("chart").getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width));
      const index = chartPointIndexAtRatio(latestPoints, ratio);
      const point = latestPoints[index];
      const vibration = finiteNumber(point.vibrationRmsRaw ?? point.vibration);
      const acoustic = finiteNumber(point.acousticRmsRaw ?? point.acoustic);
      const score = finiteNumber(point.anomalyScore ?? point.score);
      const vibrationLabel = latestUnits.vibrationRmsRaw ? "vibration raw RMS" : "demo vibration (mm/s RMS)";
      const acousticLabel = latestUnits.acousticRmsRaw ? "acoustic raw RMS" : "demo acoustic (dB)";
      $("chartHint").textContent = `${point.timestamp} · ${vibrationLabel}: ${vibration ?? "-"} · ${acousticLabel}: ${acoustic ?? "-"} · RPM: ${point.rpm ?? "-"} · score: ${score ?? "unavailable"} (${point.anomalyStatus ?? "-"})`;
    });
    setInterval(() => { if (token) render().catch(error => console.error(error)); }, 5000);
  </script>
</body>
</html>"""
