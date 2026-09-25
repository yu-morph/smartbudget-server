# Azure 개발 서버 배포 설계

상태: Azure VM 배포 완료. `main` CI 성공 후 Azure 자동 배포를 구성했으며, 이 브랜치 변경이 `main`에 반영된 뒤 첫 배포를 확인해야 한다.

## 목표와 범위

이 문서는 2026-09-25 최초 `develop` 배포와 이후 `main` 자동 배포 운영을 함께 기록한다. 도메인을 구매하지 않고 Azure가 제공하는 DNS 이름을 사용한다. GHCR은 비공개로 유지하고 이미지 게시 workflow는 수정하지 않는다. 서버는 개발 환경이며, API 스텁이 남아 있으므로 완성된 서비스나 실사용 데이터용 프로덕션 환경으로 안내하지 않는다.

## 승인된 기본 구성

- 지역: Korea Central
- VM 아키텍처: x86_64
- OS: Ubuntu LTS
- 최소 사양: Standard_B1ms, 1 vCPU·2 GiB 메모리.
- 네트워크: Azure 관리 공인 IP와 Azure DNS 이름 `smartbudget-dev-kc-260925.koreacentral.cloudapp.azure.com`.
- 웹 접속: HTTPS를 제공하며 외부에는 웹 포트만 연다. SSH는 현재 관리자 공인 IP로 제한한다.
- 데이터: SQLite 파일은 컨테이너 밖의 영속 디스크 경로에 둔다. `.env`와 JWT 서명 키는 저장소·이미지에 포함하지 않는다.
- 최초 배포 이미지: VM에서 `develop` checkout의 Dockerfile로 직접 빌드했다. GHCR의 `latest` 이미지를 pull하지 않는다.

## 구현 원칙

1. VM은 저장소의 `main`에 포함된 이벤트 커밋을 checkout하고 앱 서비스만 빌드·실행한다. Compose의 `--pull never`는 GHCR 이미지를 pull하지 않게 한다.
2. `.github/workflows/test.yml`은 기존 검증 job이 성공한 `main` push에서만 Azure VM 배포를 실행한다. PR과 다른 브랜치 push는 검증만 한다. GitHub Actions는 OIDC로 로그인하고 VM의 Run Command만 호출한다.
3. Compose의 `smartbudget-server` 서비스만 실행하고 `--no-deps`를 지정해 Watchtower를 시작하지 않는다. Watchtower 자동 이미지 감지는 이 배포 경로에서 사용하지 않는다.
4. SQLite와 `.env`는 권한을 제한한다. 백업은 현재 설정하지 않았으며 실제 가계부 데이터를 받기 전에 별도 위치에 복구 가능한 백업이 필요하다.
5. TLS 종료는 Azure가 제공한 DNS 이름을 대상으로 설정하고 외부 HTTPS 요청으로 인증서를 확인한다.

## 비용 기준

2026-09-25 Azure Retail Prices API 기준 Linux B1ms는 시간당 USD 0.026(월 730시간 기준 USD 18.98), Standard IPv4 정적 공인 IP는 시간당 USD 0.005(월 USD 3.65), 32 GiB Standard SSD E4 LRS는 월 USD 2.40이다. 합계는 약 USD 25.03/월이며, 데이터 송신·세금·백업·구독별 할인을 포함하지 않는다. 실제 구독 청구액은 달라질 수 있다. [Azure Retail Prices API 안내](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)

## 실제 배포 상태

- 생성일: 2026-09-25
- 리소스 그룹: `rg-smartbudget-dev-koreacentral`
- VM: `vm-smartbudget-dev-kc`, Korea Central, `Standard_B1ms`, Ubuntu 24.04 x86_64
- OS 디스크: 32 GiB Standard SSD LRS
- 주소: `https://smartbudget-dev-kc-260925.koreacentral.cloudapp.azure.com`
- 앱 소스: `origin/develop`, 배포 커밋 `35167adadf58c797c79a9dbc0ed5cac6d3e25346`
- NSG: SSH 22는 현재 관리자 IPv4 `/32`만 허용, HTTP 80과 HTTPS 443 허용. 앱 포트 8000의 인터넷 인바운드 규칙은 없다.
- 최초 실행 구성: `smartbudget-server` 서비스만 로컬 빌드하여 실행했다. Watchtower는 실행하지 않는다.
- Caddy가 HTTPS를 종료하고 앱으로 전달한다. `.env`는 VM에서 생성했고 권한은 `600`, DB 데이터 디렉터리는 컨테이너 UID `10001` 소유로 권한 `700`이다. 비밀값은 저장소에 기록하지 않는다.
- 최초 확인: 컨테이너 health `healthy`; HTTPS `/api/v1/version`, `/openapi.json`, `/docs`가 모두 HTTP 200. HTTP 요청은 HTTPS로 리디렉션된다.
- 루트 주소(`/`)에는 별도 페이지나 라우트가 없다. 따라서 브라우저에서 `/`에 접속하면 공통 HTTP 오류 처리기가 `{"status":404,"code":"INVALID_REQUEST","message":"The requested operation is not available.","data":null}`를 반환한다. 이는 서버 중단이 아니라 API 서버에 등록되지 않은 경로의 정상적인 404 응답이다. API 문서는 `/docs`, 구현 중인 API 목록은 `/openapi.json`, 배포 버전은 `/api/v1/version`에서 확인한다. 2026-09-25 외부 HTTPS 요청으로 루트 404와 이 세 경로의 HTTP 200을 확인했다.
- SSH: `ssh -i ~/.ssh/smartbudget-dev-azure azureuser@smartbudget-dev-kc-260925.koreacentral.cloudapp.azure.com`

## 자동 배포 설정

- Entra 앱 `smartbudget-server-main-deploy`는 `yu-morph/smartbudget-server`의 `main` ref OIDC 토큰만 신뢰한다.
- 사용자 지정 역할 `SmartBudget VM Run Command`에는 `Microsoft.Compute/virtualMachines/runCommand/action`만 있으며 대상 VM 리소스에만 할당했다.
- GitHub Actions 비밀값: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.
- GitHub Actions 변수: `AZURE_RESOURCE_GROUP`, `AZURE_VM_NAME`, `AZURE_VM_APP_DIR`, `AZURE_APP_BASE_URL`.
- 앱 비밀값은 GitHub에 등록하지 않았다. 기존 비공개 GHCR 게시 workflow는 별도로 유지한다.
- Actions는 Azure 인증 전 이벤트 SHA와 현재 `main` SHA를 비교해 오래된 실행을 건너뛴다. Run Command는 배포 스크립트를 명시적으로 Bash로 실행하고 성공 SHA 마커를 확인한다.
- Compose는 컨테이너 health가 healthy가 될 때까지 최대 120초 기다린다. 배포 성공 확인은 `/api/v1/version`의 `data.version`과 workflow 이벤트 SHA 비교로 한다. `/`는 점검 주소로 사용하지 않는다.
- 현재 VM은 아직 `develop` 커밋을 실행하고 있다. CI/CD 변경이 `main`에 도달한 후 첫 자동 배포 및 실행 SHA를 확인해야 한다.

## 복구

배포 workflow가 실패하면 해당 GitHub Actions 실행에서 Azure 로그인, Run Command, HTTPS 버전 확인 단계의 오류를 확인한다. VM에서 수동 복구할 때는 저장소 루트에서 이전에 정상 실행된 `main` 커밋 SHA를 지정한다.

```sh
bash scripts/deploy_azure_vm.sh /home/azureuser/smartbudget-server <정상-커밋-SHA> yu-morph/smartbudget-server
curl --fail --silent --show-error https://smartbudget-dev-kc-260925.koreacentral.cloudapp.azure.com/api/v1/version
```

이 스크립트는 SQLite 데이터 볼륨이나 `.env`를 삭제하지 않는다. 데이터 스키마 변경이 포함된 배포는 이전 앱 코드만 되돌려도 복구되지 않을 수 있으므로 DB 백업·복구 가능성을 먼저 확인한다.

## 남은 운영 항목

- B1ms는 최소 개발 후보이며 CPU 크레딧이 소진되면 성능 제한이 생길 수 있다.
- 월별 예산·거래 CRUD·소비 리포트·OCR API 일부는 현재 구현 스텁이다.
- 자동 백업은 설정하지 않았다. 실제 가계부 데이터를 넣기 전 별도 위치에 복구 가능한 백업을 구성해야 한다.
- 현재 운영 코드는 `develop`에서 배포됐으며, 앞으로는 `main`에 반영되어 CI를 통과한 커밋이 자동 배포된다. 수동 복구 외에는 VM에서 브랜치를 직접 업데이트하지 않는다.
- 실제 사용자 데이터 기준의 부하·보안 검증은 아직 수행되지 않았다.
