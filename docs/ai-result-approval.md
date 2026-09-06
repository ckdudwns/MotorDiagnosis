# AI1 결과 자동 등록과 사람 검토

## 범위

AI1 4주차 학습 작업의 **선택된 후보 한 개**를 기존 백엔드 모델 메타데이터 레지스트리로 인계한다. 등록은 `draft / pending`, 사람의 명시적인 검토는 `approved` 또는 `rejected`로 기록한다. 운영 모델 배포·롤백·추론 점수 교체·학습 재실행은 하지 않는다.

기존 AI1의 동결 매니페스트 무결성 검사와 모델 등록 검사를 재사용한다. 인계 전에 로컬 산출물 바이트의 SHA-256을 보고서와 대조한다. 백엔드는 산출물 URI를 내려받거나 PyTorch 파일을 실행하지 않는다. 따라서 서버의 `artifactVerified`는 승인 후에도 `false`, `deploymentStatus`는 `not_deployed`다. 체크섬과 성능은 제출자의 근거이며 서버가 파일·현장 성능을 독립 검증했다는 뜻이 아니다.

이번 검토 단위는 모델 결과와 연결된 데이터셋·기준선 snapshot이다. 데이터셋/기준선 자체의 별도 승인 상태를 바꾸지 않는다. 기존 수동 등록을 없애지 않지만, 인계 근거·체크섬이 없는 기존 수동 메타데이터는 새 검토 경로로 승인할 수 없다.

## 사전 등록과 인증

1. 학습에 쓰는 AI1 동결 매니페스트를 준비한다. 기존 백엔드 데이터셋 등록 API에 대응 데이터셋을 등록하고, 그 데이터셋을 참조하는 자산별 기준선을 등록한다. 이것은 학습 결과 자동 인계와 별개의 데이터/운영 범위 결정이다.
2. 백엔드 데이터셋의 `source.checksum`은 AI1 동결 매니페스트의 `source.checksum`과 **정확히 같아야 한다**. 외부 원본 압축파일 해시와 특징 snapshot 해시를 임의로 같은 것으로 취급하지 않는다. 기존 내부 실측 export를 변환해 학습할 경우에도 이 연결 계약을 충족하는 매핑이 필요하며, 이 기능이 서로 다른 데이터셋 형식을 자동 변환하지는 않는다.
3. 백엔드에 `AI1_RESULT_PRODUCERS_JSON`을 주입한다. 예시 형식은 다음과 같다. 토큰 값은 실제 무작위 비밀값으로 별도 생성하고 Git에 넣지 않는다.

```json
{"AI1-TRAINER":{"token":"<전용 무작위 토큰>","allowedSiteIds":["SITE-01"]}}
```

토큰은 ASCII 영숫자·`_`·`-` 32–128자다. 중복 키·중복 토큰·빈 사이트 목록·와일드카드 사이트는 거부한다. 기본값은 비활성이다. 장치·관리자 토큰을 자동 인계 인증으로 대체하지 않는다. 서비스 principal은 `model:write`, `baseline:read`, `dataset:read`만 가지며 사람 검토 권한은 없다.

AI1 실행 환경에는 `AI1_RESULT_URL=https://backend.example.com/api/ai1/results`와 `AI1_RESULT_TOKEN`을 설정한다. 리디렉션을 따르지 않는다. 로컬 시험에만 `AI1_RESULT_ALLOW_LOCAL_HTTP=true`와 loopback HTTP를 함께 허용한다. URL과 토큰의 운영 주입·배포는 별도 작업이다.

인계 대상 JSON 파일:

```json
{
  "producerId": "AI1-TRAINER",
  "datasetId": "DATASET-00001",
  "datasetFingerprint": "sha256:<백엔드 dataset.versionFingerprint의 64자리 해시>",
  "baselineVersion": "BASELINE-00001"
}
```

선택적으로 `artifactUri`를 넣어 미리 복사해 둔 HTTPS/S3 참조를 사용할 수 있다. 업로더를 새로 제공하는 것은 아니다. 생략하면 기존 학습 보고서의 로컬 file URI를 사용하며, 다른 컴퓨터에서 그 경로가 열리는 것을 보장하지 않는다.

## 학습 완료 후 자동 인계

기존 학습 명령에 `--handoff-target`을 추가한다.

```text
python ai/ai1/week4/ai1/freq_baseline/train_and_evaluate.py --manifest frozen.json --artifact-dir output/models --output output/training-report.json --handoff-target handoff-target.json
```

성공한 학습 결과·동결 매니페스트와 `result-handoff.json`은 작업 ID별 산출물 폴더에 보관한다. 전송 전에 불변 outbox를 쓰며, 토큰은 저장하지 않는다. 같은 파일을 다른 내용으로 덮어쓰지 않는다. 파일 발행은 fsync한 임시 파일과 hard-link의 원자적 생성에 의존하므로 이를 지원하는 로컬 파일시스템을 사용한다. 네트워크 공유·실제 전원 차단 시험은 별도다.

HTTP 요청은 10초 timeout, 기본 최대 3회(최대 5회)다. 연결 오류·429·5xx에는 제한된 대기를 적용하고, 4xx·리디렉션·잘못된 ACK는 등록 완료로 처리하지 않는다. ACK 유실·실패 시 outbox를 유지한다. 모델 버전, producer/job ID, 제출 digest, 데이터셋·기준선·산출물 체크섬이 일치하는 ACK에만 receipt를 남긴다.

재학습 없이 재전송:

```text
python -m ai.ai1.result_handoff --pending output/models/<jobId>/result-handoff.json
```

이미 완료된 보고서로 인계 파일을 준비하고 전송:

```text
python -m ai.ai1.result_handoff --report training-report.json --manifest frozen-manifest.json --target handoff-target.json --pending output/result-handoff.json
```

자동 인계는 opt-in이며, 옵션이 없으면 기존 학습 경로를 그대로 유지한다. 실패한 네트워크 등록을 해결하려고 학습 명령을 다시 실행할 필요는 없다. 주기적 백그라운드 전송 서비스까지 설치하지는 않는다.

## API와 승인·반려

- `POST /api/ai1/results`: 전용 producer 인증. 기존 동결 데이터셋·기준선의 범위와 source checksum/fingerprint를 확인한다. `{producerId, jobId}`와 내용 digest로 중복을 판정한다. 최초는 저장 commit 후 201, 같은 내용은 200, 같은 ID의 다른 내용은 409다. 검토 완료 후의 동일 재전송도 승인 상태를 초기화하지 않는다.
- `GET /api/model-versions?siteId=...&assetId=...&status=draft|approved|rejected&page=1&size=12`: 기존 사용자 인증, `model:read`, 사이트 범위. status 생략 시 전체다.
- `GET /api/model-versions/{version}`: 데이터셋·기준선 snapshot, 제출 근거, 등록 digest, 검토 이력을 함께 반환한다.
- `POST /api/model-versions/{version}/reviews`: 사용자 인증과 `model:review`. 관리자 B 및 시스템 관리자 C만 사용하며 사이트 범위를 다시 확인한다. 작성자 자신의 검토는 거부한다.

```json
{
  "decision": "approve",
  "reason": "등록 근거와 현장 적용 한계를 검토함",
  "requestId": "<이번 검토 요청의 UUID>",
  "expectedRevision": 0,
  "registrationDigest": "sha256:<화면에서 조회한 등록 근거 digest>"
}
```

반려는 `decision: "reject"`다. 사유는 필수이며 최대 1000자다. 최신 revision과 digest가 아니면 409로 다시 조회하도록 안내한다. 같은 requestId·작성자·내용의 재시도는 이력을 추가하지 않는다. 승인/반려가 끝난 버전의 근거를 덮어쓰거나 다시 승인하지 않으며, 보완 결과는 새 jobId·모델 버전으로 인계한다.

등록·검토는 기존 일반 상태 DB transaction에 모델/검토/감사를 함께 저장한다. 저장 실패 시 메모리도 복원된다. 운영 실행은 기존 `STATE_DB_PATH`의 영속 저장소를 사용한다. 재시작 후에도 제출 식별자와 승인자·사유·시각·metric snapshot이 유지된다.

## 화면

`AI 결과 검토` 화면에서 사이트·설비별 대기/승인/반려 결과를 조회하고 상세 근거를 연다. 읽기 전용 사용자는 버튼을 사용할 수 없다. 관리자에게는 사유 작성과 승인/반려 버튼이 표시된다. 오래된 목록/상세 응답은 버리며, 조회 실패 시 이전 결과를 남겨 잘못 선택하게 하지 않는다. 저장 ACK를 먼저 확정하고 후속 목록 조회 실패를 저장 실패로 오인하지 않는다. 검토 화면에서는 텔레메트리 자동 갱신이 선택 근거·입력을 초기화하지 않는다. 미저장 사유가 있으면 목록·필터·선택 변경 전 폐기 여부를 확인한다.

## 검증 범위

백엔드 HTTP 계약·권한·중복/경합·저장 실패·재시작, AI1 불변 outbox와 실제 HTTP 인계, 대시보드 실패·응답 경합을 회귀 테스트로 검증한다. 테스트 산출물은 시험용이며 실제 모델 성능·운영 TLS·배포·사람의 실제 운영 승인을 대신하지 않는다.
