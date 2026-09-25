# Azure 개발 서버 배포 및 CI/CD 운영

상태 확인일: 2026-09-25

이 문서는 Azure 개발 서버의 실제 상태와 GitHub Actions 배포 파이프라인의 현재 활성 여부·동작 방식을 기록합니다. **실제 Azure 운영 상태와 아직 `main`에 반영되지 않은 PR의 목표 동작을 구분합니다.**

## 현재 상태 요약

- Azure VM `vm-smartbudget-dev-kc`는 실행 중입니다. 주소는 `https://smartbudget-dev-kc-260925.koreacentral.cloudapp.azure.com`이며, Azure 버전 API가 실행 커밋 `35167adadf58c797c79a9dbc0ed5cac6d3e25346`과 `local_docker` 채널을 반환했습니다.
- GitHub `main`은 `886d17c6f614ee7328523acd2be461c8b39fdbb7`입니다. 이 시점의 `main` 워크플로에는 검증 작업만 있고 Azure 배포 작업은 없습니다.
- Azure 배포 워크플로 변경 PR [#13](https://github.com/yu-morph/smartbudget-server/pull/13)은 `develop` 대상의 열린 PR입니다. 검증 두 작업은 통과했고, 배포 작업은 `develop` 대상 PR이므로 건너뛰었습니다. 따라서 **자동 배포는 아직 활성화되지 않았습니다.**
- CI/CD 변경이 `main`에 반영된 뒤 `main` push에서 첫 Azure 자동 배포가 성공해야 파이프라인 활성 상태로 볼 수 있습니다.

## Azure 서버의 실제 구성

| 항목 | 현재 값 |
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

### 현재 활성 상태와 활성 조건

현재 `main`의 `.github/workflows/test.yml`은 push와 pull request에서 Ruff·pytest 검증만 합니다. Azure 배포 작업은 현재 `main`에 없습니다. PR #13의 검증된 변경이 `main`에 반영되고, 그 뒤 `main`에 새 커밋이 push되어야 아래 Azure 배포 경로가 실행됩니다. PR 또는 `develop` push만으로는 Azure 배포하지 않습니다.

PR #13 검증 시점에 저장소 설정 이름은 다음과 같이 등록되어 있음을 확인했습니다. 비밀값 자체는 문서화하지 않습니다.

- GitHub Actions secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
- GitHub Actions variables: `AZURE_RESOURCE_GROUP`, `AZURE_VM_NAME`, `AZURE_VM_APP_DIR`, `AZURE_APP_BASE_URL`

### `main`에 반영될 배포 흐름

1. 모든 PR과 push에서 기존 `test` 작업이 Python 3.14 환경의 Ruff·pytest 검증을 실행합니다.
2. `main` push에서만 `Deploy main to Azure VM` 작업이 검증 성공 후 이어집니다. PR 및 다른 브랜치에서는 배포하지 않습니다.
3. GitHub Actions는 저장소의 `main` ref만 신뢰하는 Entra OIDC로 Azure에 로그인합니다. 장기 Azure 자격 증명이나 앱 비밀값을 저장하지 않습니다.
4. 배포 직전 이벤트 커밋과 현재 `main` SHA를 비교합니다. 더 오래된 실행이면 건너뜁니다. 같은 배포 그룹의 동시 실행은 직렬화합니다.
5. Azure VM Run Command가 저장소의 [`scripts/deploy_azure_vm.sh`](../scripts/deploy_azure_vm.sh)를 Bash로 실행합니다. 스크립트는 대상 SHA가 공개 저장소 `yu-morph/smartbudget-server`의 `main` 이력에 포함되는지 확인하고 그 커밋을 체크아웃합니다.
6. VM에서 [`docker-compose.yml`](../docker-compose.yml)의 `smartbudget-server` 서비스만 로컬 빌드·갱신합니다. `--pull never`로 GHCR 이미지를 가져오지 않고, `--no-deps`로 Watchtower를 실행하지 않으며, 컨테이너 health가 최대 120초 안에 통과하는지 기다립니다.
7. GitHub Actions가 HTTPS `/api/v1/version`을 호출해 `data.version`이 배포 대상 SHA와 같은지 확인합니다. 이 검증까지 성공해야 배포 작업이 통과합니다.

`.github/workflows/test.yml`이 배포 파이프라인을 정의하고, `scripts/deploy_azure_vm.sh`가 VM 내부 배포 절차를 정의합니다. 별도 `.github/workflows/publish.yml`은 검증 후 GHCR에 이미지를 게시하는 기존 작업입니다. **Azure VM 배포는 이 GHCR 게시 이미지나 Watchtower 자동 감지에 의존하지 않습니다.**

## 배포 확인·복구

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

정상 SHA는 `main` 이력에 포함된 40자리 커밋이어야 합니다. 배포 스크립트는 `.env`와 SQLite 데이터 디렉터리를 삭제하지 않습니다. DB 스키마 변경이 포함된 경우 앱 코드만 되돌려도 복구되지 않을 수 있으므로, 복구 전 DB 백업과 호환성을 확인합니다. 현재 자동 백업은 없으므로 데이터 복구 지점이 필요하면 먼저 별도 백업을 구성해야 합니다.
