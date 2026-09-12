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
    main { padding:24px; display:grid; gap:18px; max-width:1600px; margin:auto; }
    #appPanel { display:grid; gap:18px; }
    .toolbar, .grid, .kpis, .login { display:grid; gap:10px; }
    .login { grid-template-columns:1fr 1fr 140px; align-items:end; }
    .toolbar { grid-template-columns:repeat(5, minmax(150px, 1fr)); }
    .grid { grid-template-columns:1fr 1.2fr; }
    .kpis { grid-template-columns:repeat(4, 1fr); }
    .panel, .kpi { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; box-shadow:0 14px 32px rgba(23,33,31,.07); }
    .kpi span, label, th, small { color:var(--muted); font-size:14px; font-weight:600; }
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
    .workspace-nav { display:flex; flex-wrap:wrap; gap:8px; border-bottom:1px solid var(--line); padding-bottom:12px; }
    .workspace-nav button { width:auto; min-width:140px; padding:12px 18px; background:var(--panel); color:var(--muted); border-color:var(--line); }
    .workspace-nav button[aria-pressed="true"] { background:var(--teal); color:white; border-color:var(--teal); }
    button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible { outline:3px solid var(--blue); outline-offset:3px; }
    .workspace-title { margin:0; font-size:22px; }
    .facts { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:12px 0; }
    .facts div { min-width:0; padding:12px; background:var(--bg); border-radius:6px; }
    .facts dt { font-size:14px; color:var(--muted); margin-bottom:6px; }
    .facts dd { margin:0; font-weight:600; overflow-wrap:anywhere; white-space:pre-wrap; }
    .history-card { padding:14px 0; border-bottom:1px solid var(--line); }
    .history-card p { white-space:pre-wrap; overflow-wrap:anywhere; }
    .status-table { min-width:680px; }
    .status-table caption { text-align:left; font-weight:700; padding:12px 0; }
    .status-table td { font-size:14px; overflow-wrap:anywhere; }
    #snapshotRows .table-scroll { max-height:480px; }
    #snapshotRows .status-table { min-width:1100px; table-layout:fixed; }
    #snapshotRows th { position:sticky; top:0; background:var(--panel); }
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
      <label>사용자<input id="username" autocomplete="username"></label>
      <label>비밀번호<input id="password" type="password" autocomplete="current-password"></label>
      <button id="loginBtn">로그인</button>
    </section>
    <section id="appPanel" hidden>
      <div id="appStatus" class="notice" role="status" aria-live="polite" hidden></div>
      <nav class="workspace-nav" aria-label="업무 화면">
        <button id="navOverview" aria-pressed="true" aria-controls="siteKpis snapshotPanel rf66Panel sitesPanel notificationsPanel modelPanel healthPanel">운영 현황</button>
        <button id="navEvents" aria-pressed="false" aria-controls="exportPanel chartPanel eventListPanel eventReviewPanel">이벤트 검수</button>
        <button id="navManagement" aria-pressed="false" aria-controls="managementPanel">운영 관리</button>
        <button id="navModels" aria-pressed="false" aria-controls="modelReviewPanel">AI 결과 검토</button>
        <button id="navDeviceOps" aria-pressed="false" aria-controls="deviceOpsPanel">장치 운영</button>
      </nav>
      <h2 id="viewHeading" class="workspace-title">운영 현황</h2>
      <section class="toolbar panel">
        <label>사이트<select id="siteSelect"></select></label>
        <label>설비<select id="assetSelect"></select></label>
        <label id="telemetryPeriodField">조회 기간<select id="periodSelect">
          <option value="1">최근 1시간</option>
          <option value="6">최근 6시간</option>
          <option value="24" selected>최근 24시간</option>
        </select></label>
        <button id="refreshBtn">새로고침</button>
        <button class="danger" id="injectBtn" hidden>데모 이상 주입</button>
      </section>
      <section class="panel" id="exportPanel" hidden>
        <h2>조회 범위·내보내기</h2>
        <div class="subgrid">
          <label>조회 시작 (현지 시각)<input id="fromInput" type="datetime-local" step="1"></label>
          <label>조회 종료 (현지 시각)<input id="toInput" type="datetime-local" step="1"></label>
          <label>데이터셋 버전<select id="datasetSelect"><option value="">실시간 실측 데이터</option></select></label>
          <label>내보내기 형식<select id="exportFormat"><option value="csv">CSV</option><option value="xlsx">XLSX</option></select></label>
        </div>
        <div class="actions"><button id="applyRange">기간 적용</button><button id="exportBtn" class="secondary" disabled>선택 조건 내보내기</button></div>
        <small id="exportScope">내보내기는 설비·조회 기간·데이터셋 버전을 사용합니다. 아래 이벤트 목록 필터는 신호 데이터에 적용되지 않습니다.</small>
      </section>
      <section class="kpis" id="siteKpis" aria-label="전체 접근 가능 사이트 현황">
        <div class="kpi"><span>사이트</span><strong id="siteCount">-</strong></div>
        <div class="kpi"><span>온라인 장치</span><strong id="onlineCount">-</strong></div>
        <div class="kpi"><span>주의 설비</span><strong id="warningAssets">-</strong></div>
        <div class="kpi"><span>위험 설비</span><strong id="criticalAssets">-</strong></div>
      </section>
      <section class="grid">
        <article class="panel wide" id="snapshotPanel">
          <h2>단건 진동 수신 · 새 모델 판정</h2>
          <p>새 이력 정책: 상태와 관계없이 25초마다 특징 9개 · 과거 24건 확보 후 검증. 이상 진입·정상 복귀 즉시, 이상 유지 중 10초 보고는 이력과 별도로 검증합니다. 이전 5분 단건 정책도 구분하여 표시합니다.</p>
          <p>보드 상태는 장치가 보고한 값이며 서버 모델 판정과 다릅니다. 새 단건 결과는 기존 통계 점수·RF66 과거 이력과 별개입니다. 사건·알림은 아래 운영 모드에 따르며 교체 모델 미설정 시 생성하지 않습니다.</p>
          <p id="snapshotStatus" role="status" aria-live="polite">설비를 선택하고 새로고침하세요.</p>
          <div id="snapshotRows"></div>
        </article>
        <article class="panel wide" id="rf66Panel">
          <h2>RF66 진동 모델 · 과거 판정 이력</h2>
          <p>기존 RF66 모델 로딩·자동 추론은 중지되었습니다. 아래는 기존 통계 점수와 별개인 과거 구간별 판정과 연속 3구간 확인 이력입니다. 새 입력은 위의 ‘단건 진동 수신 · 새 모델 판정’에서 확인하세요.</p>
          <p id="rf66Status" role="status" aria-live="polite">설비를 선택하고 새로고침하세요.</p>
          <div id="rf66Rows"></div>
        </article>
        <article class="panel wide" id="sitesPanel">
          <h2>전체 사이트 현황</h2>
          <div class="table-scroll"><table><thead><tr><th>사이트</th><th>지역</th><th>상태</th><th>정상</th><th>주의</th><th>위험</th><th>미검수</th><th>최근 수신</th><th>장치</th></tr></thead><tbody id="siteRows"></tbody></table></div>
          <div class="actions"><button id="sitesPrev" class="secondary">이전</button><span id="sitesPage"></span><button id="sitesNext" class="secondary">다음</button></div>
        </article>
        <article class="panel wide" id="chartPanel" hidden>
          <h2 id="chartTitle">설비 신호</h2>
          <canvas id="chart" width="900" height="280"></canvas>
          <small id="chartHint">차트에 마우스를 올리거나 클릭하면 해당 시점의 원시값을 확인할 수 있습니다.</small>
          <div class="actions"><button id="zoomIn" class="secondary">확대</button><button id="panLeft" class="secondary">이전 구간</button><button id="panRight" class="secondary">다음 구간</button><button id="zoomReset" class="secondary">전체 구간</button></div>
          <small id="chartRange"></small>
        </article>
        <article class="panel" id="eventListPanel" hidden>
          <h2>이벤트 목록</h2>
          <div class="subgrid">
            <label>심각도<select id="severityFilter"><option value="">전체</option><option value="critical">Critical</option><option value="warning">Warning</option><option value="device">Device</option></select></label>
            <label>라벨<select id="eventLabelFilter"><option value="">전체</option><option value="needs_review">Needs review</option><option value="normal_false_positive">Normal / false positive</option><option value="confirmed_anomaly">Confirmed anomaly</option><option value="sensor_issue">Sensor issue</option><option value="repair_completed">Repair completed</option></select></label>
            <label>검수 상태<select id="reviewedFilter"><option value="">전체</option><option value="false">미검수</option><option value="true">검수 완료</option></select></label>
            <label>정렬<select id="eventSort"><option value="unreviewed_desc">미검수 우선</option><option value="occurredAt_desc">최신순</option><option value="occurredAt_asc">오래된순</option><option value="score_desc">최대 점수순</option></select></label>
          </div>
          <div id="events" class="event-list"></div>
          <div class="actions"><button id="eventsPrev" class="secondary">이전</button><span id="eventsPage"></span><button id="eventsNext" class="secondary">다음</button></div>
        </article>
        <article class="panel" id="notificationsPanel">
          <h2>웹 알림</h2>
          <div id="notifications" role="status" aria-live="polite">No notifications.</div>
        </article>
        <article class="panel" id="modelPanel">
          <h2>AI 기준선·모델 정보</h2>
          <div id="modelResult" role="status" aria-live="polite">Loading model metadata.</div>
        </article>
        <article class="panel wide" id="modelReviewPanel" hidden>
          <h2>AI1 결과 등록·사람 검토</h2>
          <p class="notice">자동 인계 결과는 검토 대기로 등록됩니다. 승인은 근거 검토의 기록이며, 모델 파일 검증·현장 성능 보장·운영 배포를 의미하지 않습니다.</p>
          <div class="actions"><label>검토 상태<select id="modelStatusFilter"><option value="draft">검토 대기</option><option value="approved">승인</option><option value="rejected">반려</option><option value="">전체</option></select></label><button id="modelQueueLoad" class="secondary">목록 새로고침</button></div>
          <div id="modelQueue" class="history" role="status" aria-live="polite">목록을 조회하세요.</div>
          <div class="actions"><button id="modelQueuePrev" class="secondary">이전</button><span id="modelQueuePage"></span><button id="modelQueueNext" class="secondary">다음</button></div>
          <h3>선택 결과의 등록 근거</h3>
          <div id="modelReviewSummary">검토할 결과를 선택하세요.</div>
          <details><summary>데이터셋·기준선·지표·산출물 체크섬·검토 이력</summary><pre id="modelReviewEvidence"></pre></details>
          <label>승인·반려 사유<textarea id="modelReviewReason" maxlength="1000" placeholder="검토한 근거와 판단 사유를 작성하세요."></textarea></label>
          <div class="actions"><button id="modelApprove" disabled>승인 기록</button><button id="modelReject" class="secondary" disabled>반려 기록</button></div>
          <div id="modelReviewStatus" role="status" aria-live="polite"></div>
        </article>
        <article class="panel detail" id="eventReviewPanel" hidden>
          <h2>이벤트 상세·검수</h2>
          <div id="eventDetail">목록에서 이벤트를 선택하세요.</div>
          <canvas id="evidenceChart" width="900" height="220" aria-label="선택 이벤트 전후 신호"></canvas>
          <div id="evidenceSummary"></div>
          <details><summary>특징·규칙·장치 스냅샷</summary><pre id="eventEvidence"></pre></details>
          <label>검수 라벨<select id="labelSelect">
            <option value="needs_review">Needs review</option>
            <option value="normal_false_positive">Normal / false positive</option>
            <option value="confirmed_anomaly">Confirmed anomaly</option>
            <option value="sensor_issue">Sensor issue</option>
            <option value="repair_completed">Repair completed</option>
          </select></label>
          <label>검수 요약 메모<textarea id="noteInput"></textarea></label>
          <label>검수 변경 사유<input id="reviewReason" maxlength="1000"></label>
          <button id="saveReview" disabled>검수 저장</button>
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
        <article class="panel wide" id="healthPanel"><h2>선택 사이트의 장치·서비스 상태</h2><div id="deviceHealth" class="table-scroll"></div><div id="serviceHealth" class="table-scroll"></div></article>
        <article class="panel wide" id="managementPanel" hidden>
          <h2>운영 관리</h2>
          <div class="subgrid"><label>관리 항목<select id="managementKind"></select></label><label>항목 선택<select id="managementRecord"></select></label></div>
          <div class="actions"><button id="managementLoad" class="secondary">목록 새로고침</button><button id="managementNew" class="secondary" hidden>신규 등록</button><button id="managementDelete" class="danger" hidden>삭제</button></div>
          <div id="managementStatus" class="notice" role="status"></div>
          <form id="managementForm"><div id="managementFields" class="subgrid"></div><label>변경 사유<input id="managementReason" maxlength="1000" required></label><button id="managementSave" type="submit" hidden>변경 저장</button></form>
          <details><summary>현재 등록 정보·이력</summary><pre id="managementDetail"></pre></details>
          <section id="auditPanel" hidden><h2>감사 기록</h2><div class="subgrid"><label>작업 종류<input id="auditAction"></label><label>대상 종류<input id="auditTarget"></label><label>작성자 ID<input id="auditActor"></label></div><div class="actions"><button id="auditLoad" class="secondary">조회</button><button id="auditPrev" class="secondary">이전</button><span id="auditPage"></span><button id="auditNext" class="secondary">다음</button></div><div id="auditRows" class="history table-scroll"></div></section>
        </article>
        <article class="panel wide" id="deviceOpsPanel" hidden>
          <h2>장치 운영 · 통신 품질 · 제한된 설정</h2>
          <p class="notice">선택 설비에 연결된 장치만 표시합니다. 통신 품질은 장치가 보고한 HTTP 전송 관측값이며, 보고가 없는 구간은 정상으로 간주하지 않습니다.</p>
          <div class="actions"><label>운영 장치<select id="opsDevice"></select></label><button id="opsReload" class="secondary">장치 목록 새로고침</button></div>
          <div id="opsStatus" role="status" aria-live="polite"></div>
          <div id="opsIdentity"></div><div id="opsHealth" class="table-scroll"></div>
          <h3>통신 품질</h3>
          <div class="subgrid">
            <label>품질 조회 기간<select id="opsQualityRange"><option value="1">최근 1시간</option><option value="6">최근 6시간</option><option value="24" selected>최근 24시간</option><option value="168">최근 7일</option><option value="720">최근 30일</option></select></label>
            <label>집계 간격<select id="opsQualityBucket"><option value="60">1분</option><option value="300">5분</option><option value="3600" selected>1시간</option><option value="86400">1일</option></select></label>
          </div>
          <div class="actions"><button id="opsQualityLoad" class="secondary">상태·품질 조회</button></div>
          <div id="opsQualityStatus" role="status" aria-live="polite"></div><div id="opsQualitySummary"></div>
          <p><small>관측 창 전체를 종료 시각의 구간에 배정합니다. 긴 창은 구간 밖 관측을 포함할 수 있습니다. ACK 지연은 HTTP 응답 검증까지의 왕복 시간이며, 버퍼 평균은 표본 평균입니다. 화면은 자동 갱신하지 않으며 조회 시각을 확인해 주세요.</small></p>
          <div id="opsQualityRows" class="table-scroll"></div>
          <div class="actions"><button id="opsQualityPrev" class="secondary" disabled>이전 구간</button><span id="opsQualityPage"></span><button id="opsQualityNext" class="secondary" disabled>다음 구간</button></div>
          <h3>제한된 원격 설정</h3>
          <p class="notice">발행은 서버에 요청을 저장하는 단계입니다. 장치의 적용 성공 보고 전에는 적용 완료가 아닙니다. 센서·인증·네트워크·모터 속도·OTA 설정은 변경하지 않습니다.</p>
          <div id="opsConfigState"></div>
          <div class="actions"><button id="opsConfigLoad" class="secondary">서버 설정 다시 확인</button></div>
          <div class="subgrid">
            <label>측정 후 대기 시간 (ms)<input id="opsInterval" type="number" min="3000" max="60000" step="1" disabled></label>
            <label>루프당 버퍼 재전송 상한 (건)<input id="opsReplay" type="number" min="1" max="4" step="1" disabled></label>
            <label>정기 상태 보고 간격 (ms)<input id="opsHealthInterval" type="number" min="10000" max="300000" step="1" disabled></label>
          </div>
          <small>대기 시간 3,000–60,000ms, 재전송 1–4건, 정기 상태 보고 10,000–300,000ms. 고장·복구는 별도 보고하며 실제 측정 시작 간격은 취득·전송 시간에 따라 달라집니다.</small>
          <label>설정 변경 사유<textarea id="opsConfigReason" maxlength="1000" disabled></textarea></label>
          <div class="actions"><button id="opsConfigPublish" disabled>설정 요청 발행</button><button id="opsConfigCancel" class="secondary" disabled>편집 취소</button></div>
          <div id="opsConfigStatus" role="status" aria-live="polite"></div>
          <h3>설정 요청·결과 이력</h3>
          <button id="opsHistoryLoad" class="secondary">이력 새로고침</button>
          <div id="opsHistoryStatus" role="status"></div><div id="opsHistoryRows" class="table-scroll"></div>
          <div class="actions"><button id="opsHistoryPrev" class="secondary" disabled>이전 이력</button><span id="opsHistoryPage"></span><button id="opsHistoryNext" class="secondary" disabled>다음 이력</button></div>
          <h3>분석 특징·요청한 원시 파형</h3>
          <p class="notice">분석 채널을 활성화한 장치에서만 수신합니다. 평상시 특징량은 30초 간격이며, 원시 음향·진동 파형은 요청 후 다음 정상 0.64초 구간만 보존합니다. 요청은 5분 후 만료되며 실제 수신 전에는 완료가 아닙니다.</p>
          <label>원시 파형 요청 사유<input id="opsAnalysisReason" maxlength="1000" disabled></label>
          <div class="actions"><button id="opsAnalysisRequest" disabled>다음 구간 파형 보존 요청</button><button id="opsAnalysisLoad" class="secondary" disabled>분석 기록 조회</button></div>
          <div id="opsAnalysisStatus" role="status"></div><div id="opsAnalysisRows"></div><div id="opsWaveform"></div>
          <h3>연속 진동 구간 · 800Hz / 640ms</h3>
          <p>구간별 21개 특징과 비교 판정을 조회합니다. 모델 대기·불량 구간은 정상 판정이 아닙니다.</p>
          <button id="opsWindowsLoad" class="secondary" disabled>최근 진동 구간 100건 조회</button>
          <div id="opsWindowsStatus" role="status"></div><div id="opsWindowsRows"></div>
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
    let analysisGeneration = 0, analysisBusy = false, analysisIntent = null;
    let windowsGeneration = 0;
    let modelQueueGeneration = 0, modelReviewGeneration = 0, modelQueueRows = [], modelReviewRow = null;
    let modelQueuePage = 1, modelQueueTotal = 0, modelReviewBusy = false, pendingModelReview = null;
    let reviewSaving = false, noteSaving = false, managementSaving = false, noteHistoryGeneration = 0;
    let noteListGeneration = 0;
    let currentView = "overview";
    let opsGeneration = 0, opsDevices = [], opsDevice = null, opsConfig = null;
    let opsConfigGeneration = 0, opsQualityGeneration = 0, opsHealthGeneration = 0, opsHistoryGeneration = 0;
    let opsSaving = false, opsDirty = false, opsUncertain = false, opsIntent = null;
    let opsQuality = null, opsQualityPage = 1, opsHistory = [], opsHistoryPage = 1;
    const CHART_PAD = 34;
    const $ = (id) => document.getElementById(id);

    async function api(path, options = {}, preserveJson = false) {
      const headers = {...(options.headers || {})};
      if (token) headers.authorization = `Bearer ${token}`;
      const res = await fetch(path, {...options, headers});
      const type = res.headers.get("content-type") || "";
      const original = preserveJson ? await res.text() : null;
      let body;
      if (preserveJson) body = type.includes("json") ? JSON.parse(original) : original;
      else body = type.includes("json") ? await res.json() : await res.text();
      if (!res.ok) throw new Error(body?.error?.message || `${path} ${res.status}`);
      if (preserveJson && !type.includes("json")) throw new Error("JSON 응답이 아닙니다.");
      return preserveJson ? {body, original} : body;
    }

    async function login() {
      clearSnapshots();
      clearRF66();
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
    const WORKSPACE_VIEWS = {
      overview:{button:"navOverview",title:"운영 현황",panels:["siteKpis","snapshotPanel","rf66Panel","sitesPanel","notificationsPanel","modelPanel","healthPanel"]},
      events:{button:"navEvents",title:"이벤트 검수",panels:["exportPanel","chartPanel","eventListPanel","eventReviewPanel"]},
      management:{button:"navManagement",title:"운영 관리",panels:["managementPanel"]},
      models:{button:"navModels",title:"AI 결과 검토",panels:["modelReviewPanel"]},
      deviceOps:{button:"navDeviceOps",title:"장치 운영",panels:["deviceOpsPanel"]},
    };
    function hasManagementAccess() {return can("audit-log:read") || Object.values(MANAGEMENT).some(config => can(config.permission + ":read"));}
    function setView(view) {
      if (!Object.hasOwn(WORKSPACE_VIEWS, view) || (view === "management" && !hasManagementAccess())) return;
      if (view === "models" && !can("model:read")) return;
      if (view === "deviceOps" && !can("device:read")) return;
      const enteringModels = view === "models" && currentView !== "models";
      const enteringDevices = view === "deviceOps" && currentView !== "deviceOps";
      const enteringOverview = view === "overview" && currentView !== "overview";
      if (view !== "overview") clearSnapshots();
      currentView = view;
      for (const [name,config] of Object.entries(WORKSPACE_VIEWS)) {
        $(config.button).setAttribute("aria-pressed", String(name === view));
        for (const id of config.panels) $(id).hidden = name !== view;
      }
      $("navManagement").hidden = !hasManagementAccess();
      $("navModels").hidden = !can("model:read");
      $("navDeviceOps").hidden = !can("device:read");
      $("telemetryPeriodField").hidden = view === "deviceOps";
      $("viewHeading").textContent = WORKSPACE_VIEWS[view].title;
      if (view === "events") redrawChart();
      if (enteringModels && !modelReviewRow) act(() => loadModelQueue(true));
      if (enteringDevices && !opsDirty && !opsSaving) act(loadDeviceOperations);
      if (enteringOverview) void loadSnapshots(selectionQuery());
    }
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
      clearRF66("새로고침 후 RF66 결과를 확인합니다.");
      if (currentView === "models") return loadModelQueue(true);
      if (currentView === "deviceOps") return loadDeviceOperations();
      const requestGeneration = ++renderGeneration;
      const query = selectionQuery();
      if (!query) { clearSnapshots("조회 가능한 사이트 또는 설비가 없습니다."); statusMessage("조회 가능한 사이트 또는 설비가 없습니다."); return; }
      // New input is independent of old telemetry, health and RF66 requests.
      if (currentView === "overview") void loadSnapshots(query);
      if (JSON.stringify(query) !== JSON.stringify(lastQuery)) {lastQuery = null; $("exportBtn").disabled = true;}
      const site = selectedSite();
      const assetId = query.assetId, from = query.from, to = query.to;
      const [telem, summaries, eventPage, alertPage, modelPage, rule, devices, dependencies] = await Promise.all([
        api(`/api/telemetry?${scopeParams(query)}`),
        api("/api/dashboard/sites-summary"),
        api(`/api/events?siteId=${site.id}&assetId=${assetId}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&${eventFilterParams()}`),
        api(`/api/alerts?siteId=${site.id}&channel=web&status=sent&size=10`),
        api(`/api/model-versions?siteId=${site.id}&assetId=${assetId}&size=1`),
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
      void loadRF66(devices, query);
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

    let snapshotGeneration = 0, snapshotBusy = null;
    function clearSnapshots(message = "설비를 선택하고 새로고침하세요.") {
      snapshotGeneration++;
      snapshotBusy = null;
      $("snapshotRows").replaceChildren();
      $("snapshotStatus").textContent = message;
    }
    const snapshotNumber = value => typeof value === "number" && Number.isFinite(value);
    const snapshotTime = value => typeof value === "string" && Number.isFinite(Date.parse(value));
    const snapshotScope = (value, scope) => value && ["deviceId","siteId","assetId"].every(k => value[k] === scope[k]);
    const verifierFeatures = ["cf_a_1","cf_a_2","cf_a_3","sk_a_1","sk_a_2","sk_a_3","ku_a_1","ku_a_2","ku_a_3"];
    const isHistoryVerifier = model => model?.contractId === "history-event-verifier-v1";
    function snapshotModelValid(model, scope) {
      const input = model?.inputContract;
      const common = snapshotScope(model?.scope, scope)
        && ["modelId","modelVersion","preprocessingVersion","scoreType"].every(k =>
          typeof model[k] === "string" && /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(model[k]))
        && snapshotNumber(model.threshold) && [">",">=","<","<="].includes(model.comparison);
      if (!common) return false;
      if (isHistoryVerifier(model)) return model.modelId === "pump-event-verifier-v1"
        && typeof model.scope.sensorId === "string" && /^[A-Z0-9][A-Z0-9._-]{0,99}$/.test(model.scope.sensorId)
        && model.scoreType === "novelty_reference_ratio" && model.threshold === 1 && model.comparison === ">"
        && input?.adapterId === "pump-nine-source-features-v1" && input.sourceProfileId === "pump-cf-sk-ku-summary-v1"
        && JSON.stringify(input.shape) === "[9]" && JSON.stringify(input.features) === JSON.stringify(verifierFeatures)
        && input.unit === "source_feature_values" && input.historyRows === 24 && input.historyIntervalSec === 25 && input.currentEventMaxDelaySec === 50;
      return model.contractId === "single-snapshot-anomaly-v1"
        && input?.adapterId === "adxl345-xyz-g-unmodified-v1" && input.sourceProfileId === "adxl345-800hz-xyz-counts-v1"
        && JSON.stringify(input.shape) === "[512,3]" && JSON.stringify(input.axes) === '["X","Y","Z"]'
        && input.unit === "g" && input.sampleRateHz === 800 && input.gPerCount === .0039 && input.meanRemoved === false;
    }
    function snapshotCompleted(item) {
      const a = item.analysis, model = a?.inference?.model, w = item.window;
      const inputValid = isHistoryVerifier(model) ? w.profileId === "pump-cf-sk-ku-summary-v1"
        && w.sensorId === model.scope?.sensorId
        && w.features && Object.keys(w.features).length === 9 && verifierFeatures.every(k=>snapshotNumber(w.features[k]))
        : w.sampleCount === 512;
      if (a?.status !== "completed" || w.quality !== "valid" || !inputValid
          || !snapshotModelValid(model, w) || !snapshotNumber(a.score) || !snapshotNumber(a.threshold)
          || typeof a.verdict !== "boolean" || a.affectsAlerts !== false || a.reason !== null
          || !snapshotTime(a.inference.completedAt) || a.inference.inputDigest !== item.digest
          || !/^[0-9a-f]{64}$/.test(item.digest || "")
          || a.modelVersion !== model.modelVersion || a.threshold !== model.threshold
          || a.comparison !== model.comparison || a.scoreType !== model.scoreType) return false;
      const decision = a.comparison === ">" ? a.score > a.threshold : a.comparison === ">=" ? a.score >= a.threshold
        : a.comparison === "<" ? a.score < a.threshold : a.score <= a.threshold;
      return a.verdict === decision;
    }
    function snapshotProcessing(item) {
      const a = item.analysis || {}, job = item.inferenceJob;
      if (snapshotCompleted(item)) return isHistoryVerifier(a.inference.model)
        ? (a.verdict ? "이상 후보 · 학습 기준 초과" : "학습 기준 미초과 · 정상 확정 아님")
        : (a.verdict ? "모델 이상 후보" : "모델 정상 후보");
      if (a.status === "unavailable") return "판정 불가";
      if (a.status === "queued" && item.window.quality === "valid") return "입력 준비 대기";
      if (a.status === "waiting_model" && item.window.quality === "valid") return "모델 대기 · 이 구간에 모델 미배정";
      if (a.status === "queued_inference" && item.window.quality === "valid" && job) {
        if (job.runtimeAvailable === false) return "추론 대기 · 해당 모델 연결 중지";
        if (job.runtimeAvailable === true && job.status === "running") return "모델 추론 중";
        if (job.runtimeAvailable === true && job.status === "queued") return "모델 추론 대기";
      }
      return "판정 불가 · 결과 형식 확인 필요";
    }
    function snapshotReason(reason) {
      const labels = {VERIFIER_REQUIRES_24_PRIOR_RECORDS:"25초 과거 이력 24건 확보 필요",
        VERIFIER_HISTORY_DISCONTINUITY:"25초 이력 시각·순번 불연속",VERIFIER_HISTORY_UPTIME_DISCONTINUITY:"이력 uptime 불연속",
        VERIFIER_HISTORY_QUALITY_OR_SCOPE:"이력 품질 불량 또는 부팅·대상 변경",VERIFIER_EVENT_HISTORY_TOO_OLD:"마지막 이력과 현재 구간의 간격이 50초 초과 또는 시각 불일치",
        MODEL_NOT_CONFIGURED:"수신 당시 모델 미설정",INFERENCE_NOT_ENABLED:"입력 준비 전",
        fifo_overrun:"센서 FIFO 넘침",clipped:"센서 포화",sample_gap:"샘플 누락",
        sensor_unavailable:"센서 사용 불가",INPUT_PREPARATION_FAILED:"입력 준비 실패",
        SNAPSHOT_INTEGRITY_MISMATCH:"무결성 불일치",SNAPSHOT_INPUT_INTEGRITY_FAILED:"추론 입력 무결성 확인 실패",
        MODEL_EXECUTION_FAILED:"모델 실행 실패",MODEL_OUTPUT_INVALID:"모델 출력 형식 오류",
        INFERENCE_DEADLINE_EXCEEDED:"추론 제한 시간 초과",INFERENCE_RETRY_EXHAUSTED:"추론 재시도 한도 초과"};
      return Object.hasOwn(labels, reason) ? labels[reason] + " (" + reason + ")" : (reason || "—");
    }
    function snapshotDelivery(info) {
      if (info?.policyId === "pump-verifier-history-v1") {
        const history = info.reason === "history_periodic";
        const expected = history ? [info.state,"periodic",25] : {
          anomaly_enter:["ANOMALY_ACTIVE","immediate",0],anomaly_periodic:["ANOMALY_ACTIVE","periodic",10],
          normal_recovered:["NORMAL","immediate",0]}[info.reason];
        const a=info.anomalyCount,n=info.normalCount;
        if (!expected || !["NORMAL","ANOMALY_ACTIVE"].includes(info.state)
            || info.state!==expected[0] || info.mode!==expected[1] || info.intervalSec!==expected[2]
            || ![a,n].every(v=>Number.isInteger(v)&&v>=0&&v<=2147483647) || (a&&n)
            || (info.state==="NORMAL"&&a>=3) || (info.state==="ANOMALY_ACTIVE"&&n>=5)
            || (info.reason==="anomaly_enter"&&(a!==3||n!==0)) || (info.reason==="normal_recovered"&&(n!==5||a!==0)))
          return {state:"보고 형식 확인 필요",reason:"전송 정책 확인 필요",interval:null,counters:"미확인"};
        return {state:info.state+" · 보드 보고",reason:history ? "25초 정기 이력" : {
          anomaly_enter:"이상 진입 · 즉시 검증",anomaly_periodic:"이상 유지 · 10초 검증",normal_recovered:"정상 복귀 · 즉시 검증"}[info.reason],
          interval:info.state==="NORMAL" ? 25 : 10,counters:`이상 ${a}회 / 정상 ${n}회 (보드 보고)`};
      }
      if (info?.policyId === "periodic-single-v1" && info.mode === "periodic" && info.intervalSec === 300)
        return {state:"미보고 (이전 5분 정책)", reason:"5분 정기 전송", interval:300, counters:"미보고"};
      const rules = {
        normal_periodic:["NORMAL","periodic",300,"평상시 정기 전송"],
        anomaly_enter:["ANOMALY_ACTIVE","immediate",0,"이상 진입 · 즉시 전송"],
        anomaly_periodic:["ANOMALY_ACTIVE","periodic",10,"이상 상태 정기 전송"],
        normal_recovered:["NORMAL","immediate",0,"정상 복귀 · 즉시 전송"],
      };
      const rule = Object.hasOwn(rules, info?.reason) ? rules[info.reason] : null;
      const countsValid = [info?.anomalyCount,info?.normalCount].every(n => Number.isInteger(n) && n >= 0 && n <= 2147483647)
        && !(info.anomalyCount && info.normalCount);
      const transitionValid = countsValid && (info.reason === "anomaly_enter" ? info.anomalyCount === 3 && info.normalCount === 0
        : info.reason === "normal_recovered" ? info.normalCount === 5 && info.anomalyCount === 0
        : info.reason === "normal_periodic" ? info.anomalyCount < 3 : info.normalCount < 5);
      if (info?.policyId !== "edge-state-snapshot-v1" || !rule || !transitionValid
          || info.state !== rule[0] || info.mode !== rule[1] || info.intervalSec !== rule[2])
        return {state:"보고 형식 확인 필요",reason:"전송 정책 확인 필요",interval:null,counters:"미확인"};
      return {state:info.state === "NORMAL" ? "NORMAL · 보드 정상 상태" : "ANOMALY_ACTIVE · 보드 이상 상태",
        reason:rule[3], interval:info.state === "NORMAL" ? 300 : 10,
        counters:`이상 ${info.anomalyCount}회 / 정상 ${info.normalCount}회 (보드 보고)`};
    }
    function snapshotFreshness(item, queriedAt) {
      const interval = snapshotDelivery(item.transmission).interval;
      if (!snapshotTime(queriedAt) || interval === null) return "확인 불가 · 서버 조회 시각 또는 전송 정책 미확인";
      const age = (Date.parse(queriedAt) - Date.parse(item.window.timestamp)) / 1000;
      if (age < -5) return "시각 확인 필요 · 측정 시각이 서버 조회 시각보다 미래입니다.";
      if (age > interval + 60) return `최신 측정 지연 · ${Math.round(age)}초 전 측정 (예상 ${interval}초 + 여유 60초 초과)`;
      return `예상 보고 간격 내 · ${Math.max(0,Math.round(age))}초 전 측정 (예상 ${interval}초 + 여유 60초)`;
    }
    function snapshotModelText(model) {
      return `${model.modelId} · ${model.modelVersion} · 전처리 ${model.preprocessingVersion} · ${model.scoreType} · 점수 ${model.comparison} ${model.threshold}일 때 이상 후보`
        + (isHistoryVerifier(model) ? ` · 적용 센서 ${model.scope.sensorId} · 고장 확률 아님 · 라벨 기반 성능·적용 장비 검증 미완료` : "");
    }
    function renderSnapshotCard(result) {
      const card = document.createElement("section"); card.className = "history-card";
      const title = document.createElement("h3"); title.textContent = result.deviceId + " / " + result.assetId;
      const configured = document.createElement("p");
      configured.textContent = result.configuredModel ? "현재 연결 모델: " + snapshotModelText(result.configuredModel)
        : "현재 교체 모델 미설정 · 수신·입력 준비는 가능하며, 기존 RF66은 실행하지 않습니다.";
      const policy = document.createElement("p");
      policy.textContent = "단건 이벤트 운영 모드: " + ({shadow:"비교만 · 사건·알림 미생성",events:"이벤트 기록 · 알림 꺼짐",alerts:"이벤트 기록 + 알림 허용 (정책·입력 유효성 적용)"}[result.eventPolicy?.mode] || "미확인 (이전 서버)")
        + (isHistoryVerifier(result.configuredModel) ? " · 이력 검증 기준 초과로 발생 / 같은 모델 기준 미초과로 사건 해제 (장비 정상 확정 아님)"
          : " · 서버 이상 1건으로 발생 / 같은 모델 정상 1건으로 복귀") + " · 품질 불량·수신 중단은 관측 불명";
      card.append(title,configured,policy);
      // Measured time, not receipt time, determines the latest state. Keep the API's tie order.
      const items = [...result.items].sort((a,b) => Date.parse(b.window.timestamp) - Date.parse(a.window.timestamp));
      if (!items.length) {
        const empty = document.createElement("p"); empty.textContent = "새 단건 입력 대기 · 이 경로에 저장된 측정이 없습니다. 기존 telemetry·RF66 수신과는 별개입니다.";
        card.appendChild(empty); return card;
      }
      const latest = items[0], delivery = snapshotDelivery(latest.transmission);
      card.appendChild(facts([
        ["최근 측정 센서",latest.window.sensorId || "미구분 (이전 Raw 규격)"],
        ["최근 측정 시각",formatLocalTime(latest.window.timestamp)], ["해당 구간 서버 수신 시각",formatLocalTime(latest.receivedAt)],
        ["최근 측정의 보드 보고",delivery.state], ["전송 사유",delivery.reason],
        ["최근 구간 서버 처리",snapshotProcessing(latest)], ["입력 품질·사유",latest.window.quality + " / " + snapshotReason(latest.analysis?.reason)],
        ["검증에 사용한 과거 이력",latest.analysis?.evidence ? `${latest.analysis.evidence.historicalRecordsUsed}건 / 필요 24건` : "—"],
      ]));
      const freshness = document.createElement("p"); freshness.textContent = snapshotFreshness(latest,result.queriedAt)
        + " · 장치 온라인/오프라인 판정이 아닙니다. 아래 장치 상태에서 별도로 확인하세요.";
      const summary = document.createElement("p"), recent = items.find(snapshotCompleted);
      summary.textContent = recent ? "최근 측정 기준 완료 결과 · 판정 완료: " + formatLocalTime(recent.analysis.inference.completedAt)
        + " · 대상 측정 " + formatLocalTime(recent.window.timestamp) + " · " + snapshotProcessing(recent)
        + " · 현재 상태 보장 아님" : "조회된 최근 20건 내 완료된 모델 판정 없음";
      card.append(freshness,summary);
      const scroll = document.createElement("div"); scroll.className = "table-scroll";
      scroll.appendChild(statusTable("최근 측정 최대 20건 · 점수 범위·의미는 모델별 규격을 따릅니다.",
        ["측정 / 서버 수신 시각","보드 보고 / 전송 사유","서버 처리·판정","모델 점수 / 임계 조건","입력 품질 / 사유","판정 완료 / 적용 모델","구간 식별 / 도착","사건 처리"],
        items.slice(0,20).map(item => {
          const a = item.analysis || {}, valid = snapshotCompleted(item), d = snapshotDelivery(item.transmission);
          const m = a.inference?.model;
          const same = m && result.configuredModel && ["modelId","modelVersion","preprocessingVersion","scoreType","threshold","comparison"].every(k => m[k] === result.configuredModel[k])
            && (!isHistoryVerifier(m) || m.scope.sensorId === result.configuredModel.scope.sensorId);
          return [formatLocalTime(item.window.timestamp) + " / " + formatLocalTime(item.receivedAt),
            d.state + " / " + d.reason + " / " + d.counters, snapshotProcessing(item),
            valid ? `${a.score} / ${a.comparison} ${a.threshold} (${a.scoreType})` : "—",
            item.window.quality + " / " + snapshotReason(a.reason),
            valid ? formatLocalTime(a.inference.completedAt) + " / " + snapshotModelText(m) + (same ? "" : " (이전 모델 설정의 결과)") : "—",
            (item.window.sensorId ? item.window.sensorId+" / " : "") + item.window.bootId + " / #" + item.window.windowIndex + (item.lateArrival ? " · 늦은 도착" : ""),
            ({pending:"처리 대기",processed:"처리 완료 (사건 발생 여부는 이벤트 검수에서 확인)",ignored:"사건 반영 제외"}[item.eventProcessing?.status] || "미기록") + (item.eventProcessing?.reason ? " · " + item.eventProcessing.reason : "")];
        })));
      card.appendChild(scroll); return card;
    }
    async function snapshotApi(path) {
      const controller = new AbortController();
      let timer;
      const timeout = new Promise((resolve,reject) => {
        timer = setTimeout(() => {reject(new Error("조회 제한 시간 초과 · 다음 갱신에서 재시도합니다.")); controller.abort();},15000);
      });
      try {return await Promise.race([api(path,{signal:controller.signal}),timeout]);}
      finally {clearTimeout(timer);}
    }
    async function loadSnapshots(query) {
      if (currentView !== "overview") return;
      if (!query) {clearSnapshots("조회 가능한 사이트 또는 설비가 없습니다."); return;}
      if (!["device:read","telemetry:read"].every(can)) {clearSnapshots("단건 수신 조회 권한이 없습니다."); return;}
      const key = JSON.stringify([token,query.siteId,query.assetId]);
      // A slow poll must finish, not be invalidated every five seconds.
      if (snapshotBusy?.key === key) return;
      clearSnapshots("단건 수신 조회 중 · 조회 기간 필터와 별개인 최근 측정 최대 20건입니다.");
      const generation = snapshotGeneration, session = token;
      snapshotBusy = {key,generation};
      const current = () => generation === snapshotGeneration && session === token && currentView === "overview"
        && $("siteSelect").value === query.siteId && $("assetSelect").value === query.assetId
        && ["device:read","telemetry:read"].every(can);
      try {
        const devices = await snapshotApi(`/api/sites/${encodeURIComponent(query.siteId)}/devices`);
        if (!current()) return;
        if (!Array.isArray(devices)) throw new Error("장치 목록 형식 불일치");
        const selected = devices.filter(d => d.siteId === query.siteId && d.assetId === query.assetId);
        if (!selected.length) {$("snapshotStatus").textContent = "선택 설비에 조회 가능한 장치가 없습니다."; return;}
        let errors = 0;
        const cards = await Promise.all(selected.map(async device => {
          const id = device.id || device.deviceId, scope = {deviceId:id,siteId:query.siteId,assetId:query.assetId};
          try {
            if (typeof id !== "string" || !id) throw new Error("장치 ID 확인 필요");
            const result = await snapshotApi(`/api/devices/${encodeURIComponent(id)}/periodic-snapshots`);
            if (!current()) return null;
            if (!snapshotScope(result,scope) || !Array.isArray(result.items) || !Object.hasOwn(result,"configuredModel")
                || result.affectsAlerts !== Boolean(result.configuredModel && result.eventPolicy?.mode === "alerts")
                || result.items.some(item => !snapshotScope(item?.window,scope)
                  || !snapshotTime(item.window.timestamp) || !snapshotTime(item.receivedAt)))
              throw new Error("장치·설비 매핑 또는 결과 형식 불일치");
            if (result.configuredModel !== null && !snapshotModelValid(result.configuredModel,scope))
              throw new Error("단건 모델 설정 응답 확인 필요");
            return renderSnapshotCard(result);
          } catch (error) {
            if (!current()) return null;
            errors++;
            const failed = document.createElement("p"); failed.className = "notice error";
            failed.textContent = id + " · 단건 조회 실패: " + error.message + " · 로그인·권한·서버 배포 상태를 확인하세요.";
            return failed;
          }
        }));
        if (!current()) return;
        $("snapshotRows").replaceChildren(...cards.filter(Boolean));
        $("snapshotStatus").textContent = (errors ? `${errors}개 장치 조회 실패 · ` : "조회 완료 · ")
          + "운영 현황에서 5초마다 갱신 · 조회 기간과 별개 · 시각은 브라우저 현지 시각입니다.";
      } catch (error) {
        if (current()) {$("snapshotRows").replaceChildren(); $("snapshotStatus").textContent = "단건 조회 실패: " + error.message;}
      } finally {
        if (snapshotBusy?.generation === generation) snapshotBusy = null;
      }
    }

    let rf66Generation = 0;
    function clearRF66(message = "설비를 선택하고 새로고침하세요.") {
      rf66Generation++;
      $("rf66Rows").replaceChildren();
      $("rf66Status").textContent = message;
    }
    function rf66Number(value) {
      return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
    }
    function rf66Completed(item) {
      const a = item.analysis || {};
      return a.status === "completed" && item.window.quality === "valid"
        && a.modelType === "random_forest" && a.featureProfileId === "mcc5-vibration-800hz-spectral66-v1"
        && typeof a.modelVersion === "string" && /^sha256:[0-9a-f]{64}$/.test(a.modelVersion)
        && rf66Number(a.score) && rf66Number(a.threshold) && typeof a.verdict === "boolean"
        && a.verdict === (a.score > a.threshold) && a.affectsAlerts === false;
    }
    function rf66Confirmation(item) {
      const a = item.analysis || {}, c = a.confirmation;
      if (a.confirmationApplied !== true) return "미적용 (기존 결과/모델 대기)";
      if (!rf66Completed(item)) return "확인 불가 · 연속 초기화";
      if (!c || c.policyId !== "rf66-consecutive-3-v1" || c.width !== 3 || c.requiredHits !== 3
          || c.affectsAlerts !== false || !Array.isArray(c.verdicts)
          || c.verdicts.length < 1 || c.verdicts.length > 3
          || c.verdicts.some(v => typeof v !== "boolean")
          || c.validWindows !== c.verdicts.length || c.anomalyHits !== c.verdicts.filter(Boolean).length
          || c.verdicts.at(-1) !== a.verdict) return "확인 불가 · 결과 형식 확인 필요";
      const decision = c.validWindows < 3 ? -1 : c.anomalyHits === 3 ? 1 : 0;
      const status = decision === -1 ? "warming_up" : decision === 1 ? "confirmed_anomaly" : "no_confirmed_anomaly";
      if (c.decision !== decision || c.status !== status) return "확인 불가 · 결과 형식 확인 필요";
      const label = decision === -1 ? "확인 대기" : decision === 1 ? "3구간 이상 확인 (비교 판정)" : "3구간 이상 미확인 (정상 확정 아님)";
      return label + " · 유효 " + c.validWindows + "/3 · 초과 " + c.anomalyHits + "/3" + (c.resetReason ? " · 초기화: " + c.resetReason : "");
    }
    function renderRF66Card(result) {
      const card = document.createElement("section");
      const title = document.createElement("h3");
      title.textContent = result.deviceId + " / " + result.assetId;
      card.appendChild(title);
      const model = result.configuredModel;
      const configured = document.createElement("p");
      configured.textContent = result.runtimeStatus === "disabled"
        ? "기존 RF66 연결 중지 · 모델 로딩·자동 추론·자동 이벤트/알림을 실행하지 않습니다. 아래는 과거 저장 이력입니다."
        : model
        ? "현재 적용: RF66 (Random Forest) · " + model.modelVersion + " · 임계값 " + model.threshold + " (초과 시 이상 후보)"
        : "현재 RF66 모델 미설정 · 아래 기록이 있으면 과거 결과입니다.";
      configured.style.overflowWrap = "anywhere";
      card.appendChild(configured);
      const items = [...result.items].sort((a,b) => Date.parse(b.window.timestamp) - Date.parse(a.window.timestamp));
      const recent = items.find(rf66Completed);
      const summary = document.createElement("p");
      summary.textContent = recent
        ? "최근 판정 대상 구간: " + formatLocalTime(recent.window.timestamp) + " · " + (recent.analysis.verdict ? "이상 후보" : "정상 후보 (임계값 이내)") + (recent.analysis.modelVersion !== model?.modelVersion ? " · 과거 모델 결과" : "")
        : "완료된 판정 없음";
      card.appendChild(summary);
      const freshness = document.createElement("p");
      freshness.textContent = items.length
        ? "최근 원시 구간: " + formatLocalTime(items[0].window.timestamp) + " · 저장 이력이며 현재 실시간 수신을 보장하지 않습니다."
        : (model ? "원시 구간 입력 대기 · 모델은 준비됐지만 저장된 원시 구간이 없습니다." : "저장된 원시 구간이 없습니다.");
      card.appendChild(freshness);
      const eventMode = document.createElement("p");
      eventMode.textContent = "이벤트 운영 모드: " + ({disabled:"기존 RF66 자동 운영 중지",shadow:"비교만 · 이벤트/알림 미생성",events:"이벤트 기록 · 알림 꺼짐",alerts:"이벤트 기록 + 알림 활성화 (정책·입력 유효성 적용)"}[result.eventPolicy?.mode] || "미확인 (이전 서버)");
      card.appendChild(eventMode);
      if (items.length) {
        const rows = items.slice(0, 5).map(item => {
          const a = item.analysis || {}, valid = rf66Completed(item);
          const labels = {queued:"처리 대기",waiting_model:"모델 대기",unavailable:"판정 불가"};
          const label = valid ? (a.verdict ? "이상 후보" : "정상 후보 (임계값 이내)") : (labels[a.status] || "판정 불가 · 결과 형식 확인 필요");
          return [formatLocalTime(item.window.timestamp), label, valid ? String(a.score) : "—",
            valid ? String(a.threshold) : "—", item.window.quality,
            a.reason || "—", (a.modelVersion || "—") + (a.modelVersion && a.modelVersion !== model?.modelVersion ? " (과거 모델)" : ""), rf66Confirmation(item)];
        });
        const scroll = document.createElement("div");
        scroll.className = "table-scroll";
        scroll.style.overflowWrap = "anywhere";
        scroll.appendChild(statusTable("최근 구간 결과 · 점수 0~1 (현장 고장 확률 아님)",
          ["측정 시각","단일 구간 판정","RF66 점수","임계값","입력 품질","사유","결과의 모델 버전","연속 3구간 확인"], rows));
        card.appendChild(scroll);
      }
      return card;
    }
    async function loadRF66(devices, query) {
      clearRF66();
      if (!["device:read","telemetry:read","model:read"].every(can)) {
        $("rf66Status").textContent = "RF66 조회 권한이 없습니다.";
        return;
      }
      const generation = rf66Generation, session = token;
      const current = () => generation === rf66Generation && session === token
        && selectedSite()?.id === query.siteId && $("assetSelect").value === query.assetId
        && ["device:read","telemetry:read","model:read"].every(can);
      const selected = devices.filter(d => d.assetId === query.assetId
        && (!d.siteId || d.siteId === query.siteId));
      if (!selected.length) {
        $("rf66Status").textContent = "선택 설비에 조회 가능한 장치가 없습니다.";
        return;
      }
      $("rf66Status").textContent = "RF66 결과 조회 중 · 조회 기간 필터와 별개인 최근 저장 구간입니다.";
      const cards = await Promise.all(selected.map(async device => {
        const id = device.deviceId || device.id;
        try {
          const result = await api(`/api/devices/${encodeURIComponent(id)}/raw-vibration-windows`);
          if (!current()) return null;
          if (!Object.hasOwn(result, "configuredModel") || result.deviceId !== id || result.siteId !== query.siteId || result.assetId !== query.assetId
              || !Array.isArray(result.items) || result.items.some(item =>
                !item?.window || item.window.deviceId !== id || item.window.siteId !== query.siteId
                || item.window.assetId !== query.assetId || !Number.isFinite(Date.parse(item.window.timestamp)))) {
            throw new Error("장치·설비 매핑 또는 결과 형식 불일치");
          }
          const m = result.configuredModel;
          if (m && (m.modelType !== "random_forest" || !rf66Number(m.threshold)
              || !/^sha256:[0-9a-f]{64}$/.test(m.modelVersion || "")
              || m.featureProfileId !== "mcc5-vibration-800hz-spectral66-v1"
              || m.comparison !== ">" || m.mode !== "shadow" || m.affectsAlerts !== false)) {
            throw new Error("RF66 모델 설정 응답 확인 필요");
          }
          return renderRF66Card(result);
        } catch (error) {
          if (!current()) return null;
          const card = document.createElement("p");
          card.textContent = id + " · RF66 조회 실패: " + error.message + " · 로그인·권한·서버 API를 확인하고 새로고침하세요.";
          return card;
        }
      }));
      if (!current()) return;
      $("rf66Rows").replaceChildren(...cards.filter(Boolean));
      $("rf66Status").textContent = "최근 저장 구간 · 정상 후보는 장비 정상 확정이 아닙니다. 장치별 조회 상태를 확인하세요.";
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
      else {
        panel.appendChild(statusTable("장치별 수집 상태", ["장치 / 설비","연결","최근 수신","RSSI","재부팅","버퍼","센서 / 활성 오류"], devices.map(health => [
          `${displayValue(health.deviceId || health.id)} / ${displayValue(health.assetId)}`,
          statusLabel(health.health || health.status), formatLocalTime(health.lastReceivedAt),
          measuredValue(health.rssiDbm," dBm"), measuredValue(health.rebootCount,"회"), measuredValue(health.bufferUsagePct,"%"),
          `${statusLabel(health.sensorHealth)} / ${Array.isArray(health.activeSensorFaults) ? health.activeSensorFaults.length + "건" : "미수신"}`,
        ])));
        panel.appendChild(rawDetails("장치별 상세 정보·고장 기록", devices));
      }
      const servicePanel = $("serviceHealth"); servicePanel.replaceChildren();
      if (!dependencies || dependencies.error) servicePanel.textContent = dependencies?.error || "서비스 상태 조회 권한이 없습니다.";
      else {
        servicePanel.appendChild(facts([
          ["서비스 종합 상태",statusLabel(dependencies.status)], ["조회 시각",formatLocalTime(dependencies.checkedAt)],
          ["격리된 메시지",measuredValue(dependencies.quarantinedMessageCount,"건")],
        ]));
        const rows = dependencies.dependencies || [];
        if (rows.length) servicePanel.appendChild(statusTable("서비스별 상태", ["서비스","상태","응답 지연","오류율","최근 장애","최근 복구","영향 범위"], rows.map(item => [
          item.name || item.id, statusLabel(item.status), measuredValue(item.latencyMs," ms"), measuredValue(item.errorRatePct,"%"),
          formatLocalTime(item.lastFailureAt), formatLocalTime(item.lastRecoveryAt), item.impactScope,
        ])));
        servicePanel.appendChild(rawDetails("수집 지표·서비스 상태 원문", dependencies));
      }
    }

    function displayValue(value) {return value === null || value === undefined || value === "" ? "—" : String(value);}
    function measuredValue(value, unit) {const number = finiteNumber(value); return number === null ? "미수신" : `${number}${unit}`;}
    function statusLabel(value) {
      const labels = {online:"온라인",offline:"오프라인",healthy:"정상",ready:"준비",degraded:"성능 저하",fault:"고장",unknown:"상태 미수신"};
      return Object.hasOwn(labels,value) ? labels[value] : displayValue(value);
    }
    function reviewLabel(value) {
      const labels = {needs_review:"검수 필요",normal_false_positive:"정상 / 오탐",confirmed_anomaly:"이상 확인",sensor_issue:"센서 문제",repair_completed:"조치 완료"};
      return Object.hasOwn(labels,value) ? labels[value] : displayValue(value);
    }
    function facts(entries) {
      const list = document.createElement("dl"); list.className = "facts";
      for (const [label,value] of entries) {
        const group = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd");
        term.textContent = label; description.textContent = displayValue(value);
        group.append(term,description); list.appendChild(group);
      }
      return list;
    }
    function statusTable(caption, headings, rows) {
      const table = document.createElement("table"); table.className = "status-table";
      const title = document.createElement("caption"); title.textContent = caption;
      const head = document.createElement("thead"), header = document.createElement("tr"), body = document.createElement("tbody");
      for (const heading of headings) {const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = heading; header.appendChild(cell);}
      head.appendChild(header);
      for (const values of rows) {
        const row = document.createElement("tr");
        for (const value of values) {const cell = document.createElement("td"); cell.textContent = displayValue(value); row.appendChild(cell);}
        body.appendChild(row);
      }
      table.append(title,head,body); return table;
    }
    function rawDetails(label, value) {
      const details = document.createElement("details"), title = document.createElement("summary"), content = document.createElement("pre");
      title.textContent = label; content.textContent = JSON.stringify(value,null,2);
      details.append(title,content); return details;
    }

    function renderNotifications(rows) {
      const panel = $("notifications");
      panel.replaceChildren();
      if (!rows.length) panel.textContent = "No notifications.";
      rows.forEach(row => {
        const item = document.createElement("p");
        const verifier = row.event.snapshotEvidence?.inference?.model?.contractId === "history-event-verifier-v1";
        const transition = row.event.source === "snapshot" ? (verifier
          ? (row.event.snapshotTransition === "closed" ? "[이력 모델 기준 미초과 · 사건 해제] " : "[이력 모델 기준 초과 · 이상 후보] ")
          : (row.event.snapshotTransition === "closed" ? "[단건 모델 정상 복귀] " : "[단건 모델 이상 발생] ")) : "";
        item.textContent = `${row.isTest ? "[TEST] " : ""}${row.event.isSynthetic ? "[SYNTHETIC] " : ""}${transition}${row.event.title} · ${new Date(row.deliveredAt).toLocaleString()}`;
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
        meta.append(pill, ` ${formatLocalTime(event.occurredAt)} - ` + eventModelSummary(event));
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
      $("eventDetail").textContent = "목록에서 이벤트를 선택하세요.";
      $("labelSelect").value = "needs_review";
      $("noteInput").value = "";
      $("reviewReason").value = "";
      $("eventEvidence").textContent = "";
      $("evidenceSummary").replaceChildren();
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
      if (timestamp === null || timestamp === undefined || timestamp === "") return "—";
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

    function rpmValue(point) {
      if (Object.hasOwn(point, "rpmStatus") && point.rpmStatus !== "valid") return null;
      return finiteNumber(point.rpm);
    }

    function rpmDescription(point) {
      const status = {valid:"유효 보고값", unavailable:"미취득", stale:"오래된 측정", invalid:"무효"}[point.rpmStatus]
        ?? (Object.hasOwn(point, "rpmStatus") ? "알 수 없는 상태" : "상태정보 없음");
      const value = rpmValue(point);
      const observation = Object.hasOwn(point, "rpmStatus")
        ? ` · RPM 측정 시각: ${point.rpmMeasuredAt ?? "없음"} · RPM 출처: ${point.rpmSource ?? "미설정"}` : "";
      return `RPM: ${value === null ? "미수신" : value} (${status})${observation}`;
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
        ["RPM (보고값)", "#b7791f", p => rpmValue(p), null, null],
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

    function eventModelSummary(event) {
      if (event.source === "snapshot") return `단건 모델 발생 점수 ${event.snapshotScore} (통계 점수 아님) · ${event.modelVersion} · ${event.status} · `
        + ({observing:"이상 관측 중",recovered:"정상 판정으로 복귀",unknown:"관측 불명 (자동 복귀 아님)",operator_resolved:"작업자 수동 해제"}[event.snapshotObservation] || "관측 미확인");
      if (event.source === "rf66") return `RF66 ${event.rf66Score} · ${event.status} · ${event.rf66Observation}`;
      return `점수 ${event.maxScore ?? event.score ?? "-"}`;
    }
    function rf66ResolutionControls(event, current) {
      const section = document.createElement("section");
      if (!["rf66","snapshot"].includes(event.source) || event.status !== "open" || !can("event:review")) return section;
      const label = event.source === "snapshot" ? "단건 모델" : "RF66";
      const reason = document.createElement("textarea"), button = document.createElement("button"), status = document.createElement("p");
      reason.setAttribute("aria-label", label + " 수동 해제 사유");
      reason.setAttribute("maxlength", "500");
      reason.placeholder = "모델 교체·점검 등 수동 해제 사유 (센서 정상 복귀와 별도 기록)";
      button.textContent = label + " 이벤트 수동 해제";
      button.addEventListener("click", async () => {
        if (button.disabled || !current() || !can("event:review")) return;
        const value = reason.value.trim();
        if (!value || value.length > 500) {status.textContent = "해제 사유를 1~500자로 입력하세요."; return;}
        if (!confirm("이 이벤트를 수동 해제할까요? 정상 복귀 알림은 보내지 않으며 작업자와 사유가 기록됩니다.")) return;
        button.disabled = true;
        try {
          await api(`/api/events/${encodeURIComponent(event.id)}/${event.source}-resolve`, {method:"POST",body:JSON.stringify({reason:value})});
          if (current()) status.textContent = "수동 해제 저장 완료 · 목록은 다음 새로고침에 반영됩니다.";
        } catch (error) {
          if (current()) {status.textContent = "해제 실패: " + error.message; button.disabled = false;}
        }
      });
      section.append(reason, button, status);
      return section;
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
      detail.append(title, document.createElement("br"), `${formatLocalTime(event.occurredAt)} · ${event.duration ?? "-"} · ` + eventModelSummary(event), document.createElement("br"), event.note || "");
      const missing = document.createElement("p");
      detail.appendChild(rf66ResolutionControls(event, () => generation === detailGeneration && selectedEventId === eventId));
      missing.className = "notice";
      missing.textContent = event.source === "snapshot" ? "서버 모델 사건입니다. 발생·최근·해제 판정의 식별자, 무결성 해시와 모델 조건을 보존합니다. 이력 모델은 과거 24개 기록의 근거도 포함합니다. RF66 3구간 정책과 별개입니다."
        : response.context.rawDataMissing ? "요약 시계열 없음: 보관기간 만료 또는 미수신. 보존된 특징·버전만 표시합니다." : `전후 요약 시계열 ${response.context.points.length}건 · 출처 ${response.context.source} (원시 파형은 별도 분석 기록에서 확인)`;
      detail.appendChild(missing);
      if (response.analysis) {
        const analysis = document.createElement("section");
        detail.appendChild(analysis);
        renderAnalysisRows(analysis,response.analysis,()=>detailGeneration===generation && selectedEventId===eventId);
      }
      const installation = response.installationSnapshot;
      if (installation?.status === "recorded") {
        detail.appendChild(facts([["설치 정보 기준 시각",formatLocalTime(installation.effectiveAt)]]));
        for (const point of installation.points || []) detail.appendChild(facts([
          ["설치점",point.id],["위치",point.position],["방향",point.orientation],
          ["고정 방식",point.mountingMethod],["음향 방향",point.acousticDirection],
          ["사진 참조",(point.photoRefs || []).join(" · ")],["설치 버전 시각",formatLocalTime(point.versionAt)]
        ]));
      } else detail.appendChild(facts([["발생 당시 설치 정보","미확인 — 기록된 과거 설치 정보 없음"]]));
      $("eventEvidence").textContent = JSON.stringify({context:{from:response.context.from, to:response.context.to, source:response.context.source}, featureSnapshot:response.featureSnapshot, appliedRule:response.appliedRule, modelVersion:response.modelVersion, deviceSnapshot:response.deviceSnapshot,
        ...(event.source === "snapshot" ? {lastEvidence:event.snapshotLastEvidence,recoveryEvidence:event.snapshotRecoveryEvidence,resolution:event.snapshotResolution,observationReason:event.snapshotObservationReason} : {})}, null, 2);
      const rule = response.appliedRule || {};
      $("evidenceSummary").replaceChildren(facts([
        ["이벤트 ID",event.id], ["모델 버전",response.modelVersion], ["규칙 버전",rule.version || rule.policyId],
        ["장치",response.deviceSnapshot?.deviceId || response.deviceSnapshot?.id],
        ["진입 점수 임계값",event.source === "snapshot" ? `${event.snapshotEvidence?.comparison} ${event.snapshotThreshold} (${event.snapshotEvidence?.scoreType})` : event.source === "rf66" ? event.rf66Threshold : rule.scoreThreshold],
        ["규칙 지속시간",event.source === "snapshot" ? "서버 이상 1건 발생 / 같은 모델 정상 1건 복귀" : event.source === "rf66" ? "발생/복귀 각 3구간" : measuredValue(rule.durationSec,"초")],
        ["규칙 활성",rule.active === true ? "활성" : rule.active === false ? "비활성" : "미수신"],
        ["신호 출처",response.context.source],
      ]));
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
        const row = document.createElement("section"); row.className = "history-card";
        const title = document.createElement("b"); title.textContent = `${formatLocalTime(item.changedAt)} · ${displayValue(item.actor?.name || item.actor?.id)}`;
        row.append(title, facts([["이전 라벨",reviewLabel(item.before?.label)],["변경 라벨",reviewLabel(item.after?.label)],["변경 사유",item.reason],["변경 메모",item.after?.note]]), rawDetails("검수 기록 원문",item));
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
        const row = document.createElement("section"); row.className = "history-card";
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
      setView(currentView === "management" && !hasManagementAccess() ? "overview" : currentView);
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
      const panel = $("auditRows"); panel.replaceChildren();
      if (!result.items.length) panel.textContent = "감사 기록이 없습니다.";
      else {
        panel.appendChild(statusTable("변경 감사 기록", ["변경 시각","작성자","작업","대상","변경 사유"], result.items.map(item => [
          formatLocalTime(item.changedAt), item.actor?.name || item.actor?.id, item.action,
          `${displayValue(item.targetType)} / ${displayValue(item.targetId)}`, item.reason,
        ])));
        panel.appendChild(rawDetails("변경 전·후 원문",result.items));
      }
      renderPager("audit", result.page, result.total);
    }

    function modelScope() {return JSON.stringify([token, $("siteSelect").value, $("assetSelect").value]);}
    function modelReviewButtons() {
      const disabled = modelReviewBusy || !can("model:review") || !modelReviewRow?.submission || modelReviewRow.approvalStatus !== "pending";
      $("modelApprove").disabled = disabled; $("modelReject").disabled = disabled;
      $("modelReviewReason").disabled = disabled;
      $("modelStatusFilter").disabled = modelReviewBusy; $("modelQueueLoad").disabled = modelReviewBusy;
    }
    function clearModelReview() {
      modelReviewGeneration++; modelReviewRow = null; pendingModelReview = null;
      $("modelReviewSummary").textContent = "검토할 결과를 선택하세요.";
      $("modelReviewEvidence").textContent = ""; $("modelReviewReason").value = "";
      $("modelReviewStatus").textContent = ""; modelReviewButtons();
    }
    function renderModelQueue() {
      $("modelQueue").replaceChildren();
      if (!modelQueueRows.length) $("modelQueue").textContent = "해당 조건의 등록 결과가 없습니다.";
      for (const row of modelQueueRows) {
        const button = document.createElement("button"); button.className = "secondary";
        button.textContent = `${row.version} · ${row.approvalStatus || "pending"} · ${row.submission?.jobId || "수동 메타데이터"}`;
        button.addEventListener("click", () => act(() => selectModelReview(row.version)));
        $("modelQueue").append(button);
      }
      $("modelQueuePage").textContent = `${modelQueuePage} / ${Math.max(1,Math.ceil(modelQueueTotal / PAGE_SIZE))} · ${modelQueueTotal}건`;
      $("modelQueuePrev").disabled = modelQueuePage <= 1;
      $("modelQueueNext").disabled = modelQueuePage * PAGE_SIZE >= modelQueueTotal;
    }
    let modelQueueFilter = "draft";
    async function loadModelQueue(reset = false, requestedPage = null) {
      if (modelReviewBusy || !can("model:read")) return;
      if ($("modelReviewReason").value.trim() && !confirm("작성 중인 AI 검토 사유를 버리고 목록을 다시 조회할까요?")) {$("modelStatusFilter").value = modelQueueFilter; return;}
      if (reset) modelQueuePage = 1;
      else if (requestedPage !== null) modelQueuePage = requestedPage;
      modelQueueFilter = $("modelStatusFilter").value;
      const generation = ++modelQueueGeneration, scope = modelScope();
      clearModelReview(); modelQueueRows = []; modelQueueTotal = 0; renderModelQueue();
      $("modelQueue").textContent = "등록 결과를 불러오는 중입니다.";
      const query = new URLSearchParams({siteId:$("siteSelect").value,assetId:$("assetSelect").value,status:$("modelStatusFilter").value,page:modelQueuePage,size:PAGE_SIZE});
      if (!query.get("siteId") || !query.get("assetId")) {$("modelQueue").textContent = "사이트와 설비를 선택하세요."; return;}
      try {
        const page = await api(`/api/model-versions?${query}`);
        if (generation !== modelQueueGeneration || scope !== modelScope()) return;
        const last = Math.max(1, Math.ceil(page.total / PAGE_SIZE));
        if (modelQueuePage > last) {modelQueuePage = last; return loadModelQueue();}
        modelQueueRows = page.items; modelQueueTotal = page.total; renderModelQueue();
      } catch (error) {
        if (generation !== modelQueueGeneration || scope !== modelScope()) return;
        $("modelQueue").textContent = `목록 조회 실패: ${error.message}`;
        $("modelQueuePage").textContent = "조회 실패";
      }
    }
    function renderModelReview() {
      const row = modelReviewRow;
      if (!row) return;
      $("modelReviewSummary").textContent = `${row.version} · ${row.approvalStatus} · ${row.deploymentStatus} / ${row.artifactVerified ? "파일 검증됨" : "산출물 참조·제출자 체크섬 (서버 파일 검증 아님)"}`;
      $("modelReviewEvidence").textContent = JSON.stringify({submission:row.submission, artifactUri:row.artifactUri, artifactChecksum:row.artifactChecksum, registrationDigest:row.registrationDigest, dataset:row.datasetSnapshot, baseline:row.baselineSnapshot, metrics:row.metrics, domainGap:row.domainGap, fieldCalibrationPlan:row.fieldCalibrationPlan, errorCases:row.errorCases, limitations:row.limitations, reviewHistory:row.reviewHistory || []}, null, 2);
      modelReviewButtons();
    }
    async function selectModelReview(version) {
      if (modelReviewBusy) return;
      if (modelReviewRow?.version === version) return;
      if ($("modelReviewReason").value.trim() && !confirm("작성 중인 AI 검토 사유를 버리고 다른 결과를 선택할까요?")) return;
      clearModelReview();
      const generation = modelReviewGeneration, scope = modelScope();
      $("modelReviewStatus").textContent = "등록 근거를 불러오는 중입니다.";
      try {
        const row = await api(`/api/model-versions/${encodeURIComponent(version)}`);
        if (generation !== modelReviewGeneration || scope !== modelScope()) return;
        if (row.version !== version) throw new Error("선택 결과와 응답 버전이 다릅니다.");
        modelReviewRow = row; renderModelReview();
        $("modelReviewStatus").textContent = row.submission ? (can("model:review") ? "사유를 작성하고 승인 또는 반려를 기록하세요." : "읽기 전용입니다. 승인·반려 권한이 필요합니다.") : "기존 수동 메타데이터입니다. 체크섬과 AI1 인계 근거를 갖춘 새 버전을 등록해야 검토할 수 있습니다.";
      } catch (error) {
        if (generation === modelReviewGeneration && scope === modelScope()) $("modelReviewStatus").textContent = `상세 조회 실패: ${error.message}`;
      }
    }
    async function submitModelReview(decision) {
      const row = modelReviewRow, reason = $("modelReviewReason").value.trim();
      if (modelReviewBusy || !row?.submission || !can("model:review") || row.approvalStatus !== "pending") return;
      if (!reason) {$("modelReviewStatus").textContent = "승인·반려 사유를 입력하세요."; return;}
      if (!pendingModelReview || pendingModelReview.version !== row.version || pendingModelReview.payload.reason !== reason || pendingModelReview.payload.decision !== decision) {
        pendingModelReview = {version:row.version, payload:{decision,reason,requestId:crypto.randomUUID(),expectedRevision:row.approvalRevision,registrationDigest:row.registrationDigest}};
      }
      const intent = pendingModelReview, generation = modelReviewGeneration, scope = modelScope();
      modelReviewBusy = true; modelReviewButtons(); $("modelReviewStatus").textContent = "검토 결과를 저장하는 중입니다.";
      try {
        const result = await api(`/api/model-versions/${encodeURIComponent(row.version)}/reviews`, jsonOptions("POST", intent.payload));
        if (generation !== modelReviewGeneration || scope !== modelScope()) return;
        if (result.version !== row.version || result.registrationDigest !== row.registrationDigest) throw new Error("검토 응답의 등록 근거가 일치하지 않습니다.");
        // Confirm the write first; do not turn a subsequent list failure into a duplicate review.
        modelReviewRow = result; pendingModelReview = null; $("modelReviewReason").value = "";
        modelQueueGeneration++;
        modelQueueRows = modelQueueRows.map(item => item.version === result.version ? result : item);
        if ($("modelStatusFilter").value && $("modelStatusFilter").value !== result.status) {
          modelQueueRows = modelQueueRows.filter(item => item.version !== result.version); modelQueueTotal = Math.max(0,modelQueueTotal - 1);
        }
        renderModelQueue(); renderModelReview();
        $("modelReviewStatus").textContent = `${result.approvalStatus === "approved" ? "승인이" : "반려가"} 기록됐습니다. 운영 배포는 수행하지 않았습니다.`;
      } catch (error) {
        if (generation === modelReviewGeneration && scope === modelScope()) $("modelReviewStatus").textContent = `저장 결과 확인 실패: ${error.message}. 같은 내용으로 재시도하거나 목록을 다시 조회해 상태를 확인하세요.`;
      } finally {modelReviewBusy = false; modelReviewButtons();}
    }

    function opsKey() {return JSON.stringify([token,$("siteSelect").value,$("assetSelect").value,opsDevice?.id]);}
    function opsScopeMatches(row) {
      return !!row && !!opsDevice && row.siteId === opsDevice.siteId && row.assetId === opsDevice.assetId &&
        row.siteId === $("siteSelect").value && row.assetId === $("assetSelect").value;
    }
    function opsConfigScopeMatches(config) {
      return !!config && config.deviceId === opsDevice?.id && opsScopeMatches(config) &&
        [config.desired,config.lastApplied].every(command => command == null || opsScopeMatches(command));
    }
    function opsCanEdit() {
      return opsConfigScopeMatches(opsConfig) && can("device:read") && can("parameter:write") &&
        opsDevice.mappingStatus === "active" && opsDevice.certificateStatus === "registered" &&
        [opsDevice.id,opsDevice.siteId,opsDevice.assetId].every(id => /^[A-Z0-9][A-Z0-9_.-]{0,62}$/.test(id));
    }
    function opsControls() {
      const edit = opsCanEdit() && !opsSaving && !opsUncertain;
      for (const id of ["opsInterval","opsReplay","opsHealthInterval","opsConfigReason"]) $(id).disabled = !edit;
      $("opsConfigPublish").disabled = !edit;
      $("opsConfigCancel").disabled = !edit || !opsDirty;
      $("opsConfigLoad").disabled = !opsDevice || opsSaving;
      $("opsDevice").disabled = opsSaving || !opsDevices.length;
      $("opsReload").disabled = opsSaving;
      $("opsQualityLoad").disabled = !opsDevice;
      $("opsHistoryLoad").disabled = !opsDevice;
      $("opsAnalysisLoad").disabled = !opsDevice || analysisBusy || !can("telemetry:read");
      $("opsWindowsLoad").disabled = !opsDevice || !can("telemetry:read") || !can("model:read");
      $("opsAnalysisRequest").disabled = !opsDevice || analysisBusy || !can("telemetry:read") || !can("device:write") || opsDevice.mappingStatus !== "active";
      $("opsAnalysisReason").disabled = $("opsAnalysisRequest").disabled;
    }
    function clearOpsDevice() {
      windowsGeneration++;
      $("opsWindowsRows").replaceChildren(); $("opsWindowsStatus").textContent="";
      analysisGeneration++; analysisBusy = false; analysisIntent = null;
      for (const id of ["opsAnalysisRows","opsWaveform"]) $(id).replaceChildren();
      $("opsAnalysisReason").value = ""; $("opsAnalysisStatus").textContent = "";
      opsGeneration++; opsConfigGeneration++; opsQualityGeneration++; opsHealthGeneration++; opsHistoryGeneration++;
      opsDevice = null; opsConfig = null; opsQuality = null; opsHistory = []; opsQualityPage = 1; opsHistoryPage = 1;
      opsDirty = false; opsUncertain = false; opsIntent = null;
      for (const id of ["opsIdentity","opsHealth","opsConfigState","opsQualitySummary","opsQualityRows","opsHistoryRows"]) $(id).replaceChildren();
      for (const id of ["opsInterval","opsReplay","opsHealthInterval","opsConfigReason"]) $(id).value = "";
      for (const id of ["opsStatus","opsConfigStatus","opsQualityStatus","opsHistoryStatus","opsQualityPage","opsHistoryPage"]) $(id).textContent = "";
      for (const id of ["opsQualityPrev","opsQualityNext","opsHistoryPrev","opsHistoryNext"]) $(id).disabled = true;
      opsControls();
    }
    function renderAnalysisRows(panel,response,stillCurrent) {
      panel.replaceChildren();
      if (response.shadowInference) {
        const shadow=response.shadowInference, model=shadow.checkpoint;
        const statuses={queued:"처리 대기",warming_up:"연속 구간 대기",not_evaluated:"판정 불가",inferred:"비교 추론 완료"};
        const rawState=shadow.rawInputStatus==="configured_unverified" ? "원시 변환 설정됨 · 학습 호환성 미검증" : "판정 불가 · 모델 전처리 미연결";
        panel.appendChild(facts([["모델 비교",`${model.modelType} · ${model.modelVersion}`],["모델 입력",`${model.featureCount}개 특징 / ${model.sequenceLength}개 연속 구간`],["현재 원시 신호",rawState],["적용 범위","비교용 · 기존 이벤트/알림 변경 없음 · 현장 성능 미검증"]]));
        if (shadow.preprocessing) panel.appendChild(facts([["전처리 버전",shadow.preprocessing.preprocessingId],["입력 조건",`${shadow.preprocessing.profile.channel} / ${shadow.preprocessing.profile.unit} / ${shadow.preprocessing.profile.sampleRateHz}Hz / ${shadow.preprocessing.profile.windowSamples}개 표본`]]));
        for (const item of shadow.items || []) {
          const verdict=item.status==="inferred" && typeof item.verdict==="boolean" ? (item.verdict?"이상 후보":"임계값 이내") : "판정 불가";
          panel.appendChild(facts([["입력 시각",formatLocalTime(item.timestamp)],["입력 종류",item.inputKind==="raw"?"원시 파형 변환":"준비된 특징"],["모델 버전",item.modelVersion],["전처리 버전",item.preprocessing?.preprocessingId || item.preprocessingId || "—"],["처리 상태",statuses[item.status] || "판정 불가"],["비교 판정",verdict],["재구성 오차",item.reconstructionError ?? "미산출"],["임계값",item.threshold ?? "미적용"],["상태 사유",item.reason || "—"]]));
        }
      }
      for (const request of response.requests || []) panel.appendChild(facts([["파형 요청",request.id],["상태",request.status],["사유",request.reason]]));
      if (!response.items?.length) panel.appendChild(facts([["분석 기록","미수신 또는 보관기간 만료"]]));
      for (const row of response.items || []) {
        const card=document.createElement("section"); card.className="history-card";
        card.appendChild(facts([["측정 시각",formatLocalTime(row.timestamp)],["특징 버전",row.featureVersion],["원시 파형",row.hasWaveform?"보존됨":"요약 특징만 보존"]]));
        card.appendChild(rawDetails("신호별 특징",row.channels));
        if (row.hasWaveform) {
          const button=document.createElement("button"); button.textContent="파형 보기 · 분석 파일";
          const preview=document.createElement("section");
          button.addEventListener("click",async()=>{
            button.disabled=true;
            try {
              const {body:result,original}=await api(`/api/analysis/${encodeURIComponent(row.id)}`,{},true);
              if (!stillCurrent()) return;
              if (result.frame.deviceId!==row.deviceId || result.frame.siteId!==row.siteId || result.frame.assetId!==row.assetId) throw new Error("파형 대상 불일치");
              preview.replaceChildren();
              for (const [name,channel] of Object.entries(result.frame.channels)) {
                if (!channel.samplesFloat32LE) continue;
                const bytes=Uint8Array.from(atob(channel.samplesFloat32LE),c=>c.charCodeAt(0));
                if (bytes.length!==channel.sampleCount*4 || channel.sampleCount>16384) throw new Error("파형 길이 오류");
                const view=new DataView(bytes.buffer), values=Array.from({length:channel.sampleCount},(_,i)=>view.getFloat32(i*4,true));
                if (!values.every(Number.isFinite)) throw new Error("유효하지 않은 파형");
                preview.appendChild(facts([["채널",name],["단위",channel.unit],["샘플링",`${channel.sampleRateHz}Hz / ${channel.sampleCount}개`]]));
                const canvas=document.createElement("canvas"); canvas.width=640; canvas.height=120; canvas.style.width="100%";
                const ctx=canvas.getContext("2d"), lo=Math.min(...values), hi=Math.max(...values), span=hi-lo||1;
                ctx.strokeStyle="#205b9f"; ctx.beginPath();
                for(let x=0;x<640;x++) {
                  const bucket=values.slice(Math.floor(x*values.length/640),Math.max(Math.floor((x+1)*values.length/640),Math.floor(x*values.length/640)+1));
                  ctx.moveTo(x,110-(Math.min(...bucket)-lo)/span*100); ctx.lineTo(x,110-(Math.max(...bucket)-lo)/span*100);
                }
                ctx.stroke(); preview.appendChild(canvas);
              }
              const download=document.createElement("button"); download.textContent="분석 JSON 저장 (전체 샘플 · 미라벨)";
              download.addEventListener("click",()=>{
                // Keep the server's numeric representation for the AI1 checksum.
                const url=URL.createObjectURL(new Blob([original],{type:"application/json"}));
                const link=document.createElement("a"); link.href=url; link.download=`analysis-${row.id}.json`; link.click(); URL.revokeObjectURL(url);
              });
              preview.appendChild(download);
            } catch(error) {if(stillCurrent()) preview.textContent=`파형 조회 실패: ${error.message}`;}
            finally {if(stillCurrent()) button.disabled=false;}
          });
          card.append(button,preview);
        }
        panel.appendChild(card);
      }
    }
    async function loadOpsWindows() {
      if(!opsDevice || !can("telemetry:read") || !can("model:read")) return;
      const key=opsKey(), generation=++windowsGeneration;
      $("opsWindowsRows").replaceChildren(); $("opsWindowsStatus").textContent="조회 중…";
      try {
        const result=await api(`/api/devices/${encodeURIComponent(opsDevice.id)}/vibration-windows`);
        if(key!==opsKey() || generation!==windowsGeneration) return;
        if(result.deviceId!==opsDevice.id || !opsScopeMatches(result)) {rejectOpsScope();return;}
        const labels={queued:"처리 대기",waiting_model:"모델 대기",unavailable:"판정 불가",completed:"비교 판정 완료"};
        $("opsWindowsRows").appendChild(statusTable("구간별 수집·분석",["구간 시작","부팅 / 순번","품질","앞선 누락 구간","RMS X / Y / Z (g)","처리 상태","비교 판정"],(result.items||[]).map(item=>{
          const w=item.window, a=item.analysis, f=w.features;
          return [formatLocalTime(w.timestamp),`${w.bootId.slice(0,8)} / ${w.windowIndex}`,w.quality,item.missingWindowsBefore,
            f ? [f[0],f[7],f[14]].join(" / ") : "판정 불가",labels[a.status]||a.status,
            a.status==="completed" && typeof a.verdict==="boolean" ? (a.verdict?"이상 후보":"정상 후보") : "미판정"];
        })));
        $("opsWindowsRows").appendChild(rawDetails("21개 특징·모델 입력·판정 사유 원문",result));
        $("opsWindowsStatus").textContent=`최근 ${(result.items||[]).length}건 · ${result.retentionHours}시간 보관 · 비교용(알림 미반영)`;
      } catch(error) {if(key===opsKey() && generation===windowsGeneration) $("opsWindowsStatus").textContent=`조회 실패: ${error.message}`;}
    }
    async function loadOpsAnalysis(prefix="",cursor=null) {
      if(!opsDevice || !can("telemetry:read")) return;
      const key=opsKey(), generation=++analysisGeneration;
      $("opsAnalysisRows").replaceChildren();
      try {
        const result=await api(`/api/devices/${encodeURIComponent(opsDevice.id)}/analysis${cursor?`?cursor=${encodeURIComponent(cursor)}`:""}`);
        if(key!==opsKey() || generation!==analysisGeneration) return;
        if(result.deviceId!==opsDevice.id || !opsScopeMatches(result)) {rejectOpsScope();return;}
        renderAnalysisRows($("opsAnalysisRows"),result,()=>key===opsKey() && generation===analysisGeneration);
        if(result.nextCursor) {
          const next=document.createElement("button"); next.textContent="이전 분석 기록 100건";
          next.addEventListener("click",()=>{if(key===opsKey() && generation===analysisGeneration) loadOpsAnalysis("",result.nextCursor);});
          $("opsAnalysisRows").appendChild(next);
        }
        $("opsAnalysisStatus").textContent=prefix || "분석 기록 페이지당 100건 · 기본 7일 보관 (용량 상한 별도)";
      } catch(error) {if(key===opsKey() && generation===analysisGeneration) $("opsAnalysisStatus").textContent=`${prefix} 목록 조회 실패: ${error.message}`;}
    }
    async function requestOpsWaveform() {
      if(!opsDevice || analysisBusy || !can("device:write") || !can("telemetry:read")) return;
      const reason=$("opsAnalysisReason").value.trim(), key=opsKey();
      if(!reason || reason.length>1000) {$("opsAnalysisStatus").textContent="요청 사유를 입력하세요.";return;}
      if(!analysisIntent) {
        if(!confirm("이 장치의 다음 0.64초 원시 음향·진동 구간을 서버에 보존할까요?")) return;
        analysisIntent={siteId:opsDevice.siteId,assetId:opsDevice.assetId,reason,requestId:crypto.randomUUID().replaceAll("-","")};
      } else if(analysisIntent.reason!==reason) {$("opsAnalysisStatus").textContent="이전 요청의 결과가 불확실합니다. 같은 사유로 재시도하세요.";return;}
      analysisBusy=true; opsControls();
      try {
        const result=await api(`/api/devices/${encodeURIComponent(opsDevice.id)}/analysis/requests`,jsonOptions("POST",analysisIntent));
        if(key!==opsKey()) return;
        if(!opsScopeMatches(result) || result.requestId!==analysisIntent.requestId) throw new Error("요청 결과 대상 불일치");
        analysisIntent=null; $("opsAnalysisReason").value="";
        await loadOpsAnalysis(`요청 저장 완료 (${result.status}). 실제 파형 수신 여부를 확인하세요.`);
      } catch(error) {if(key===opsKey()) $("opsAnalysisStatus").textContent=`요청 결과 미확인: ${error.message}. 같은 사유로 재시도하면 기존 요청 ID를 재사용합니다.`;}
      finally {if(key===opsKey()) {analysisBusy=false;opsControls();}}
    }
    function invalidateDeviceOperations() {
      opsDevices = []; clearOpsDevice(); $("opsDevice").replaceChildren();
    }
    function rejectOpsScope() {
      const dirty = opsDirty, fields = ["opsInterval","opsReplay","opsHealthInterval","opsConfigReason"];
      const draft = fields.map(id => $(id).value);
      // Drop the cached device list and every in-flight generation. Only a fresh
      // list plus target selection may re-enable writes; a late GET cannot.
      invalidateDeviceOperations();
      if (dirty) {fields.forEach((id,index) => {$(id).value = draft[index];}); opsDirty = true;}
      $("opsStatus").textContent = "장치의 사이트·설비가 현재 화면과 다릅니다. 장치 목록을 새로고친 뒤 올바른 사이트·설비·장치를 다시 선택하세요.";
      $("opsConfigStatus").textContent = $("opsStatus").textContent;
    }
    function allowOpsChange() {
      return !opsSaving && (!opsDirty || confirm("작성 중인 장치 설정을 버리고 대상을 변경할까요?"));
    }
    async function loadDeviceOperations() {
      if (!can("device:read") || !allowOpsChange()) return;
      const previous = opsDevice?.id, scope = JSON.stringify([token,$("siteSelect").value,$("assetSelect").value]);
      invalidateDeviceOperations(); const generation = opsGeneration;
      const siteId = $("siteSelect").value, assetId = $("assetSelect").value;
      if (!siteId || !assetId) {$("opsStatus").textContent = "사이트와 설비를 선택하세요."; return;}
      $("opsStatus").textContent = "설비에 연결된 장치를 조회하는 중입니다.";
      try {
        const rows = await api(`/api/sites/${encodeURIComponent(siteId)}/devices`);
        if (generation !== opsGeneration || scope !== JSON.stringify([token,$("siteSelect").value,$("assetSelect").value])) return;
        if (!Array.isArray(rows)) throw new Error("장치 목록 형식이 올바르지 않습니다.");
        opsDevices = rows.filter(row => row.siteId === siteId && row.assetId === assetId);
        setOptions($("opsDevice"), opsDevices, row => row.id, row => `${row.id} · ${row.mappingStatus === "active" ? "활성" : "비활성"}`);
        const selected = opsDevices.find(row => row.id === previous) || opsDevices.find(row => row.mappingStatus === "active") || opsDevices[0];
        opsControls();
        if (!selected) {$("opsStatus").textContent = "이 설비에 연결된 장치가 없습니다. 운영 관리에서 장치를 등록하세요."; return;}
        $("opsDevice").value = selected.id; await selectOpsDevice(selected.id);
      } catch (error) {if (generation === opsGeneration) $("opsStatus").textContent = `장치 목록 조회 실패: ${error.message}`;}
    }
    async function selectOpsDevice(deviceId) {
      if (!allowOpsChange()) {$("opsDevice").value = opsDevice?.id || ""; return;}
      const row = opsDevices.find(item => item.id === deviceId);
      clearOpsDevice();
      if (!row) return;
      opsDevice = {...row}; $("opsDevice").value = row.id;
      $("opsIdentity").append(facts([["장치",row.id],["연결 설비",row.assetId],["매핑",row.mappingStatus === "active" ? "활성" : "비활성"],["인증서 상태",row.certificateStatus],["등록 펌웨어",row.firmwareVersion]]));
      $("opsStatus").textContent = "조회 시점의 보고값입니다. 설정 편집 중에도 상태·품질은 별도로 조회할 수 있습니다.";
      opsControls();
      await Promise.all([loadOpsHealth(),loadOpsQuality(),loadOpsConfig(),loadOpsHistory()]);
    }
    async function loadOpsHealth() {
      if (!opsDevice || !can("device:read")) return;
      const key = opsKey(), generation = ++opsHealthGeneration, id = opsDevice.id;
      $("opsHealth").replaceChildren(); $("opsHealth").textContent = "장치 상태 조회 중";
      try {
        const row = await api(`/api/devices/${encodeURIComponent(id)}/health`);
        if (key !== opsKey() || generation !== opsHealthGeneration) return;
        if (row.deviceId !== id || !opsScopeMatches(row)) {rejectOpsScope(); return;}
        $("opsHealth").replaceChildren(facts([["연결 상태",statusLabel(row.health || row.status)],["최근 수신",formatLocalTime(row.lastReceivedAt)],["RSSI",measuredValue(row.rssiDbm," dBm")],["재부팅",measuredValue(row.rebootCount,"회")],["버퍼 사용률",measuredValue(row.bufferUsagePct,"%")],["센서 상태",statusLabel(row.sensorHealth)]]), rawDetails("센서 고장·원본 상태",row));
      } catch (error) {if (key === opsKey() && generation === opsHealthGeneration) $("opsHealth").textContent = `장치 상태 조회 실패: ${error.message}`;}
    }
    function opsMetric(row, field, unit = "") {return row?.hasData ? measuredValue(row[field],unit) : "관측 없음";}
    function renderOpsQuality() {
      const q = opsQuality; $("opsQualityRows").replaceChildren();
      if (!q) return;
      $("opsQualityRows").append(statusTable("종료 시각 기준 통신 품질",["구간 (현지 시각)","관측 창","시도 / 실패 / 재시도","ACK 평균 / 최대","버퍼 표본 평균 / 최대","삭제","경계 초과 창"],q.items.slice((opsQualityPage - 1) * PAGE_SIZE,opsQualityPage * PAGE_SIZE).map(row => [
        `${formatLocalTime(row.from)} ~ ${formatLocalTime(row.to)}`,row.hasData ? `${row.windowCount}건` : "관측 없음",
        ["attempts","failures","retries"].map(field => opsMetric(row,field)).join(" / "),
        `${opsMetric(row,"ackLatencyMeanMs"," ms")} / ${opsMetric(row,"ackLatencyMaxMs"," ms")}`,
        `${opsMetric(row,"bufferDepthSampleMean")} / ${opsMetric(row,"bufferDepthMax")}`,opsMetric(row,"bufferDropped"),row.crossBoundaryWindows || 0,
      ])));
      renderPager("opsQuality",opsQualityPage,q.items.length);
    }
    async function loadOpsQuality() {
      if (!opsDevice || !can("device:read")) return;
      const key = opsKey(), generation = ++opsQualityGeneration, id = opsDevice.id;
      opsQuality = null; opsQualityPage = 1; $("opsQualitySummary").replaceChildren(); $("opsQualityRows").replaceChildren();
      $("opsQualityPage").textContent = ""; $("opsQualityPrev").disabled = true; $("opsQualityNext").disabled = true;
      $("opsQualityStatus").textContent = "통신 품질 조회 중";
      try {
        const hours = Number($("opsQualityRange").value), bucket = Number($("opsQualityBucket").value);
        const to = Date.now(), from = to - hours * 3600000, width = bucket * 1000;
        if (![1,6,24,168,720].includes(hours) || ![60,300,3600,86400].includes(bucket) || Math.floor((to - 1 - Math.floor(from / width) * width) / width) + 1 > 1000) throw new Error("최대 1,000개 구간까지 조회할 수 있습니다. 집계 간격을 늘려 주세요.");
        const query = new URLSearchParams({from:new Date(from).toISOString(),to:new Date(to).toISOString(),bucketSeconds:bucket});
        const q = await api(`/api/devices/${encodeURIComponent(id)}/communication-quality?${query}`);
        if (key !== opsKey() || generation !== opsQualityGeneration) return;
        if (q.deviceId !== id || !opsScopeMatches(q)) {rejectOpsScope(); return;}
        if (q.transport !== "http" || q.attribution !== "whole_window_at_end" || !q.summary || !Array.isArray(q.items)) throw new Error("통신 품질 응답의 집계 계약이 다릅니다.");
        opsQuality = q; const s = q.summary;
        $("opsQualityStatus").textContent = `${formatLocalTime(q.from)} ~ ${formatLocalTime(q.to)} · ${q.bucketSeconds}초 집계 · 조회 완료 ${formatLocalTime(new Date().toISOString())}${s.hasData ? "" : " · 수신된 관측 창이 없습니다. 정상 여부를 판단할 수 없습니다."}`;
        $("opsQualitySummary").append(facts([["전송 시도",opsMetric(s,"attempts","회")],["실패율",opsMetric(s,"failureRatePct","%")],["재시도",opsMetric(s,"retries","회")],["버퍼 재전송",opsMetric(s,"replayAttempts","회")],["성공 ACK",opsMetric(s,"acknowledged","회")],["ACK 가중 평균",opsMetric(s,"ackLatencyMeanMs"," ms")],["ACK 최대",opsMetric(s,"ackLatencyMaxMs"," ms")],["최신 버퍼 깊이 / 용량",`${opsMetric(s,"bufferDepthLast")} / ${opsMetric(s,"bufferCapacity")}`],["버퍼 삭제",opsMetric(s,"bufferDropped","건")],["관측 지속시간 합",s.hasData ? measuredValue(s.observedDurationMs / 1000,"초") : "관측 없음"],["최초 관측 시작",formatLocalTime(s.firstWindowStartedAt)],["마지막 창 종료",formatLocalTime(s.lastWindowEndedAt)]]),rawDetails("실패 분류·표본 카운터 원문",s));
        renderOpsQuality();
      } catch (error) {if (key === opsKey() && generation === opsQualityGeneration) $("opsQualityStatus").textContent = `통신 품질 조회 실패: ${error.message}`;}
    }
    function opsSettingsValid(settings) {
      return settings && Object.keys(settings).every(key=>["measurementIntervalMs","replayBatchSize","healthReportIntervalMs"].includes(key)) && Number.isInteger(settings.measurementIntervalMs) && settings.measurementIntervalMs >= 3000 && settings.measurementIntervalMs <= 60000 && Number.isInteger(settings.replayBatchSize) && settings.replayBatchSize >= 1 && settings.replayBatchSize <= 4 && (!Object.hasOwn(settings,"healthReportIntervalMs") || Number.isInteger(settings.healthReportIntervalMs) && settings.healthReportIntervalMs >= 10000 && settings.healthReportIntervalMs <= 300000);
    }
    function opsSameSettings(left, right) {return opsSettingsValid(left) && opsSettingsValid(right) && left.measurementIntervalMs === right.measurementIntervalMs && left.replayBatchSize === right.replayBatchSize && left.healthReportIntervalMs === right.healthReportIntervalMs;}
    function opsCommandLabel(state) {return ({pending:"적용 보고 대기",applied:"적용 성공 보고",failed:"실패 보고",rejected:"거부 보고"})[state] || (state ? "알 수 없는 보고 상태" : "발행 없음");}
    function opsSettingsText(settings) {return opsSettingsValid(settings) ? `대기 ${settings.measurementIntervalMs}ms / 재전송 ${settings.replayBatchSize}건 / 정기 상태 ${settings.healthReportIntervalMs == null ? "미지정" : settings.healthReportIntervalMs + "ms"}` : "보고 없음";}
    function renderOpsConfig() {
      $("opsConfigState").replaceChildren();
      if (!opsConfig) {opsControls(); return;}
      const c = opsConfig, desired = c.desired, applied = c.lastApplied;
      $("opsConfigState").append(facts([["서버 요청 버전",c.version],["최신 요청 상태",opsCommandLabel(desired?.state)],["요청한 값",opsSettingsText(desired?.settings)],["요청 시각",formatLocalTime(desired?.requestedAt)],["장치의 마지막 적용 보고",applied ? `v${applied.version} · ${opsSettingsText(applied.settings)}` : "아직 적용 성공 보고가 없습니다."],["적용 보고 수신",formatLocalTime(applied?.result?.receivedAt)],["최신 결과 오류",desired?.result?.errorCode || "없음 / 미보고"]]),rawDetails("서버 요청·장치 적용 보고 원문",c));
      if (c.scopeChanged) $("opsConfigState").append(facts([["매핑 변경","이전 매핑의 설정은 현재 장치 적용값으로 표시하지 않습니다."]]));
      opsControls();
    }
    function fillOpsConfig() {
      if (!opsConfig) return;
      const value = opsConfig.desired?.settings || opsConfig.lastApplied?.settings || opsConfig.defaults;
      $("opsInterval").value = String(value.measurementIntervalMs); $("opsReplay").value = String(value.replayBatchSize); $("opsConfigReason").value = "";
      $("opsHealthInterval").value = String(value.healthReportIntervalMs ?? 30000);
      opsDirty = false; opsControls();
    }
    async function loadOpsConfig() {
      if (!opsDevice || opsSaving || !can("device:read")) return;
      const key = opsKey(), generation = ++opsConfigGeneration, id = opsDevice.id, preserve = opsDirty, intent = opsIntent;
      opsConfig = null; renderOpsConfig(); $("opsConfigStatus").textContent = "서버 설정 조회 중";
      try {
        const c = await api(`/api/devices/${encodeURIComponent(id)}/configuration`);
        if (key !== opsKey() || generation !== opsConfigGeneration) return;
        if (c.deviceId !== id || !Number.isInteger(c.version) || c.version < 0 || !opsSettingsValid(c.defaults) || (c.desired && !opsSettingsValid(c.desired.settings)) || (c.lastApplied && !opsSettingsValid(c.lastApplied.settings))) throw new Error("설정 응답의 대상 또는 형식이 다릅니다.");
        if (!opsConfigScopeMatches(c)) {rejectOpsScope(); return;}
        opsConfig = c; opsUncertain = false;
        const found = intent && c.version === intent.expectedVersion + 1 && c.desired?.version === c.version && opsSameSettings(c.desired.settings,intent.settings) && c.desired.reason === intent.reason;
        if (!preserve || found) fillOpsConfig();
        opsIntent = null; renderOpsConfig();
        $("opsConfigStatus").textContent = found ? "요청과 같은 버전·설정·사유가 서버에 있습니다. 중복 발행하지 않았습니다. 장치 적용 보고는 별도로 확인하세요." : !opsCanEdit() ? "읽기 전용입니다. 설정 발행에는 권한과 활성·등록된 장치 매핑이 필요합니다." : preserve ? "최신 버전을 조회했습니다. 보존된 편집 내용과 서버 요청을 비교한 뒤 발행하세요." : c.desired ? "최신 요청을 불러왔습니다. 발행과 장치 적용 성공 보고는 별개입니다." : "발행 이력이 없어 기본값을 편집기에 표시합니다. 장치의 실제 적용값을 의미하지 않습니다.";
      } catch (error) {if (key === opsKey() && generation === opsConfigGeneration) {$("opsConfigStatus").textContent = `설정 조회 실패: ${error.message}. 다시 확인하기 전에는 발행할 수 없습니다.`; opsControls();}}
    }
    function renderOpsHistory() {
      $("opsHistoryRows").replaceChildren();
      if (!opsHistory.length) $("opsHistoryRows").textContent = "현재 매핑에 보관된 설정 이력이 없습니다.";
      else $("opsHistoryRows").append(statusTable("설정 발행·장치 결과",["버전","상태","요청 설정","요청 시각 / 작성자","변경 사유","결과 시각 / 오류"],opsHistory.slice((opsHistoryPage - 1) * PAGE_SIZE,opsHistoryPage * PAGE_SIZE).map(row => [row.version,opsCommandLabel(row.state),opsSettingsText(row.settings),`${formatLocalTime(row.requestedAt)} / ${displayValue(row.requestedBy)}`,row.reason,`${formatLocalTime(row.result?.receivedAt)} / ${displayValue(row.result?.errorCode)}`])));
      renderPager("opsHistory",opsHistoryPage,opsHistory.length);
    }
    async function loadOpsHistory() {
      if (!opsDevice || !can("device:read")) return;
      const key = opsKey(), generation = ++opsHistoryGeneration, id = opsDevice.id;
      opsHistory = []; opsHistoryPage = 1; renderOpsHistory(); $("opsHistoryStatus").textContent = "설정 이력 조회 중";
      try {
        const result = await api(`/api/devices/${encodeURIComponent(id)}/configuration/history`);
        if (key !== opsKey() || generation !== opsHistoryGeneration) return;
        if (!Array.isArray(result.items)) throw new Error("설정 이력의 형식이 다릅니다.");
        if (result.items.some(row => !opsScopeMatches(row))) {rejectOpsScope(); return;}
        opsHistory = result.items; renderOpsHistory(); $("opsHistoryStatus").textContent = `현재 매핑의 최근 ${result.retainedLimit}개 요청·최종 결과를 보관합니다.`;
      } catch (error) {if (key === opsKey() && generation === opsHistoryGeneration) {$("opsHistoryRows").replaceChildren(); $("opsHistoryStatus").textContent = `설정 이력 조회 실패: ${error.message}`;}}
    }
    async function publishOpsConfig() {
      if (!opsCanEdit() || opsSaving || opsUncertain) return;
      const settings = {measurementIntervalMs:Number($("opsInterval").value),replayBatchSize:Number($("opsReplay").value),healthReportIntervalMs:Number($("opsHealthInterval").value)}, reason = $("opsConfigReason").value.trim();
      if (!opsSettingsValid(settings) || !reason || reason.length > 1000) {$("opsConfigStatus").textContent = "허용 범위의 정수 두 값과 변경 사유(1–1000자)를 입력하세요."; return;}
      if (opsConfig.desired && ["pending","applied"].includes(opsConfig.desired.state) && opsSameSettings(settings,opsConfig.desired.settings)) {$("opsConfigStatus").textContent = "같은 설정이 이미 요청돼 있습니다. 중복 발행하지 않고 적용 보고를 확인해 주세요."; return;}
      const intent = {expectedVersion:opsConfig.version,settings,reason}, key = opsKey(), id = opsDevice.id;
      if (!confirm(`${id}에 v${intent.expectedVersion + 1} 설정을 발행할까요?\n${opsSettingsText(settings)}\n장치의 적용 성공 보고는 별도입니다.`)) return;
      opsSaving = true; opsIntent = intent; opsDirty = true; opsControls(); $("opsConfigStatus").textContent = "설정 요청을 저장하는 중입니다.";
      try {
        const command = await api(`/api/devices/${encodeURIComponent(id)}/configuration`,jsonOptions("PUT",intent));
        if (key !== opsKey()) return;
        if (command.version !== intent.expectedVersion + 1 || command.siteId !== opsDevice.siteId || command.assetId !== opsDevice.assetId || command.reason !== reason || command.state !== "pending" || !/^[0-9a-f]{32}$/.test(command.commandId) || !opsSameSettings(command.settings,settings)) throw new Error("서버 발행 확인이 요청 내용과 다릅니다.");
        opsConfigGeneration++; opsHistoryGeneration++;
        opsConfig = {...opsConfig,version:command.version,desired:command};
        opsHistory = [command,...opsHistory.filter(row => row.commandId !== command.commandId)].slice(0,100); opsHistoryPage = 1;
        opsUncertain = false; opsIntent = null; fillOpsConfig(); renderOpsConfig(); renderOpsHistory();
        $("opsConfigStatus").textContent = `v${command.version} 요청을 저장했습니다. 장치 적용 성공 보고 대기 중입니다.`;
        $("opsHistoryStatus").textContent = "이번 발행을 반영했습니다. 이전 이력 전체와 결과는 이력 새로고침으로 확인하세요.";
      } catch (error) {if (key === opsKey()) {opsUncertain = true; $("opsConfigStatus").textContent = `발행 결과 확인 실패: ${error.message}. 중복 발행 방지를 위해 잠갔습니다. 서버 설정을 다시 확인하세요.`;}}
      finally {opsSaving = false; opsControls();}
    }

    let acceptedSelection = {};
    const SELECTION_CONTROLS = ["siteSelect","assetSelect","periodSelect","severityFilter","eventLabelFilter","reviewedFilter","eventSort"];
    function rememberSelection() { acceptedSelection = Object.fromEntries(SELECTION_CONTROLS.map(id => [id,$(id).value])); }
    function allowSelectionChange() {
      if (!modelReviewBusy && !opsSaving && (!(reviewDirty || memoDirty || managementDirty || opsDirty || $("modelReviewReason").value.trim()) || confirm("저장하지 않은 편집을 버리고 조회 조건을 변경할까요?"))) return true;
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
      clearSnapshots();
      clearRF66();
      invalidateDeviceOperations();
      modelQueueGeneration++; modelQueueRows = []; modelQueueTotal = 0; modelQueuePage = 1; clearModelReview(); renderModelQueue();
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
    $("opsReload").addEventListener("click", () => act(loadDeviceOperations));
    $("opsDevice").addEventListener("change", () => act(() => selectOpsDevice($("opsDevice").value)));
    $("opsQualityLoad").addEventListener("click", () => act(() => Promise.all([loadOpsHealth(),loadOpsQuality()])));
    for (const id of ["opsQualityRange","opsQualityBucket"]) $(id).addEventListener("change", () => act(loadOpsQuality));
    $("opsConfigLoad").addEventListener("click", () => act(loadOpsConfig));
    $("opsHistoryLoad").addEventListener("click", () => act(loadOpsHistory));
    $("opsConfigPublish").addEventListener("click", () => act(publishOpsConfig));
    for (const id of ["opsInterval","opsReplay","opsHealthInterval","opsConfigReason"]) $(id).addEventListener("input", () => {opsDirty = true; opsControls();});
    $("opsAnalysisLoad").addEventListener("click",()=>loadOpsAnalysis());
    $("opsWindowsLoad").addEventListener("click",()=>loadOpsWindows());
    $("opsAnalysisRequest").addEventListener("click",()=>requestOpsWaveform());
    $("opsConfigCancel").addEventListener("click", () => {if (!opsSaving && !opsUncertain) {fillOpsConfig(); opsIntent = null;}});
    for (const [id,step] of [["opsQualityPrev",-1],["opsQualityNext",1]]) $(id).addEventListener("click", () => {if (opsQuality) {opsQualityPage = Math.min(Math.max(1,Math.ceil(opsQuality.items.length / PAGE_SIZE)),Math.max(1,opsQualityPage + step)); renderOpsQuality();}});
    for (const [id,step] of [["opsHistoryPrev",-1],["opsHistoryNext",1]]) $(id).addEventListener("click", () => {opsHistoryPage = Math.min(Math.max(1,Math.ceil(opsHistory.length / PAGE_SIZE)),Math.max(1,opsHistoryPage + step)); renderOpsHistory();});
    $("modelStatusFilter").addEventListener("change", () => act(() => loadModelQueue(true)));
    $("modelQueueLoad").addEventListener("click", () => act(() => loadModelQueue(true)));
    for (const [id,step] of [["modelQueuePrev",-1],["modelQueueNext",1]]) $(id).addEventListener("click", () => act(() => loadModelQueue(false, Math.max(1,modelQueuePage + step))));
    $("modelApprove").addEventListener("click", () => act(() => submitModelReview("approve")));
    $("modelReject").addEventListener("click", () => act(() => submitModelReview("reject")));
    for (const [view,config] of Object.entries(WORKSPACE_VIEWS)) $(config.button).addEventListener("click", () => setView(view));
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
      $("chartHint").textContent = `${point.timestamp} · ${vibrationLabel}: ${vibration ?? "-"} · ${acousticLabel}: ${acoustic ?? "-"} · ${rpmDescription(point)} · score: ${score ?? "unavailable"} (${point.anomalyStatus ?? "-"})`;
    });
    setInterval(() => {
      // Evidence and an unsaved decision must not be replaced by telemetry polling.
      if (["models","deviceOps"].includes(currentView) || !token || !selectedSite() || !$("assetSelect").value) return;
      if (!$("fromInput").value && !chartViewport) activeWindow = null;
      act(render);
    }, 5000);
  </script>
</body>
</html>"""
