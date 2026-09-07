# 운영 수집 토큰과 EC2 적용

## 적용 범위

`APP_ENV=production`에서는 기본 텔레메트리·MQTT·검증 라벨·장치 상태 토큰을 모두 `401 AUTH_REQUIRED`로 거부합니다. `DEMO_ENABLED=true`를 지정하거나 기본 토큰을 `DEVICE_HEALTH_TOKEN`에 다시 지정해도 허용하지 않습니다. 개발 모드의 기존 데모 테스트는 유지합니다.

운영 장치 인증 파일은 `INGEST_TOKENS_FILE`로 지정합니다. 경로가 없으면 전용 장치 토큰은 비활성화되며, 기본 토큰으로 대체되지 않습니다. 운영 계정이 정상 설정돼 있으면 장치 토큰 발급 전에도 관리 서버를 실행할 수 있습니다. 경로를 설정했는데 파일이 없거나 잘못되면 시작을 거부하고, 실행 중 손상·삭제되면 수집 요청을 `503 INGEST_AUTH_UNAVAILABLE`로 차단합니다.

| 종류 | 허용 범위 |
| --- | --- |
| `device` | 지정한 장치 **1대**의 텔레메트리·일괄 전송·분석 파형·모델 입력 및 장치 상태 보고 |
| `mqtt` | 명시적으로 나열한 장치들의 텔레메트리·분석 입력, MQTT 격리 보고 및 `mqtt` 서비스 상태 보고 |

모든 장치를 뜻하는 `*`, 라벨 부여·운영 화면 관리 권한, 임의 권한 추가는 허용하지 않습니다. 단건·일괄 전송은 허용되지 않은 장치의 데이터나 거부 이력을 저장하기 전에 차단합니다. MQTT 격리 보고는 `devices/{등록한 장치 ID}/telemetry`의 정확한 주제만 허용합니다. 식별할 수 없는 주제는 서버 격리 저장 권한에 포함되지 않으므로 MQTT 브리지의 로컬 오류 기록을 확인해야 합니다. 브로커의 인증·주제 ACL은 별도로 설정해야 합니다.

기존 운영자 세션의 수집 권한과 명시적으로 설정한 `DEVICE_HEALTH_TOKEN`의 상태 보고 전용 권한은 유지합니다. 후자는 기존의 전체 장치 범위이므로 새 장치별 토큰으로 이전 후 불필요한 환경변수를 제거하세요. 원격 설정(`DEVICE_CONFIG_TOKENS_JSON`)·통신 품질(`DEVICE_QUALITY_TOKENS_JSON`) 토큰은 별도이며 이 변경으로 대체되지 않습니다.

## 파일과 비밀 관리

- 서버 파일: 스키마 버전, 자격 증명 ID·종류·장치 목록과 **토큰의 SHA-256 해시만** 저장합니다.
- 클라이언트 파일: `{"token":"…"}` 형태의 원문 비밀입니다. 해당 장치/브리지에만 전달하세요.
- 토큰은 운영 CLI가 32바이트 난수로 생성합니다. 비밀번호를 입력하거나 기존 데모 문자열을 재사용하지 않습니다.
- 두 파일 모두 Git 밖의 비공개 디렉터리에 보관합니다. Linux 디렉터리 `700`, 파일 `600`, 실행 사용자 또는 root 소유가 필요합니다. Windows에서는 별도로 사용자 전용 ACL을 적용하세요.
- CLI는 토큰을 화면·로그에 출력하지 않으며 기존 클라이언트 파일을 덮어쓰지 않습니다. 클라이언트 파일과 장치의 실제 `secrets.h`를 채팅·스크린샷·Git에 올리지 마세요.
- CLI 갱신은 잠금과 원자적 교체를 사용합니다. 비정상 종료로 잠금이 남으면 다른 발급 작업이 없는지 확인하고 해당 `.lock`만 제거하세요. 실제 전원 차단 검증을 수행한 것은 아닙니다.

## EC2에서 발급

**이 변경을 검토·병합하고 EC2에 코드를 갱신한 다음 실행합니다.** 아래 장치 ID는 예시입니다. 실제 등록된 장치 ID로 바꾸세요. 운영 계정 파일 `AUTH_USERS_FILE` 설정은 유지합니다.

```bash
cd /home/ubuntu/MotorDiagnosis
install -d -m 700 /home/ubuntu/.config/motordiagnosis
.venv/bin/python -m motor_diagnosis.ingest_auth add --file /home/ubuntu/.config/motordiagnosis/ingest-tokens.json --id esp32-01 --kind device --device DEV-01-MOT-02 --token-file /home/ubuntu/.config/motordiagnosis/esp32-01.ingest-token.json
.venv/bin/python -m motor_diagnosis.ingest_auth check --file /home/ubuntu/.config/motordiagnosis/ingest-tokens.json
```

발급 성공은 `Ingest credentials saved. No secrets displayed.`, 검증 성공은 `Ingest credentials are valid. No secrets displayed.`입니다. 실패하면 재시작하지 말고 설정을 확인하세요.

MQTT를 실제로 사용하는 경우에만 별도로 발급합니다. 여러 장치에는 `--device`를 반복합니다.

```bash
.venv/bin/python -m motor_diagnosis.ingest_auth add --file /home/ubuntu/.config/motordiagnosis/ingest-tokens.json --id mqtt-01 --kind mqtt --device DEV-01-MOT-02 --token-file /home/ubuntu/.config/motordiagnosis/mqtt-01.ingest-token.json
```

## 서비스 설정과 연결

기존 `/etc/systemd/system/motordiagnosis.service.d/auth-users.conf`는 그대로 두고, 다음 명령으로 **별도 파일**을 편집합니다. `systemctl edit`의 주석 영역에 입력하는 실수를 피하기 위한 방식입니다.

```bash
sudo nano /etc/systemd/system/motordiagnosis.service.d/ingest-auth.conf
```

파일의 내용은 다음 두 줄이며 `#`을 붙이지 않습니다.

```ini
[Service]
Environment=INGEST_TOKENS_FILE=/home/ubuntu/.config/motordiagnosis/ingest-tokens.json
```

파일 검증 성공과 저장을 확인한 다음 실행합니다.

```bash
sudo systemctl daemon-reload
sudo systemctl restart motordiagnosis
sudo systemctl status motordiagnosis --no-pager -l
curl --max-time 10 -i http://127.0.0.1:8787/api/health
```

- ESP32: 해당 장치의 클라이언트 파일 안 `token` 값을 비공개 `secrets.h`의 `INGEST_TOKEN_VALUE`와 `DEVICE_HEALTH_TOKEN_VALUE`에 설정합니다. URL의 장치 ID도 일치시킵니다. MQTT 토큰이나 운영자 비밀번호를 넣지 마세요.
- MQTT 브리지: `APP_ENV=production`, `MQTT_INGEST_TOKEN_FILE=/절대/경로/mqtt-01.ingest-token.json`을 설정합니다. `--ingest-token-file` 옵션도 가능합니다. `MQTT_INGEST_TOKEN`/`--ingest-token`과 동시에 지정하면 실패합니다. 파일은 시작 시 읽으므로 교체 후 브리지를 재시작해야 합니다.
- MQTT 브로커용 인증서·사용자 설정과 백엔드 HTTPS 설정은 별도입니다. 토큰을 운영 외부 HTTP 주소로 보내거나 TLS 인증서 검증을 끄지 마세요.
- 상태 확인 `200 OK`만으로 장치 인증·추론·실제 장비 연결까지 검증됐다고 볼 수 없습니다.

## 교체·폐기

```bash
.venv/bin/python -m motor_diagnosis.ingest_auth rotate --file /home/ubuntu/.config/motordiagnosis/ingest-tokens.json --id esp32-01 --token-file /home/ubuntu/.config/motordiagnosis/esp32-01-v2.ingest-token.json
.venv/bin/python -m motor_diagnosis.ingest_auth revoke --file /home/ubuntu/.config/motordiagnosis/ingest-tokens.json --id esp32-01
```

두 명령은 대안입니다. 교체 후 곧바로 폐기하지 마세요. `rotate`는 기존 ID·장치 범위를 보존하고 즉시 이전 토큰을 무효화합니다. 장치/브리지의 비밀도 갱신해야 하며, 겹치는 유효기간을 제공하지 않으므로 정비 시간에 수행하세요. `revoke`는 해당 항목을 제거하고 다른 항목은 보존합니다. 파일은 인증 요청마다 다시 읽으므로 백엔드 재시작은 필요하지 않습니다. 이미 처리 중인 요청까지 취소하는 기능은 아닙니다.

서버 파일 복원은 폐기한 키를 되살릴 수 있으므로 백업 복원 시 클라이언트 배포 이력도 함께 확인하세요. 단순 장치 ID 재사용 전에 기존 자격 증명을 먼저 폐기하세요.

## 공개 전 점검

운영 계정 로그인, 각 기본 토큰의 `401`, 유효한 장치 보고, 다른 장치의 `403`, HTTPS 인증서 검증, CORS 출처, 데이터 보관·백업을 확인한 뒤 공개합니다. 이 개발 작업 자체는 EC2 변경·실제 보드 시험·실제 MQTT 브로커 연결을 수행하지 않습니다.
