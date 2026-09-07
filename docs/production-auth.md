# 운영 계정 설정 및 EC2 적용

운영 사용자 계정은 `AUTH_USERS_FILE`로 지정한 **Git 저장소 밖의 비공개 JSON 파일**에서 읽습니다.
이 파일은 암호 해시와 역할·사이트 범위를 포함합니다. 원본 비밀번호를 저장하지 않습니다.

## 동작 계약

- `APP_ENV=production`이면 계정 파일이 필수입니다. 없거나 손상되면 서버가 소켓·DB를 열기 전에 시작을 거부합니다.
- 실행 중 파일이 사라지거나 검증에 실패해도 데모 계정으로 돌아가지 않습니다. 인증이 필요한 요청은 `503 AUTH_NOT_CONFIGURED`가 됩니다. `/api/health`는 프로세스 상태 확인용이며 인증 설정의 정상 여부까지 보장하지 않습니다.
- 파일이 설정된 경우 개발 모드에서도 파일에 있는 계정만 사용할 수 있습니다. 개발 모드이고 파일 경로도 없을 때만 기존 데모 계정을 사용합니다.
- A/B/C 역할과 `allowedSiteIds`는 기존 권한 정책을 그대로 사용합니다. 사용자 응답에 해시·솔트는 포함하지 않습니다.
- 파일은 인증 요청마다 다시 읽습니다. 비밀번호·이름·역할·사이트 범위·ID를 변경하거나 계정을 삭제하면 해당 계정의 기존 세션은 무효화됩니다. 세션은 서버 재시작 시에도 폐기됩니다.
- 운영 계정 ID는 데모 사용자 ID와 분리하여 이전 데모 감사 이력을 새 운영자에게 귀속시키지 않습니다. `passwd`는 기존 ID를 유지합니다.
- Linux에서는 일반 파일, 절대 경로, 소유자 전용 권한(예: `600`) 및 root/실행 사용자 소유 여부를 검사합니다. 심볼릭 링크는 허용하지 않습니다. Windows에서는 별도로 사용자 전용 ACL을 설정해야 합니다.
- 계정 파일은 64 KiB/100명 이내, 명시적인 A/B/C 역할과 사이트 목록, PBKDF2-HMAC-SHA256 600,000회 이상의 해시를 요구합니다. 평문 비밀번호·중복 키·알 수 없는 필드는 거부합니다.

해시 설정은 [OWASP Password Storage 안내](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#pbkdf2)를 참고했습니다. 비밀번호 입력은 [Python getpass](https://docs.python.org/3/library/getpass.html)를 사용하며 화면 숨김이 불가능하면 중단합니다.

## 1. 변경 코드 반영 전 준비

이 문서의 명령은 **본 변경을 커밋·푸시하고 검토 후 서버의 main에 반영한 다음** 사용합니다. 아직 외부 공개하지 마세요. 기존 서버를 실행한 상태에서 계정 파일과 서비스 설정을 먼저 준비하고 마지막에 재시작합니다.

EC2의 `ubuntu` 사용자로 다음을 실행합니다.

```bash
cd /home/ubuntu/MotorDiagnosis
install -d -m 700 /home/ubuntu/.config/motordiagnosis
.venv/bin/python -m motor_diagnosis.auth_config init \
  --file /home/ubuntu/.config/motordiagnosis/auth-users.json \
  --username operations-admin --name 'Operations administrator' --role C
```

터미널에서 15~128자의 비밀번호/암호문을 두 번 입력합니다. 입력 문자가 안 보이는 것이 정상입니다. 비밀번호를 명령 인자·환경변수·채팅에 넣지 마세요. 기존 파일이 있으면 `init`은 덮어쓰지 않습니다.

```bash
.venv/bin/python -m motor_diagnosis.auth_config check \
  --file /home/ubuntu/.config/motordiagnosis/auth-users.json
```

실행 결과만 확인하고 계정 파일 내용은 화면 캡처나 대화에 공유하지 마세요.

## 2. 기존 systemd 서비스에 경로 추가

```bash
sudo systemctl edit motordiagnosis
```

편집 화면의 적용 영역에 다음을 추가합니다. 기존 서비스는 `User=ubuntu`, `WorkingDirectory=/home/ubuntu/MotorDiagnosis`, `.venv/bin/python app.py`를 사용하는 구성을 전제로 합니다.

```ini
[Service]
Environment=APP_ENV=production
Environment=DEMO_ENABLED=false
Environment=AUTH_USERS_FILE=/home/ubuntu/.config/motordiagnosis/auth-users.json
```

파일 경로만 서비스에 저장됩니다. 비밀번호나 암호 해시를 서비스 설정에 넣지 않습니다.

```bash
sudo systemctl daemon-reload
sudo systemctl restart motordiagnosis
sudo systemctl status motordiagnosis --no-pager -l
curl --max-time 10 -i http://127.0.0.1:8787/api/health
```

`active (running)` 및 `200 OK` 확인 후 새 계정 로그인을 확인합니다. 브라우저 검증은 SSH 터널 또는 보안 설정을 마친 HTTPS 경유로 진행합니다. 실패하면 `sudo journalctl -u motordiagnosis -n 40 --no-pager`로 확인하세요. 파일 설정을 지우거나 개발 모드로 바꾸는 방법으로 해결하지 마세요.

## 3. 계정 추가·비밀번호 교체

아래 명령도 비밀번호는 화면에 표시하지 않고 직접 입력받습니다.

```bash
cd /home/ubuntu/MotorDiagnosis
.venv/bin/python -m motor_diagnosis.auth_config add \
  --file /home/ubuntu/.config/motordiagnosis/auth-users.json \
  --username operator01 --name 'Operator 01' --role A --site SITE-01
.venv/bin/python -m motor_diagnosis.auth_config passwd \
  --file /home/ubuntu/.config/motordiagnosis/auth-users.json \
  --username operations-admin
```

추가·교체는 임시 파일 저장 성공 후 원자적으로 교체하며, 실패 시 기존 파일을 보존합니다. 동시 CLI 실행은 잠금 파일로 차단합니다. 비정상 강제 종료로 `.lock` 파일이 남았다면 실행 중인 계정 설정 작업이 없는지 확인한 후에만 해당 잠금을 제거하세요.

계정 파일을 백업할 때는 DB와 별도로 암호화하여 보관하고 사용자 ID를 유지하세요. 비밀번호/권한 변경은 다음 인증 요청부터 반영되므로 재시작은 필수가 아닙니다. 파일을 수동으로 수정할 경우에도 `check`로 검증한 파일을 원자적으로 교체하세요.

## 외부 공개 전 남은 조건

이 변경은 **운영자 로그인 계정**을 보호합니다. HTTPS 인증서·도메인, 프록시, ESP32 장치 인증, CORS 허용 출처, 운영 데이터·백업 설정은 별도 단계입니다. 특히 현재 수집 경로의 기본 `demo-telemetry-ingest-token`/`demo-mqtt-ingest-token`은 이 변경만으로 차단되지 않습니다. 운영용 수집 인증 설정·기본 수집 키 차단을 완료하기 전에는 수집 API를 인터넷에 공개하지 마세요.

모델 활성화도 별도입니다. `SHADOW_MODEL_ARTIFACT` 등을 설정하지 않은 서버를 실시간 모델 추론이 동작하는 상태로 간주하지 않습니다.
