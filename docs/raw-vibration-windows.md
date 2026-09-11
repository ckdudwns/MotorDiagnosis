# 원시 XYZ 전송·보존 및 서버 66개 특징 변환

## 구현 범위

`feat/continuous-vibration-windows`의 원시 전송 확장이다. ADXL345 800Hz·XYZ·512샘플을
그대로 보관한 뒤 현재 연구 후보의 spectral66 입력으로 변환한다. 모델 설치/활성화·알림 연결은 하지 않는다.
서버 표준 라이브러리만으로 변환하며 연구용 패키지나 joblib을 운영 중에 import/load하지 않는다.
최종 모델 파일에 맞춘 로더·버전 검증은 후속 작업이다. **변환 구현과 최종 모델 합의/현장 성능 검증은 별개다.**

## 전송 계약

- POST `/api/devices/{deviceId}/raw-vibration-windows`
- 인증: 기존 해당 장치 전용 수집 토큰. 잘못된 장치 권한·설비 매핑은 거부한다.
- JSON: `{"windows":[...]}`, 한 배치 1~4구간, HTTP 최대 64KiB. 펌웨어는 최대 2구간씩 보낸다.
- 기본 메타데이터: schemaVersion=1, deviceId/siteId/assetId, bootId(32자리 소문자 hex),
  windowIndex, timestamp(측정 시작 UTC), startUptimeUs, sampleRateHz=800, sampleCount, axes=[X,Y,Z], quality.
- 원시 프로필: `adxl345-800hz-xyz-counts-v1`, unit=`count`, gPerCount=`0.0039`,
  encoding=`base64-int16le-xyz`, samples=정규 base64 문자열.
- 바이트 순서: X0,Y0,Z0,X1,Y1,Z1,..., 각 값은 signed int16 little-endian.
  정상 512샘플이면 3,072바이트, base64는 4,096문자다. 13비트 센서 범위 밖 값은 거부한다.
- 이 endpoint에는 `features`를 보내지 않는다. 기존 `/vibration-windows`는 계속 21개 특징 전용이다.

불량 구간도 원시 샘플 일부를 보존할 수 있다. sampleCount는 실제 축별 수(0~512)이며 문자열 길이와 일치해야 한다.
quality가 불량일 때만 samples=null을 허용한다. 정상 표기에는 512개의 실제 샘플이 필수다.
클리핑 레일(-4096/4095)·상수축이 정상이라고 전송되어도 서버 변환에서 판정 불가로 처리한다.

정규화된 본문 SHA-256과 장치/boot/순번으로 중복을 식별한다. 동일 본문 재전송은 200,
신규 포함은 202 및 acknowledged 목록을 반환한다. 같은 ID/다른 내용은 409다. 이미 수신한
높은 순번 뒤에 도착한 아직 저장되지 않은 낮은 순번도 같은 boot/context와 index별 uptime 순서를
만족하면 허용하며, stream high-water mark는 후퇴하지 않는다. context·UTC/uptime clock 불일치는 409다.
배치 전체가 한 트랜잭션이며 뒤 구간에 오류가 나면 앞 구간과 시계 기준점도 함께 롤백된다.

## 시각 대조

각 장치/boot의 첫 UTC와 uptime을 영속 저장한다. 신규 각 구간의 누적 UTC 경과와 uptime 경과 차이가
1초를 초과하면 409 `TIMESTAMP_UPTIME_MISMATCH`로 거부한다. 행별 작은 오차가 누적되어도 허용량을 늘리지 않는다.
이 검사는 시각 정지·누적 드리프트를 막는 무결성 검사이며 실제 샘플링 주파수 정확도 인증은 아니다.
동일 ID의 정상 재전송은 기존 ACK를 반환한다. 새 부팅은 별도 기준점을 사용한다.

펌웨어는 부팅 후 유효한 UTC가 확보되면 uptime→UTC 변환 기준을 한 번 고정한다. 이후 같은 부팅의 NTP 보정으로
이미 버퍼링된 측정 시각이 바뀌지 않는다. 초기 동기화 전 수집 시각은 해당 기준으로 역산한 추정값이다.
긴 부팅 동안의 실제 시계 드리프트/UTC 정확도는 실측해야 하며 이 방식은 하드웨어 동기화를 보장하지 않는다.
영구 400/409/401 오류도 자동으로 데이터를 버리지 않고 재시도하므로 원인 수정 전 큐가 차서 새 구간을 잃을 수 있다.
서버/장치 로그와 순번 누락을 운영자가 확인해야 한다.

## 저장·변환·조회

기본 DB: `output/raw-vibration-windows.sqlite3`, 환경 변수 `RAW_VIBRATION_WINDOW_DB_PATH`로 변경한다.
**기존 특징 DB와 다른 파일을 사용한다.** 같은 DB 파일을 두 경로에 설정하면 저장 종류 검사가 시작을 차단한다.
원래 카운트·품질·메타데이터와 계산 결과를 SQLite에 저장한다.
기존 `VIBRATION_WINDOW_DB_PATH`는 21개 경로 그대로 유지한다.

- 보존 시간 최대 48시간, 전역 최대 300,000구간, 처리 대기 최대 4,096구간.
- 한 장치의 48시간 명목 구간 수는 270,000개다. 여러 장치는 전체 한도를 공유한다.
- 한도에 도달하면 503으로 재시도를 요구한다. 미처리 구간을 자동으로 삭제하지 않는다.
- 처리 완료한 오래된 구간은 보존 기간에 따라 삭제된다. 장치 식별자 보호·스트림 기준은 유지한다.
- raw bytes만 하루 약 415MB/장치이며 base64·JSON·특징·SQLite/WAL·백업 공간은 추가다.
- 장기 수집본은 보존 종료 전에 따로 백업/내보내야 한다. 기존 PR35의 특징 전용 내보내기는 이 원시 DB 형식의 수집본을 지원하지 않는다.

GET `/api/devices/{deviceId}/raw-vibration-windows`는 운영 계정의 device/telemetry/model 읽기 권한과 사이트 범위를 검사한다.
최근 20구간의 원시 본문과 처리 결과를 반환한다. 이는 전체 데이터 내보내기 API가 아니다.
별도 GPT Sites 화면은 아직 이 endpoint에 연결하지 않았다.

계산 결과는 `variant=spectral66`, 응답 메타데이터의 featureProfileId는
`mcc5-vibration-800hz-spectral66-v1`이다. 기본 21개는 float32로 반올림 후 float64 승격,
추가 45개는 float64로 계산하여 현재 학습 캐시 정책과 맞춘다.
모델이 없으므로 정상 품질의 처리 결과는 `waiting_model`, verdict=null, affectsAlerts=false다.
불량/변환 실패는 `unavailable`이고 정상이나 0으로 채우지 않는다. RF 연구 후보를 자동 실행하지 않는다.

## 펌웨어

N8 기본 build_flags는 `CONTINUOUS_VIBRATION_ENABLED=1`, `RAW_VIBRATION_ENABLED=1`이다.
기존 특징 전송으로 빌드할 때는 RAW_VIBRATION_ENABLED를 0으로 바꾸고 보드를 재부팅한다.
원시 모드는 동일 구간을 21개 endpoint에 이중 전송하지 않으며, 기존 요약 텔레메트리·음향 수집은 유지한다.

원시 전송 큐는 내부 RAM **8구간(5.12초)**, 처리 큐는 3구간이다. 전송 중 앞 구간을 덮어쓰지 않고
넘치면 새 구간을 버리며 drop 로그/서버 순번 공백으로 알린다. 부팅이 바뀌면 미전송 RAM은 사라진다.
이 모드에 기존 특징 큐의 64구간/40.96초 수치를 적용하면 안 된다. PSRAM/Flash 영속 큐는 추가하지 않았다.
2구간 배치와 400ms 배치 간격 하한은 통신 시도 정책일 뿐 640ms마다 도착하는 지연 보장이 아니다.

서버를 먼저 배포하고 endpoint·권한·디스크 공간을 확인한 뒤 보드를 올린다. 업로드 전 기존 파티션/LittleFS 자료를 백업한다.
실제 보드에서는 10분 이상 순번/시각/누락, 최소 heap·연속 heap·stack, TLS 중 수집 지속성, 단절/ACK 유실/재부팅을 검사해야 한다.
빌드 성공과 정적 RAM 수치는 이 실측을 대신하지 않는다.

## 검증 기록

- 원시 경로 검사에는 실제 학습 Python 추출기와 이름/순서/66개 수치 비교가 포함된다.
- 최종 원시·기존 특징·수집 인증·RPM 회귀 69개 통과, 제외 없음(C++ fixture와 실제 학습 의존성 포함).
- 전체 백엔드 중간 회귀 시점: 565개 실행, 111개 선택 검사 제외, 실패 없음. 이후 추가 DB 종류 보호/네이티브 연동은 위 69개로 재검사했다.
- C++ 특징/큐/바이트 순서 검사 6개 통과. 학습용 특징과 서버 변환의 비교는 합성 입력 기준이며 현장 승인 오차 확정은 아니다.
- N8 원시 모드·기존 보드 프로필 빌드 성공. N8 정적 RAM 209,952/327,680 bytes(64.1%).
- 기존 화면 검사 91개 통과. 새 원시 조회 UI는 별도 미구현이다.
- 실제 보드, 운영 배포, 실측 모델 정확도 시험은 수행하지 않았다.

재현: `python -m unittest tests.test_raw_vibration tests.test_vibration_windows`.
학습 참조 비교에는 NumPy/SciPy/scikit-learn 등 기존 연구 의존성이 필요하며 없으면 해당 비교만 제외된다.
