# Azure 개발 서버 배포 및 CI/CD 운영

상태 확인일: 2026-11-25

2026-11-25 확인한 원본 저장소 `yu-morph/smartbudget-server`의 `main`과 VM 실행 버전은 모두 `2c9ea210`입니다. 해당 `main` [push 실행](https://github.com/yu-morph/smartbudget-server/actions/runs/44671428424)은 Azure 배포 및 HTTPS 버전 확인까지 성공했습니다. 이후 소비 분석 변경은 `develop`에 반영됐으며 [develop push 실행](https://github.com/yu-morph/smartbudget-server/actions/runs/45552583281)과 [PR 실행](https://github.com/yu-morph/smartbudget-server/actions/runs/45552563329)에서는 배포 작업이 건너뛰어졌습니다.

2026-11-19 재확인: 개발 VM의 버전 API는 `main` 커밋 `b4231f00b35c0896a590fd4a8ff982e2b3e63e50`을 반환했다. 해당 커밋의 [GitHub Actions 실행](https://github.com/yu-morph/smartbudget-server/actions/runs/36536297647)에서 검증 및 Azure VM 배포가 성공했다. OCR·소비 분석 기능은 PR #19로 `develop`에 병합됐지만 이 VM에 반영되지 않았다. 기능과 서버 검증의 현재 상태는 [작업 현황](backend-proxy-todo.md)을 따른다.

아래의 상태 요약과 서버 구성은 **2026-09-25 당시의 기록**이며 현재 상태로 해석하지 않는다. CI/CD 설명에서 PR #13의 목표 동작은 위 성공 실행으로 확인되었다.

이 문서는 Azure 개발 서버와 GitHub Actions 배포 파이프라인의 확인 기록 및 복구 절차를 설명합니다.

## 2026-09-25 당시 상태 요약

- Azure VM `vm-smartbudget-dev-kc`는 실행 중입니다. 주소는 `https://smartbudget-dev-kc-260925.koreacentral.cloudapp.azure.com`이며, Azure 버전 API가 실행 커밋 `35167adadf58c797c79a9dbc0ed5cac6d3e25346`과 `local_docker` 채널을 반환했습니다.
- GitHub `main`은 `886d17c6f614ee7328523acd2be461c8b39fdbb7`입니다. 이 시점의 `main` 워크플로에는 검증 작업만 있고 Azure 배포 작업은 없습니다.
- Azure 배포 워크플로 변경 PR [#13](https://github.com/yu-morph/smartbudget-server/pull/13)은 `develop` 대상의 열린 PR입니다. 검증 두 작업은 통과했고, 배포 작업은 `develop` 대상 PR이므로 건너뛰었습니다. 따라서 **자동 배포는 아직 활성화되지 않았습니다.**
- CI/CD 변경이 `main`에 반영된 뒤 `main` push에서 첫 Azure 자동 배포가 성공해야 파이프라인 활성 상태로 볼 수 있습니다.

## 2026-09-25 당시 Azure 서버 구성

| 항목 | 2026-09-25 당시 값 |
| --- | --- |
| 리소스 그룹 | `rg-smartbudget-dev-koreacentral` |
| 지역·VM | Korea Central · `vm-smartbudget-dev-kc` |
| 크기·운영체제 | `Standard_B1ms` · Ubuntu 24.04 x86_64 |
| OS 디스크 | 32 GiB Standard SSD LRS |
| 공인 IP·DNS | `52.141.20.105` · `smartbudget-dev-kc-260925.koreacentral.cloudapp.azure.com` |
| 실행 커밋 | `35167adadf58c797c79a9dbc0ed5cac6d3e25346` (`local_docker`) |
| 컨테이너 | `smartbudget-server` 하나가 실행 중·health `healthy`; VM에서 Docker 이미지 직접 빌드, Watchtower 미실행 |

VM은 `develop`의 위 커밋을 실행합니다. 컨테이너 응답의 `data.version`과 빌드 채널은 `/api/v1/version`에서 확인합니다. 현재 확인된 주소는 다음과 같습니다.

- `GET /api/v1/version`: 실행 버전 확인
- `/docs`: 구현된 API 문서
- `/openapi.json`: 서버가 제공하는 API 명세
- `/`: 별도 라우트가 없어 `404 INVALID_REQUEST`를 반환합니다. 서버 장애 표시가 아니며 헬스 확인 주소로 사용하지 않습니다.

네트워크 보안 그룹은 SSH 22번을 관리자 단일 IPv4 `/32`에서만 허용하고, HTTP 80·HTTPS 443을 외부에 허용합니다. 앱 포트 8000은 외부에 열지 않습니다. 관리자 IP가 바뀌면 SSH 허용 주소를 갱신해야 합니다. Caddy가 HTTPS를 종료합니다. `.env`와 JWT 키는 VM에만 두며 저장소나 이미지에는 넣지 않습니다.

SQLite 파일은 컨테이너 밖의 `./data`에 저장합니다. 자동 백업은 설정되어 있지 않습니다. 일부 월별 예산·거래·리포트·OCR API는 스텁이며, 실사용 데이터 보호를 위한 백업·복구와 실제 사용자 부하 검증도 확인되지 않았습니다. 그러므로 현재 상태를 실사용 서비스 준비 완료로 간주하지 않습니다.

## CI/CD 파이프라인

### 2026-09-25 당시 활성 상태와 활성 조건

당시 `main`의 `.github/workflows/test.yml`은 검증만 실행했고 배포 작업은 없었습니다. 이후 배포 작업이 `main`에 반영되어 위 2026-09-29 실행에서 성공했습니다. PR 또는 `develop` push만으로는 Azure 배포하지 않습니다.

PR #13 검증 시점에 저장소 설정 이름은 다음과 같이 등록되어 있음을 확인했습니다. 비밀값 자체는 문서화하지 않습니다.

- GitHub Actions secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
- GitHub Actions variables: `AZURE_RESOURCE_GROUP`, `AZURE_VM_NAME`, `AZURE_VM_APP_DIR`, `AZURE_APP_BASE_URL`

### `main` 배포 흐름

1. 모든 PR과 push에서 기존 `test` 작업이 Python 3.14 환경의 Ruff·pytest 검증을 실행합니다.
2. `main` push 또는 `main`을 선택한 수동 실행에서 `Deploy main to Azure VM` 작업이 검증 성공 후 이어집니다. PR, 다른 브랜치, 태그의 실행은 검증만 수행합니다. `develop` 변경을 배포하려면 원본 저장소의 `main`에 병합합니다.
3. GitHub Actions는 저장소의 `main` ref만 신뢰하는 Entra OIDC로 Azure에 로그인합니다. 장기 Azure 자격 증명이나 앱 비밀값을 저장하지 않습니다.
4. 배포 직전 이벤트 커밋과 현재 `main` SHA를 비교합니다. 더 오래된 실행이면 배포 관련 설정 확인, Azure 로그인 및 이후 단계를 건너뜁니다. 같은 배포 그룹의 동시 실행은 직렬화합니다.
5. Azure VM Run Command가 저장소의 [`scripts/deploy_azure_vm.sh`](../scripts/deploy_azure_vm.sh)를 Bash로 실행합니다. 스크립트는 대상 SHA가 공개 저장소 `yu-morph/smartbudget-server`의 `main` 이력에 포함되는지 확인하고 그 커밋을 체크아웃합니다.
6. VM에서 [`docker-compose.yml`](../docker-compose.yml)의 `smartbudget-server` 서비스만 로컬 빌드·갱신합니다. `--pull never`로 GHCR 이미지를 가져오지 않고, `--no-deps`로 Watchtower를 실행하지 않으며, 컨테이너 health가 최대 120초 안에 통과하는지 기다립니다.
7. GitHub Actions가 HTTPS `/api/v1/version`을 호출해 `data.version`이 배포 대상 SHA와 같은지 확인합니다. 이 검증까지 성공해야 배포 작업이 통과합니다.

`.github/workflows/deploy-azure.yml`이 검증과 Azure 배포를 정의하고, `scripts/deploy_azure_vm.sh`가 VM 내부 배포 절차를 정의합니다. 별도 `.github/workflows/deploy-ghcr.yml`은 검증 후 GHCR에 이미지를 게시합니다. Azure VM은 소스에서 직접 빌드합니다.

워크플로 변경이 기본 브랜치 `develop`과 배포 대상 `main`에 반영된 뒤, Actions에서 `Test`의 **Run workflow**를 열고 브랜치 `main`을 선택하면 현재 `main`을 수동 재배포할 수 있습니다.

## 배포 확인·복구

### VM 외부 SQLite 백업

2026-11-19 실행 중인 DB의 SQLite 온라인 백업을 Azure Storage 계정 `stsmartbudgetbkc43d1885`, 비공개 컨테이너 `sqlite-backups`의 `smartbudget-20261119T061435Z.sqlite3`로 저장했습니다. 파일 크기는 241,664바이트입니다. VM에서 다시 내려받은 파일의 SHA-256이 원본과 일치했고 `PRAGMA integrity_check`와 users·transactions·reports 테이블 읽기에 성공했습니다. 이 검증은 임시 파일에서 수행했으며 실제 서비스 DB를 교체하지 않았습니다.

저장소는 Korea Central의 Standard LRS, HTTPS 전용, 최소 TLS 1.2, Blob 익명 접근 금지, 저장 시 암호화 설정입니다. 공개 네트워크 엔드포인트는 사용하며 접근에는 인증이 필요합니다. Standard LRS는 같은 지역에 복제되므로 지역 장애에 대비하는 다른 지역 사본은 없습니다. 저장소에는 용량과 작업에 따른 요금이 발생합니다.

2026-11-19 VM의 `smartbudget-backup.timer`를 활성화했습니다. 매일 한국 시간 **03:00**에 `smartbudget-backup.service`가 실행됩니다. VM이 꺼져 예약을 놓친 경우 다시 시작할 때 누락된 실행을 한 번 수행하도록 설정했습니다. VM 관리 ID에 이 백업 컨테이너 범위의 Storage Blob Data Contributor 역할을 부여했으며 장기 저장소 키를 VM의 백업 스크립트에 저장하지 않습니다.

서비스는 SQLite 온라인 백업을 생성해 무결성을 검사하고, `sqlite-backups/daily/`에 저장한 다음 다시 읽어 SHA-256이 일치하는지 확인합니다. Azure 수명 주기 정책은 이 `daily/` 경로에 있는 파일 중 **마지막 수정 후 30일이 지난 파일만** 비동기적으로 삭제합니다. 기존 `daily/` 바깥의 수동 백업은 삭제 대상이 아닙니다. 보존 정책의 실행 시각은 정확히 30일째로 보장되지 않습니다.

서비스를 수동으로 한 번 실행해 Blob 저장과 재읽기 검증을 확인했습니다. 타이머에서 확인된 다음 예정 시각은 **2026-11-20 03:00 한국 시간**입니다. 예약 시각에 의한 첫 자동 실행은 아직 확인하지 않았습니다. 스크립트·서비스·타이머·정책의 소스는 저장소 `scripts/backup/`에 있습니다. VM에서 상태와 로그는 다음 명령으로 확인합니다.

```sh
systemctl status smartbudget-backup.timer smartbudget-backup.service
systemctl list-timers smartbudget-backup.timer
journalctl -u smartbudget-backup.service -n 30 --no-pager
```

일일 백업을 별도 디렉터리에 내려받아 해시·SQLite 무결성을 확인하고, 현재 운영 이미지로 네트워크가 차단된 독립 컨테이너를 실행해 복원된 기존 계정의 API 조회까지 확인했습니다. 운영 DB나 기존 서비스는 교체하지 않았습니다. 실제 장애 상황에서의 서비스 DB 교체와 복구 소요 시간은 아직 검증하지 않았습니다.

2026-11-19 Azure 소매 가격 API 기준 한국 중부 Hot LRS의 첫 용량 구간은 월 GB당 USD 0.02이며, 저장 요청·읽기·네트워크 등은 별도입니다. 실제 청구 금액은 계약과 사용량에 따라 달라집니다.

Azure CLI 로그인과 저장소 키 조회 권한이 있는 환경에서 임시 위치로 복구 시험할 때, 먼저 일일 백업 목록에서 필요한 시점의 파일 이름을 확인합니다. 아래 다운로드는 최초 수동 백업을 사용하는 예시이며 최신 백업이 필요한 경우 해당 파일 이름으로 바꿉니다.

```sh
export AZURE_STORAGE_KEY="$(az storage account keys list -g rg-smartbudget-dev-koreacentral -n stsmartbudgetbkc43d1885 --query '[0].value' -o tsv)"
az storage blob list --account-name stsmartbudgetbkc43d1885 \
  --container-name sqlite-backups --prefix daily/ \
  --query '[].{name:name,modified:properties.lastModified}' -o table
az storage blob download --account-name stsmartbudgetbkc43d1885 \
  --container-name sqlite-backups --name smartbudget-20261119T061435Z.sqlite3 \
  --file ./restore-test.sqlite3 --output none
unset AZURE_STORAGE_KEY
python3 -c "import sqlite3; c=sqlite3.connect('file:restore-test.sqlite3?mode=ro', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

복구 파일에는 실제 사용자 데이터가 있으므로 안전한 위치에서만 취급합니다. 서비스 DB를 교체해야 할 때는 모든 쓰기 프로세스를 중지하고 현재 DB를 별도 백업한 뒤, SQLite 저널·WAL과 파일 소유권 및 새 코드의 DB 호환성을 확인하고 복원합니다.

오래된 DB를 복원하면 백업 이후 비밀번호 변경이나 탈퇴로 무효화된 토큰이 다시 유효해지고, 예전 비밀번호와 삭제된 계정도 되살아날 수 있습니다. 서비스 재개 **전에** `JWT_SECRET`을 안전하게 새 무작위 키로 교체해 기존 토큰을 모두 무효화하고, 백업 이후의 신뢰할 수 있는 계정 변경 기록과 복원된 계정 상태를 대조해 탈퇴·차단 및 비밀번호 변경을 반영해야 합니다. 변경 기록을 확인할 수 없거나 안전한 비밀번호 재설정·본인 확인 절차가 준비되지 않았다면 영향을 받는 계정의 접근을 차단합니다. 영향 범위를 식별할 수 없다면 확인 전까지 서비스를 재개하지 않습니다. 키는 로그나 저장소에 기록하지 않으며 모든 사용자는 다시 로그인해야 합니다.

재시작 후 인증 및 주요 API를 검증합니다. 서비스 DB 교체를 포함한 전체 절차는 아직 시험하지 않았습니다.

GitHub Actions에서 `Ruff and pytest`와 `Deploy main to Azure VM` 작업을 확인합니다. 자동 배포 활성화 후 성공한 실행은 다음 주소도 확인합니다.

```sh
curl --fail --silent --show-error \
  https://smartbudget-dev-kc-260925.koreacentral.cloudapp.azure.com/api/v1/version
```

응답 `data.version`이 성공한 GitHub Actions의 커밋 SHA와 같아야 합니다. 루트 `/` 응답은 배포 상태 확인에 사용하지 않습니다.

자동 배포 실패 시 해당 Actions 실행의 Azure 로그인, Run Command, HTTPS 버전 확인 로그를 확인합니다. `main`에 반영된 정상 커밋으로 수동 복구할 때 VM의 저장소 디렉터리에서 실행합니다.

```sh
cd /home/azureuser/smartbudget-server
bash scripts/deploy_azure_vm.sh \
  /home/azureuser/smartbudget-server \
  <정상-커밋-SHA> \
  yu-morph/smartbudget-server
```

정상 SHA는 `main` 이력에 포함된 40자리 커밋이어야 합니다. 배포 스크립트는 `.env`와 SQLite 데이터 디렉터리를 삭제하지 않습니다. DB 스키마 변경이 포함된 경우 앱 코드만 되돌려도 복구되지 않을 수 있으므로, 복구 전 백업 시각과 호환성을 확인합니다.
