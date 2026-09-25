# Azure 개발 서버 CI/CD 설계

상태: 사용자 검토 대기 (2026-09-25)

## 목표

`main`에 반영된 커밋이 현재 Azure 개발 VM에 자동 배포되도록 한다. `main`에 push 또는 PR merge가 발생하면 CI 검증을 먼저 통과해야 배포한다. CI 실패 시 VM은 변경하지 않는다. 배포 후 실행 중인 서버의 버전 API가 이번 커밋을 보고하는지 확인한다.

## 현재 구성

- `.github/workflows/test.yml`은 모든 push와 pull request에서 Python 3.14 환경을 준비하고 `uv run --frozen python scripts/harness.py verify`를 실행한다.
- `.github/workflows/publish.yml`은 `main`에서 같은 검증을 통과한 뒤 비공개 GHCR에 Docker 이미지를 게시한다.
- Azure VM은 현재 `develop`의 코드를 VM에서 빌드하며, GitHub Actions와 연결되어 있지 않다. Compose에서 앱 서비스만 실행해 Watchtower는 기동하지 않는다.
- Azure VM은 개발 서버이며 자동 백업과 실사용자 기준 보안·부하 검증은 아직 구성되지 않았다.

## 설계 결정

1. 배포 대상 커밋은 `main` push 이벤트의 `github.sha`로 고정한다. pull request 이벤트에서는 검증만 하고 배포하지 않는다.
2. 기존 `test.yml`의 검증 job 뒤에 Azure 배포 job을 추가한다. 배포 job은 `push` 이벤트와 `refs/heads/main`에서만 실행되고 검증 job에 의존한다. 기존 GHCR 게시 워크플로는 별도 흐름으로 유지한다.
3. GitHub Actions는 Microsoft Entra workload identity federation을 사용하는 OIDC로 Azure에 로그인한다. 장기 Azure client secret은 저장하지 않는다. 연합 자격 증명 조건은 이 저장소의 `main` 배포 워크플로로 제한한다.
4. 배포 job은 Azure VM Run Command로 대상 VM에서 배포 스크립트를 실행한다. SSH를 GitHub 러너에 개방하지 않는다. 권한은 대상 VM 범위로 제한하며 Run Command 실행에 필요한 권한만 부여한다. Run Command 스크립트는 Linux VM에서 관리자 권한으로 실행될 수 있으므로 workflow 변경은 `main` 병합 권한과 보호 규칙으로 통제한다.
5. VM은 저장소에서 이벤트의 정확한 커밋 SHA를 가져와 해당 소스에서 앱 이미지를 빌드한다. `.env`와 SQLite 데이터 디렉터리는 기존 VM 경로에 유지한다. Compose 앱 서비스만 `--pull never --no-deps`로 갱신하며 Watchtower를 시작하지 않는다.
6. 배포 명령이 성공한 뒤 외부 HTTPS에서 `/api/v1/version`을 확인하고 응답의 `version`이 `github.sha`와 같은지 검사한다. 불일치나 health 확인 실패는 Actions 실행을 실패로 표시한다.
7. Azure 리소스 그룹, VM 이름, 배포 디렉터리와 Entra 식별자 등은 GitHub Actions 변수·비밀값으로 제공한다. 앱 비밀값은 workflow나 저장소에 추가하지 않는다.

## 흐름

1. pull request와 push에서 기존 CI가 Ruff 및 pytest 검증을 실행한다.
2. `main` push에서 CI 검증 job이 성공하면 같은 workflow의 배포 job이 OIDC 토큰으로 Azure에 로그인한다.
3. Actions는 Azure Run Command로 VM의 기존 checkout을 정확한 SHA에 맞춘 뒤 Docker Compose로 앱 서비스만 빌드·교체한다.
4. Actions가 공개 HTTPS 버전 API를 검사한다. 배포 SHA가 일치해야 workflow가 성공한다.

## 실패와 복구

- CI가 실패하면 배포 job은 실행되지 않는다.
- Azure 인증·VM 명령·컨테이너 health·외부 SHA 확인 중 하나라도 실패하면 workflow는 실패한다. 실패한 배포를 성공으로 보고하지 않는다.
- 배포는 데이터 볼륨을 제거하지 않으며 `docker compose down --volumes`를 사용하지 않는다.
- 이번 구현에는 자동 롤백을 넣지 않는다. 배포 후 오류가 나면 이전의 정상 커밋 SHA를 VM에서 다시 빌드해 복구하고, SQLite 데이터 변경이 포함된 경우에는 먼저 데이터 복구 가능성을 확인한다.

## GitHub와 Azure 사전 구성

- GitHub Actions 배포 job에는 `id-token: write`와 `contents: read`만 부여한다.
- Azure에는 이 저장소의 `main`에서 발급한 OIDC 토큰만 신뢰하는 연합 자격 증명과 대상 VM 범위의 Run Command 권한이 필요하다.
- GitHub에는 Azure client ID, tenant ID, subscription ID와 리소스 그룹·VM·실제 checkout 경로를 각각 저장한다. Azure 식별자 자체는 비밀로 취급하지 않아도 되지만 workflow에서는 저장소 변수·비밀값으로 한곳에서 관리한다.
- 첫 자동 배포 전에 workflow_dispatch 또는 별도의 사전 점검으로 Azure 로그인, VM 경로, VM Git 원격 접근, Compose 빌드와 외부 version 응답을 확인한다. 사전 점검은 앱 컨테이너를 교체하지 않는 읽기 전용 명령만 사용한다.

## 검증 기준

- pull request에서 검증 job만 실행되고 배포 job은 건너뛴다.
- `main` push에서는 검증 성공 뒤 배포가 실행되고, VM 앱 버전이 해당 push SHA와 일치한다.
- 검증 실패를 재현하면 배포 job이 시작되지 않는다.
- 배포 실패를 재현하면 Actions 실행이 실패하고 VM 데이터 볼륨은 유지된다.
- Azure VM의 GitHub 러너용 SSH 인바운드 규칙이나 공개 이미지 권한을 추가하지 않는다.

## 공식 자료

- [GitHub Actions에서 Azure OpenID Connect 구성](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-azure)
- [Azure Linux VM Run Command](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/run-command)
- [Azure Compute RBAC 권한](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute)
