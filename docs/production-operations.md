# EC2 운영 마무리

단건 입력 준비(2단계)와 기존 RF66 연결 중지를 반영한 운영 도구다. 서버 코드 머지·적용 후
EC2에서 실행한다. `/usr/bin/python3`의 표준 라이브러리만 사용하며 모델이나
실제 데이터를 읽기 위해 추가 패키지를 설치하지 않는다.

## 1. 현재 설정·저장 공간 확인

```bash
cd /home/ubuntu/MotorDiagnosis
sudo /usr/bin/python3 -m motor_diagnosis.operations inspect --device DEV-01-MOT-02
```

실행 중인 `motordiagnosis`의 PID·작업 디렉터리를 확인하고 `/proc`에서 DB·모델·
계정 파일 **경로에 해당하는 허용 목록만** 사용한다. `.venv`, `.venv-rf66` 어느
실행 환경이든 운영 서비스의 실제 설정을 기준으로 동작한다. 파일 내용이나
로그인·장치 토큰은 보고서에 출력하지 않는다. 서비스가 정지돼 있거나 조회 권한이
없으면 추정 경로로 진행하지 않고 실패한다. 출력 전체를 공개 저장소에 올리지 않는다.

점검 결과:

- 종료 코드 `0`: 점검 항목 정상, `1`: 경고, `2`: 장애 또는 점검 실패.
- 디스크 80% 이상 경고, 90% 이상 또는 여유 2 GiB 미만 장애.
- 단건 100,000건 / 기존 원시 300,000건 / 특징 500,000건 한도의 90% 이상 경고.
- 새 단건 `queued`가 수신 후 120초를 넘으면 `PREPARATION_BACKLOG`. 입력 준비를 마친 `waiting_model`은 모델 미설정 상태이며 준비 지연이 아니다.
- 새 단건 자료 없음·측정 시각 정지·상태별 주기(정상 300/이상 10초)+60초보다 오래된 입력·입력 불가를 구분.
- 기존 RF66 원시 저장소는 `historicalOnly: true`, `processingEnabled: false`다. 오래된 RF66 입력·대기·미투영 사건은 현재 추론 장애 경고로 처리하지 않는다.
- RF66 이벤트 9,000건 이상인 과거 저장 용량과 120초 이상 지난 알림 대기를 확인.
- 정상 수신 여부는 선택한 장치의 원시 데이터로 확인한다. 예전 요약 텔레메트리의
  마지막 수신 시각만으로 원시 입력 정상 여부를 판정하지 않는다.

새 단건 원본·준비 결과와 중지된 기존 RF66 원본·결과는 자동 삭제하지 않는다.
`operations observe`도 `periodic-snapshots`의 순번/측정 시각 진행을 검사한다.
RF66 모델 파일이 기존 백업 설정에 남아 있으면 파일 보존·해시 검증만 유지하며, 추론을 활성화하지 않는다.
한 장치가 640ms마다 전송하면 하루 135,000건, 48시간 270,000건이다.
기본 원시 상한은 **서버 전체 300,000건**이므로 현재 설정의 연속 수집 용량은
사실상 한 장치 기준이다. 장치를 늘리기 전에 저장 용량·처리 속도·보관 정책을 다시 정한다.
SQLite에서 행이 만료돼도 파일이 즉시 작아지지는 않는다. 여유 페이지를 재사용한다.
운영 중 임의의 DB 삭제나 VACUUM으로 공간을 확보하지 않는다.

## 2. 백업 1회 + 별도 폴더 복구 검사

백업 명령은 서비스 수집을 잠시 중지하고 DB를 함께 저장한 뒤 자동 시작한다.
운영 서비스 이외에 같은 DB를 쓰는 MQTT 브리지나 별도 스크립트가 있다면 먼저 중지한다.
백업 중 계정 파일 변경·서비스 설정 변경·모델 교체·다른 서버 프로세스 실행은 하지 않는다.
장치의 재전송 버퍼가 이 중단 시간을 감당해야 한다.

```bash
cd /home/ubuntu/MotorDiagnosis
sudo install -d -m 700 /var/backups/motordiagnosis
sudo /usr/bin/python3 -m motor_diagnosis.operations backup --destination /var/backups/motordiagnosis
curl --retry 5 --retry-connrefused --retry-delay 1 --max-time 10 https://motordiagnosis-api.duckdns.org/api/health
```

출력의 `backup` 경로를 아래 명령에 넣는다. 예시의 `BACKUP_DIRECTORY`는 실제 경로로
교체한다. 디렉터리 자동 검색으로 복구 대상을 추정하지 않는다.

```bash
sudo /usr/bin/python3 -m motor_diagnosis.operations verify-backup --backup BACKUP_DIRECTORY
sudo /usr/bin/python3 -m motor_diagnosis.operations restore-drill --backup BACKUP_DIRECTORY --destination /var/backups/motordiagnosis-restore-drill-01
```

검사 폴더는 새 경로여야 한다. 이미 존재하면 실패하며 운영 DB는 덮어쓰지 않는다.
복원 후 실제 서비스는 여전히 기존 DB를 사용한다. 검증 성공은 파일 해시·크기 및
SQLite `quick_check`, RF66 패키지 내부 무결성·모델 체크섬 검사 통과를 뜻한다.
모델을 실행하거나 현장 성능을 확인하는 검사는 아니다.

백업 내용은 runtime, alerts, analysis, communication-quality, vibration-windows,
raw-vibration-windows, 존재하는 기존 model-inference DB, 계정·수집 토큰 설정,
설정된 모델 파일과 systemd 본체·추가 설정, 존재하는 Caddyfile이다.
원시 DB의 3구간 상태·RF66 사건 이력과 runtime 검수 이력·알림 outbox를 같은 중단 구간에
저장한다. SQLite backup API로 WAL에 커밋된 내용도 포함한다.

RF66 체크섬은 다음 두 대상을 구분한다.

- 백업 `manifest.json`의 `files[].sha256`: 복사된 **ZIP 전체**의 SHA-256.
  백업 파일의 손상·변경을 검사하며 `RF66_MODEL_CHECKSUM`과 비교하지 않는다.
- `RF66_MODEL_CHECKSUM` 및 백업 `modelChecksum`: ZIP 내부
  **`model/candidate.joblib`**의 `sha256:<해시>`.
  내부 모델 바이트 및 패키지 `MANIFEST.json`의 `modelVersion`과 대조한다.

백업 생성·검증·복구 검사 모두 내부 파일 목록과 각 파일의 해시도 확인한다.
ZIP 크기·중복 파일·암호화 여부 검사는 추론 로더와 같은 코드를 사용한다.
모델 역직렬화, 패키지에 포함된 코드 실행, ML 라이브러리 로딩은 하지 않는다.

`manifest.json`을 마지막에 기록하며 파일별 해시·원래 경로·소스 커밋을 남긴다.
실패한 디렉터리는 조사용으로 보존되고 유효한 백업으로 취급하지 않는다. 백업 전체를
함께 복구해야 한다. runtime 또는 alert DB만 이전 시점으로 되돌리지 않는다.
암호·토큰 관련 설정이 포함되므로 디렉터리 700, 파일 600을 적용한다.
암호화된 EC2 볼륨/암호화된 외부 백업 저장소를 사용하고 Git·채팅에는 올리지 않는다.

새 서버 재구축에는 이 백업 외에 해당 커밋의 소스, 호환 Python·의존성,
네트워크 설정이 필요하다. Caddy 인증서 데이터·EC2 보안 그룹·장치 펌웨어 및
클라이언트용 원문 수집 토큰은 이 파일 백업에 포함되지 않는다.

실제 장애 복원은 서비스를 중지한 뒤 현재 파일 집합을 별도 보존하고, 검증된 새
폴더의 **전체 DB 집합**을 원래 경로로 옮겨 적용하는 별도 작업이다. 기존 `-wal`·`-shm`
파일을 새 DB와 섞지 말고 함께 보존·분리한다. 서비스 사용자 소유권을 복원하고
`RF66_EVENT_MODE=events`로 시작한다. API·장치 재전송·사건 중복을 점검한 다음 알림을 켠다.
이 문서의 `restore-drill`은 운영 경로 교체를 자동 실행하지 않는다.

## 3. 정기 백업·5분 점검 설치

1회 백업과 복구 검사를 통과한 뒤 설치한다. 기존 서비스 경로가 다르면 먼저 unit의
WorkingDirectory를 수정한다. 새 파일 이름이 기존 설정과 충돌하지 않는지도 확인한다.

```bash
cd /home/ubuntu/MotorDiagnosis
sudo install -m 644 deploy/systemd/motordiagnosis-backup.service /etc/systemd/system/motordiagnosis-backup.service
sudo install -m 644 deploy/systemd/motordiagnosis-backup.timer /etc/systemd/system/motordiagnosis-backup.timer
sudo install -m 644 deploy/systemd/motordiagnosis-inspect.service /etc/systemd/system/motordiagnosis-inspect.service
sudo install -m 644 deploy/systemd/motordiagnosis-inspect.timer /etc/systemd/system/motordiagnosis-inspect.timer
sudo systemd-analyze verify /etc/systemd/system/motordiagnosis-backup.service /etc/systemd/system/motordiagnosis-backup.timer /etc/systemd/system/motordiagnosis-inspect.service /etc/systemd/system/motordiagnosis-inspect.timer
sudo systemctl daemon-reload
sudo systemctl enable --now motordiagnosis-backup.timer motordiagnosis-inspect.timer
sudo systemctl list-timers motordiagnosis-backup.timer motordiagnosis-inspect.timer --no-pager
```

매일 UTC 03:30(한국 12:30)에 최대 5분 분산해 백업한다. 중단 가능한 시각으로
`OnCalendar`를 조정할 수 있다. `Persistent=true`이므로 놓친 일정은 타이머 시작 후
실행될 수 있다. 백업 helper가 중도 종료돼도 unit의 ExecStopPost가 남아 있는
재시작 표식을 확인해 앱 시작을 시도한다. 처음부터 정지돼 있던 앱은 켜지 않는다.
수동 백업 중 프로세스가 강제 종료된 경우 저장소 폴더에서
`sudo /usr/bin/python3 -m motor_diagnosis.operations resume-backup`으로 같은 복구 절차를 실행한다.
매 5분 점검 결과는 journal에 남으며 외부 메시지를 발송하는 설정은 포함하지 않는다.

```bash
sudo journalctl -u motordiagnosis-backup.service -u motordiagnosis-inspect.service -n 100 --no-pager -o cat
```

백업 생성 후 36시간이 지나면 점검에 경고가 나온다. 나이 점검은 해시 전체 검증을
대신하지 않으므로 주 1회 `verify-backup`과 복구 검사를 반복한다. 백업은 자동 삭제하지
않는다. 권장 보관 초안은 로컬 일간 3개와 외부 일간 7개·주간 4개이며, 실제 DB 크기와
업무 보관 요구로 확정한다. 외부 백업 저장 위치가 정해지기 전까지 동일 EC2 디스크의
사본만으로 볼륨 손실에 대비했다고 간주하지 않는다. 오래된 백업 삭제는 외부 사본의
검증과 삭제할 정확한 경로 확인 후 수행한다.

## 4. 장시간 관측 및 장애 시험

짧은 점검부터 실행하고 이상이 없으면 24시간으로 늘린다. observe는 실측·설정·알림에
쓰지 않고 로컬 JSONL 보고서에 관측만 기록한다. `systemd-run` 작업은 SSH가 끊겨도 계속된다.
시간·보고서 이름을 바꿔 재실행하며 같은 출력 파일을 덮어쓰지 않는다.

```bash
sudo install -d -m 700 /var/lib/motordiagnosis/operations
sudo systemd-run --unit=motordiagnosis-observe-01 --property=WorkingDirectory=/home/ubuntu/MotorDiagnosis --property=UMask=0077 /usr/bin/python3 -m motor_diagnosis.operations observe --device DEV-01-MOT-02 --seconds 86400 --interval 10 --output /var/lib/motordiagnosis/operations/observe-01.jsonl
sudo journalctl -u motordiagnosis-observe-01 --no-pager -n 20
```

5분 예비 시험에는 `--seconds 300`과 다른 unit/출력 이름을 쓴다. 종료코드 경고/장애와
`rawInputProgressObserved`를 함께 확인한다. 데이터가 전혀 증가하지 않는 관측은 통과가 아니다.

| 시험 | 실행 | 확인할 결과 |
| --- | --- | --- |
| 연속 수집 | 보드 켠 상태에서 24시간 observe | 새 원시 구간·측정 시각 증가, 대기열/공간 안정, 누락·품질 이력 확인 |
| 통신 단절 | 담당자가 보드 네트워크를 30초 끊었다 복구 | 열린 사건 유지/관측 불가, 재전송 동일 구간 중복 방지, 처리 대기 해소 |
| 서버 재시작 | `sudo systemctl restart motordiagnosis` | API 복귀, 기존 검수·사건·알림 유지, 접수한 구간 재처리/유실 확인 |
| 응답 유실 | 시험 환경에서 수집 응답 전달 차단 후 동일 구간 재전송 | 같은 bootId/index는 재접수 확인만 하고 사건 중복 없음 |
| 보드 재부팅 | 담당자가 보드 전원 재인가 | 새 bootId, 누적 초기화, 품질 확인 후 3구간부터 판정 |
| 센서 장애 | 담당자가 안전한 시험 상태에서 센서 장애 재현 | 품질 불량/판정 불가, 정상 복귀로 해제하지 않음 |
| 저장 실패 | 폐기 가능한 시험 DB의 쓰기를 실패시킴 | 성공 응답/완료를 거짓 표시하지 않음, 접수 구간과 상태 재시도 |

소프트웨어 회귀 검사는 256개 후보 구간 중 1개 누락·1개 센서 장애,
반복 응답 유실과 3회 저장소 재시작을 조합해 255개 수락 구간과 8개 사건의
중복 방지·복구 후 재전송을 확인한다. 합성 입력과 대역 모델을 사용한 가속 시험이다.
24시간 실측, 실제 네트워크 단절, 센서 전원 제어 및 RF 정확도 시험을 대신하지 않는다.

## 5. 업로드용 임시 SSH 키 정리

EC2 콘솔 연결과 별도의 정상 관리 SSH 접속을 먼저 확보한다. 기존 Git 배포키
`motordiagnosis_deploy`는 유지한다. 아래는 RF66 업로드에 사용한 **공개키 본문 전체**를
정확히 비교해 삭제하는 절차다. 원문 개인키는 출력하지 않는다.

```bash
cd /home/ubuntu/MotorDiagnosis
sudo /usr/bin/python3 -m motor_diagnosis.operations revoke-upload-key --authorized-keys /home/ubuntu/.ssh/authorized_keys --key-base64 AAAAC3NzaC1lZDI1NTE5AAAAIIcPXP01qLDXa8f0Wf6XpVyM8SkKhGloS/BUfo7NmC9t
```

`matched: 1`을 확인한 뒤 같은 명령 뒤에 `--apply`를 붙인다. 변경 전 파일을 600 권한으로
보존하고 다른 키·주석은 유지한다. 다시 실행해 `matched: 0`인지 확인한다.
서버 등록 키를 제거한 후 PC의 임시 개인키 파일은 Windows 휴지통으로 보낸다.
대상은 이번 업로드 폴더 내부의 동명 키이며 다른 SSH 키 디렉터리를 정리하지 않는다.

EC2 보안 그룹은 인스턴스에 실제 연결된 그룹에서 확인한다. SSH 22번의 임시 전체
허용 규칙이 있으면 현재 관리 IP `/32` 등 필요한 출처만 남긴다. HTTPS 443·인증서용
HTTP 80은 현재 Caddy 구성에 맞춰 유지하고 앱 내부 포트 8787을 인터넷에 열지 않는다.
EC2 규칙 변경과 PC 키 삭제는 이 CLI가 실행하지 않는다.

## 개발 검증

```text
python -m unittest tests.test_operations tests.test_operations_recovery tests.test_rf66_events tests.test_backend_production_integrations
```

Linux systemd unit의 실제 설치·중단 시간·복구된 운영 계정 로그인·외부 백업 업로드는
EC2 적용 단계에서 결과를 기록한다. 로컬 테스트 성공을 이 항목의 완료로 표기하지 않는다.
