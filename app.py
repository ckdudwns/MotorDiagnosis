from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import csv
import io
import json
import math
import os
import time


HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8787"))


NETWORK_PROFILES = [
    {
        "type": "A",
        "name": "직접 전송형",
        "condition": "Wi-Fi 또는 유선망이 안정적인 현장",
        "architecture": "센서 단말 -> MQTT/TLS -> 중앙 수집 API",
    },
    {
        "type": "B",
        "name": "게이트웨이형",
        "condition": "설비 주변망은 가능하지만 외부망이 불안정한 현장",
        "architecture": "여러 센서 -> 현장 게이트웨이 -> 중앙 서버",
    },
    {
        "type": "C",
        "name": "제한망형",
        "condition": "상시 통신이 어렵거나 대역폭이 낮은 현장",
        "architecture": "요약 전송 + Store-and-forward + 이벤트 중심 업로드",
    },
    {
        "type": "D",
        "name": "독립 검증형",
        "condition": "통신 실사 전 또는 임시 검증 현장",
        "architecture": "로컬 저장 후 수동 반출 또는 임시망 반영",
    },
]

ISLAND_NAMES = [
    "울릉",
    "추자",
    "백령",
    "소청",
    "대청",
    "비금",
    "홍도",
    "가거",
    "덕적",
    "연평",
    "거문",
    "욕지",
    "우도",
    "흑산",
    "금오",
    "하조",
    "상조",
    "자은",
    "암태",
    "팔금",
    "안좌",
    "장산",
    "노화",
    "보길",
    "청산",
    "소안",
    "신의",
    "도초",
    "압해",
    "임자",
    "낙월",
    "위도",
    "선유",
    "어청",
    "개야",
    "삽시",
    "원산",
    "외연",
    "장고",
    "연화",
    "사량",
    "한산",
    "매물",
    "가조",
    "칠천",
    "비양",
    "마라도",
    "가파",
    "추봉",
    "두미",
    "국도",
    "소매물",
    "장봉",
    "신도",
    "시도",
    "모도",
    "무의",
    "영흥",
    "이작",
    "승봉",
    "자월",
    "풍도",
    "육도",
    "효자",
    "난지도",
]

PROFILE_CYCLE = ["A", "B", "C", "A", "B", "C", "D", "A", "B", "C"]
STATUS_CYCLE = ["normal", "normal", "warning", "normal", "critical", "normal", "device"]


def build_sites():
    sites = []
    for index, name in enumerate(ISLAND_NAMES):
        total_devices = 3 + (index % 5)
        offline = 1 if index % 9 == 0 else 2 if index % 17 == 0 else 0
        sites.append(
            {
                "id": f"SITE-{index + 1:02d}",
                "name": f"{name} 발전소",
                "region": "서해권" if index < 13 else "남해권" if index < 33 else "동해·제주권",
                "network": PROFILE_CYCLE[index % len(PROFILE_CYCLE)],
                "priority": "상" if index % 5 == 0 else "중" if index % 3 == 0 else "일반",
                "status": STATUS_CYCLE[index % len(STATUS_CYCLE)],
                "totalDevices": total_devices,
                "onlineDevices": total_devices - offline,
                "eventCount": 3 if index % 6 == 0 else 1 if index % 4 == 0 else 0,
                "assetCount": 2 + (index % 4),
                "signalQuality": max(42, 96 - ((index * 7) % 48)),
            }
        )
    return sites


SITES = build_sites()
EVENTS = [
    {
        "id": "EV-241",
        "siteId": "SITE-01",
        "assetId": "SITE-01-MOT-02",
        "severity": "critical",
        "title": "냉각수 펌프 베어링 이상 후보",
        "time": "19:42:12",
        "duration": "48초",
        "score": 92,
        "label": "확인 필요",
        "note": "진동과 음향이 같은 시간축에서 동시 상승. 주변 펌프 기동 여부 확인 필요.",
    },
    {
        "id": "EV-238",
        "siteId": "SITE-02",
        "assetId": "SITE-02-GEN-01",
        "severity": "warning",
        "title": "발전기 음향 스펙트럼 변화",
        "time": "19:31:05",
        "duration": "31초",
        "score": 78,
        "label": "확인 필요",
        "note": "정상 운전 기준선 대비 음향 고주파 성분이 증가.",
    },
    {
        "id": "EV-233",
        "siteId": "SITE-05",
        "assetId": "SITE-05-FAN-03",
        "severity": "device",
        "title": "음향 센서 노이즈 플로어 고착",
        "time": "19:20:44",
        "duration": "8분",
        "score": 64,
        "label": "이상 확인",
        "note": "설비 이상이 아닌 센서 상태 이벤트로 분류.",
    },
]

PARAMETERS = {
    "TELEMETRY_INTERVAL_SEC": 1,
    "ANOMALY_SCORE_THRESHOLD": 75,
    "ANOMALY_HOLD_SEC": 10,
    "DEVICE_OFFLINE_SEC": 120,
    "EDGE_BUFFER_HOURS": 24,
    "RETENTION_RAW_DAYS": 30,
}


def now_text():
    return time.strftime("%H:%M:%S")


def site_by_id(site_id):
    return next((site for site in SITES if site["id"] == site_id), SITES[0])


def assets_for(site_id):
    site = site_by_id(site_id)
    assets = [
        {"id": f"{site['id']}-GEN-01", "name": "1호 발전기", "type": "발전기", "rpm": 1800},
        {"id": f"{site['id']}-MOT-02", "name": "냉각수 펌프", "type": "펌프", "rpm": 1450},
        {"id": f"{site['id']}-FAN-03", "name": "환기 모터", "type": "모터", "rpm": 1200},
        {"id": f"{site['id']}-PMP-04", "name": "연료 이송 펌프", "type": "펌프", "rpm": 1600},
    ]
    return assets[: site["assetCount"]]


def telemetry_for(site_id, asset_id):
    asset = next((item for item in assets_for(site_id) if item["id"] == asset_id), assets_for(site_id)[0])
    seed = sum(ord(ch) for ch in asset["id"])
    points = []
    for index in range(72):
        vibration = 1.8 + math.sin(index / 7 + seed / 11) * 0.28 + ((seed + index * 13) % 19) / 100
        acoustic = 52 + math.sin(index / 8 + seed / 8) * 4 + ((seed + index * 7) % 9) / 2
        rpm = asset["rpm"] + math.sin(index / 11) * 18 + ((seed + index * 3) % 12)
        score = 34 + max(0, vibration - 1.9) * 18 + max(0, acoustic - 54) * 1.6
        if asset["id"].endswith("MOT-02") and index > 52:
            score += 25
        points.append(
            {
                "minute": index - 71,
                "vibration": round(vibration, 2),
                "acoustic": round(acoustic, 1),
                "rpm": round(rpm),
                "score": min(98, round(score)),
            }
        )
    return points


def inject_anomaly(payload):
    site = site_by_id(payload.get("siteId", "SITE-01"))
    asset_id = payload.get("assetId") or assets_for(site["id"])[0]["id"]
    asset = next((item for item in assets_for(site["id"]) if item["id"] == asset_id), assets_for(site["id"])[0])
    event_id = f"EV-{250 + len(EVENTS)}"
    event = {
        "id": event_id,
        "siteId": site["id"],
        "assetId": asset["id"],
        "severity": "critical",
        "title": f"{asset['name']} 이상 주입 이벤트",
        "time": now_text(),
        "duration": "10초",
        "score": 94,
        "label": "확인 필요",
        "note": "데모 주입으로 생성된 이벤트입니다.",
    }
    EVENTS.insert(0, event)
    site["status"] = "critical"
    site["eventCount"] += 1
    return event


def review_event(event_id, payload):
    event = next((item for item in EVENTS if item["id"] == event_id), None)
    if not event:
        return None
    event["label"] = payload.get("label", event["label"])
    event["note"] = payload.get("note", event["note"])
    event["reviewedAt"] = now_text()
    return event


def render_page():
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
    .ok { background:#e4f4ec; color:var(--teal); }
    .detail { display:grid; gap:10px; }
    textarea { resize:vertical; min-height:90px; }
    @media (max-width:900px) { .toolbar,.grid,.kpis { grid-template-columns:1fr; } header { display:grid; } }
  </style>
</head>
<body>
  <header>
    <div><h1>Bind Edge AI 모터 센서</h1><span>도서 발전설비 상태 관제 단일 파일 프로토타입</span></div>
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
      <div class="kpi"><span>네트워크 유형</span><strong>4</strong></div>
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
    let sites = [], events = [], selectedEventId = null;
    const $ = (id) => document.getElementById(id);
    async function api(path, options) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(path + " " + res.status);
      const type = res.headers.get("content-type") || "";
      return type.includes("json") ? res.json() : res.text();
    }
    async function load() {
      const boot = await api("/api/bootstrap");
      sites = boot.sites;
      events = boot.events;
      $("siteSelect").innerHTML = sites.map(s => `<option value="${s.id}">${s.name}</option>`).join("");
      renderAssets();
      render();
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
      $("siteRows").innerHTML = sites.slice(0, 12).map(s => `<tr><td>${s.name}</td><td>${s.network}</td><td>${s.status}</td><td>${s.onlineDevices}/${s.totalDevices}</td></tr>`).join("");
      $("events").innerHTML = events.map(e => `<button class="event" data-id="${e.id}"><b>${e.id} · ${e.title}</b><br><span class="pill ${e.severity}">${e.label}</span> ${e.time} · ${e.score}점</button>`).join("");
      $("chartTitle").textContent = site.name + " / " + assetId;
      draw(telem.points);
    }
    function draw(points) {
      const c = $("chart"), ctx = c.getContext("2d"), w = c.width, h = c.height, pad = 34;
      ctx.clearRect(0,0,w,h); ctx.fillStyle = "#fff"; ctx.fillRect(0,0,w,h);
      ctx.strokeStyle = "#d8ded9"; ctx.lineWidth = 1;
      for (let i=0;i<=4;i++){ const y=pad+(h-pad*2)/4*i; ctx.beginPath(); ctx.moveTo(pad,y); ctx.lineTo(w-pad,y); ctx.stroke(); }
      const series = [["score","#c2413b",p=>p.score],["vibration","#14796f",p=>Math.min(100,p.vibration*22)],["acoustic","#4453a8",p=>Math.min(100,p.acoustic*1.15)]];
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
    load();
  </script>
</body>
</html>"""


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            return self.send_text(render_page(), "text/html; charset=utf-8")
        if path == "/api/health":
            return self.send_json({"ok": True, "service": "Bind Edge AI app.py", "timestamp": time.time()})
        if path == "/api/bootstrap":
            return self.send_json({"sites": SITES, "networkProfiles": NETWORK_PROFILES, "events": EVENTS, "parameters": PARAMETERS})
        if path == "/api/sites":
            return self.send_json(SITES)
        if path.startswith("/api/sites/") and path.endswith("/assets"):
            site_id = path.split("/")[3]
            return self.send_json(assets_for(site_id))
        if path == "/api/events":
            return self.send_json(EVENTS)
        if path == "/api/telemetry":
            site_id = query.get("siteId", ["SITE-01"])[0]
            asset_id = query.get("assetId", [assets_for(site_id)[0]["id"]])[0]
            return self.send_json({"siteId": site_id, "assetId": asset_id, "points": telemetry_for(site_id, asset_id)})
        if path == "/api/export":
            site_id = query.get("siteId", ["SITE-01"])[0]
            asset_id = query.get("assetId", [assets_for(site_id)[0]["id"]])[0]
            return self.send_csv(site_id, asset_id)
        self.send_json({"error": "not found", "path": path}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self.read_json()

        if path == "/api/demo/inject-anomaly":
            return self.send_json(inject_anomaly(payload), status=201)
        if path.startswith("/api/events/") and path.endswith("/review"):
            event_id = path.split("/")[3]
            event = review_event(event_id, payload)
            if event:
                return self.send_json(event)
            return self.send_json({"error": "event not found"}, status=404)
        self.send_json({"error": "not found", "path": path}, status=404)

    def read_json(self):
        length = int(self.headers.get("content-length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, content_type="text/plain; charset=utf-8", status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, site_id, asset_id):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["site_id", "asset_id", "minute", "vibration_rms", "acoustic_db", "rpm", "anomaly_score"])
        for point in telemetry_for(site_id, asset_id):
            writer.writerow([site_id, asset_id, point["minute"], point["vibration"], point["acoustic"], point["rpm"], point["score"]])
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/csv; charset=utf-8")
        self.send_header("content-disposition", f'attachment; filename="{site_id}_{asset_id}.csv"')
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Bind Edge AI app is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
