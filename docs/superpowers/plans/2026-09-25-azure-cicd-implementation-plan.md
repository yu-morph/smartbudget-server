# Azure CI/CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CI가 통과한 `main` 커밋을 기존 Azure 개발 VM에 자동 배포하고 실제 실행 SHA를 확인한다.

**Architecture:** 기존 `test.yml` 검증 job에 종속되는 main 전용 Azure 배포 job을 추가한다. GitHub Actions는 OIDC로 Azure에 로그인하고 VM Run Command를 통해 정확한 SHA를 VM에서 checkout·빌드·실행한다. 기존 비공개 GHCR 게시 흐름은 유지하며 Azure VM 배포는 VM의 소스 빌드 경로를 사용한다.

**Tech Stack:** GitHub Actions YAML, Microsoft Entra workload identity federation, Azure CLI `az vm run-command invoke`, Ubuntu Bash, Git, Docker Compose, FastAPI version API.

**Spec:** `docs/superpowers/specs/2026-09-25-azure-cicd-design.md`

## Global Constraints

- 배포 트리거는 `main` push이며 pull request에서는 배포하지 않는다.
- GitHub Actions는 Azure OIDC를 사용하고 장기 Azure client secret을 보관하지 않는다.
- Azure 권한은 대상 VM에서 Run Command를 실행하는 범위로 제한한다.
- VM에서 `github.repository`의 `main`에 포함되고 `github.sha`와 일치하는 커밋만 빌드한다.
- Compose의 `smartbudget-server` 서비스만 `--pull never --no-deps`로 실행한다.
- 기존 `.env`와 SQLite 데이터 디렉터리를 유지하고 볼륨을 삭제하지 않는다.
- Watchtower를 기동하지 않고 GHCR 비공개 이미지 설정을 바꾸지 않는다.
- 배포 후 HTTPS `/api/v1/version`의 버전이 배포 SHA와 일치해야 성공으로 처리한다.
- 자동 롤백은 추가하지 않는다.

## Review Focus

- pull request 이벤트에서는 Azure 배포 job을 건너뛴다. PR workflow 실행 결과에서 배포 job이 skipped인지 확인한다.
- `main`이 아닌 branch push에서는 배포 job을 건너뛴다. feature branch push 실행 결과에서 배포 job이 skipped인지 확인한다.
- OIDC 또는 Azure 설정이 빠진 경우 VM 명령 실행 전에 workflow가 실패한다. GitHub/Azure 설정 확인 단계에서 필수 변수 누락이 실행 차단되는지 확인한다.
- 손상되거나 다른 형식의 SHA와 잘못된 VM checkout 경로를 원격 스크립트가 변경 전에 거부한다. Bash 입력 검증의 실패 경로를 로컬에서 확인한다.
- Docker health가 실패하거나 공개 version API가 다른 SHA를 반환하면 배포 job이 실패하고 데이터 볼륨은 유지된다. 통합 실행 로그와 VM의 볼륨 상태로 확인한다.

---

### Task 1: VM에서 특정 커밋을 안전하게 배포하는 스크립트

**Files:**
- Create: `scripts/deploy_azure_vm.sh`
- Test: `tests/test_deploy_azure_vm.py`

**Interfaces:**
- Consumes: Azure Run Command positional arguments `app_dir`, `commit_sha`, and `source_repository`; the existing VM checkout, Docker Compose, and `.env`.
- Produces: nonzero exit for invalid inputs, dirty tracked source, Git failure, or Compose failure; zero exit after the `smartbudget-server` service is rebuilt at `commit_sha`.

- [ ] **Step 1: Add a failing input-validation test**

Create pytest cases that invoke `bash scripts/deploy_azure_vm.sh /tmp/nonexistent invalid-sha yu-morph/smartbudget-server` and an invalid repository path such as `https://attacker.invalid/repo`. Assert nonzero exits and the specific SHA/repository validation message before checking or changing the checkout. Give each test function a triple-double-quoted Korean docstring per `AGENTS.md`.

- [ ] **Step 2: Run the focused test and confirm it fails**

Run: `uv run --frozen pytest tests/test_deploy_azure_vm.py -q`
Expected: FAIL because `scripts/deploy_azure_vm.sh` does not exist yet.

- [ ] **Step 3: Implement the minimum deployment script**

The script must use `set -euo pipefail`; require exactly three arguments; require `source_repository` to match two safe GitHub path components; require an absolute existing Git checkout with `docker-compose.yml`; require a 40-character lowercase hexadecimal SHA before inspecting the checkout; use per-command `git -c safe.directory="$app_dir"`; fail if tracked or staged files are dirty; fetch `main` from `https://github.com/$source_repository.git`; require the requested SHA to exist and be an ancestor of fetched `main`; detach checkout at that SHA; compute a UTC build timestamp; and run only:

```sh
sudo env BUILD_VERSION="$commit_sha" BUILD_TIMESTAMP="$build_timestamp" \
  docker compose --project-directory "$app_dir" \
  up -d --build --pull never --no-deps smartbudget-server
```

Do not run `docker compose down`, remove `.env`, alter `data/`, or start Watchtower.

- [ ] **Step 4: Run the focused tests and script syntax check**

Run: `uv run --frozen pytest tests/test_deploy_azure_vm.py -q`
Expected: PASS for malformed SHA, unsafe repository path, and invalid directory. Run `bash -n scripts/deploy_azure_vm.sh`; expected exit code 0.

- [ ] **Step 5: Commit the deploy script and test**

```bash
git add scripts/deploy_azure_vm.sh tests/test_deploy_azure_vm.py
git commit -m "feat: Azure VM 커밋 배포 스크립트 추가"
```

### Task 2: CI 성공 뒤 main 전용 Azure 배포 job 추가

**Files:**
- Modify: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: successful `test` job, GitHub OIDC token, repository variables `AZURE_RESOURCE_GROUP`, `AZURE_VM_NAME`, `AZURE_VM_APP_DIR`, `AZURE_APP_BASE_URL`, and secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.
- Produces: a deployment job that runs only for `push` to `refs/heads/main`, reports failure on a failed remote deploy or version mismatch, and serializes VM deployments without canceling the active one.

- [ ] **Step 1: Add deploy job conditions and least workflow permissions**

Add a job with `needs: test`, condition `github.event_name == 'push' && github.ref == 'refs/heads/main'`, job-level permissions `contents: read` and `id-token: write`, timeout, and job-level concurrency group `azure-main-deploy` with `cancel-in-progress: false`. Keep the existing verification job triggers unchanged.

- [ ] **Step 2: Add OIDC login and invoke the VM deploy script**

Use `azure/login@v3` with OIDC and only the three Azure identity secrets. Validate that the four repository variables are nonempty, check out the event SHA, load `scripts/deploy_azure_vm.sh`, then call `az vm run-command invoke` for `RunShellScript` on the configured resource group and VM with `--parameters "$AZURE_VM_APP_DIR" "$GITHUB_SHA" "$GITHUB_REPOSITORY"`. Keep the script output concise because Azure Run Command response output is limited.

- [ ] **Step 3: Add the external deployment check**

Poll `"$AZURE_APP_BASE_URL/api/v1/version"` with a bounded retry loop using `curl --fail --silent --show-error`; parse the JSON using `jq`; require `status == 200` and `.data.version == "$GITHUB_SHA"`. Fail the job if retries expire or the SHA differs. Never use `/` as a health check because its documented behavior is 404.

- [ ] **Step 4: Check YAML and event gating**

Run the repository harness command `uv run --frozen python scripts/harness.py verify`. Open a pull request to `develop`; GitHub Actions must parse the workflow, run CI successfully, and skip the Azure deploy job. Do not use workflow dispatch to bypass the `main` push condition.

- [ ] **Step 5: Commit the workflow change**

```bash
git add .github/workflows/test.yml
git commit -m "ci: main 검증 통과 후 Azure 자동 배포"
```

### Task 3: Configure Azure OIDC and repository variables

**Files:**
- Configure: Microsoft Entra federated credential and Azure RBAC for the `yu-morph/smartbudget-server` repository's `main` ref
- Configure: GitHub repository Actions secrets and variables

**Interfaces:**
- Consumes: Azure subscription, tenant, VM resource group and name, VM's actual checkout path, and Entra permissions to create a federated credential and role assignment.
- Produces: OIDC login restricted to `yu-morph/smartbudget-server` `main`, with a custom role containing only `Microsoft.Compute/virtualMachines/runCommand/action` assigned at the target VM resource scope; required GitHub secrets and variables.

- [x] **Step 1: Confirm the live Azure resource identifiers and checkout path**

Read the Azure subscription, tenant, VM resource group and VM name; use a read-only Azure VM Run Command inspection to confirm the existing clone directory, Git `origin` URL, Docker Compose availability, and public version endpoint. Do not run the deployment command in this inspection.

- [x] **Step 2: Create the branch-restricted federated credential**

Create or reuse a Microsoft Entra application for GitHub Actions. Add a federated credential with issuer `https://token.actions.githubusercontent.com`, subject `repo:yu-morph/smartbudget-server:ref:refs/heads/main`, and audience `api://AzureADTokenExchange`. Do not create a client secret.

- [x] **Step 3: Assign the single-VM Run Command role**

Create or reuse a custom role with the Run Command action `Microsoft.Compute/virtualMachines/runCommand/action`, then assign it at the exact VM resource scope. Do not grant subscription-wide Contributor or Virtual Machine Contributor access.

- [x] **Step 4: Store GitHub Actions settings**

Set secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`; set variables `AZURE_RESOURCE_GROUP`, `AZURE_VM_NAME`, `AZURE_VM_APP_DIR`, and `AZURE_APP_BASE_URL`. Confirm no app secret, JWT key, database credential, or SSH private key is added to GitHub.

- [x] **Step 5: Record setup instructions**

Record only non-secret resource names, required secret/variable names, role scope, and configuration status in the Azure deployment document. Never record credential values.

### Task 4: Document automated deployment and recovery

**Files:**
- Modify: `docs/azure-deployment-design.md`
- Modify: `docs/server.md`
- Modify: `docs/superpowers/specs/2026-09-25-azure-cicd-design.md`
- Modify: `docs/index.md` only if document purpose or read condition changes

**Interfaces:**
- Consumes: deployed workflow behavior and configured Azure resources.
- Produces: accurate distinction between automatic main deployment and manual source updates; links to required GitHub settings, readiness endpoint, and manual recovery procedure.

- [x] **Step 1: Replace outdated manual-only statements**

Update the deployment status and operating procedure: main pushes deploy automatically after CI success; feature branches and PRs do not deploy; GHCR publication remains private and separate; Watchtower is not started; users can still manually run the documented VM script for recovery.

- [x] **Step 2: Document the smoke check and recovery**

State that the root URL may return 404, and the deployment check uses `/api/v1/version` and compares the SHA. Document how to inspect failed workflow/Run Command output, restore a previous source SHA without removing the SQLite volume, and confirm the restored version API response.

- [x] **Step 3: Record implementation state and check documentation links**

Update the spec status only after all implementation and Azure setup steps succeed. Check all new docs links resolve and run `git diff --check`.

- [ ] **Step 4: Commit documentation updates**

```bash
git add docs/azure-deployment-design.md docs/server.md docs/superpowers/specs/2026-09-25-azure-cicd-design.md docs/index.md
git commit -m "docs: Azure CI/CD 운영 절차 반영"
```

### Task 5: Verify the upstream PR gate and first main deployment

**Files:**
- Verify: `.github/workflows/test.yml`, `scripts/deploy_azure_vm.sh`, live Azure VM and HTTPS API

**Interfaces:**
- Consumes: merged workflow, OIDC identity, VM role, repository variables, and a trusted commit on `main`.
- Produces: a successful Actions run whose deployed SHA equals the `main` event SHA and whose SQLite data volume remains present.

- [ ] **Step 1: Open the feature branch PR to develop**

Open the feature branch PR to `develop`. Confirm the existing CI passes and the Azure deploy job is skipped. Confirm no Azure token is requested in this run.

- [ ] **Step 2: Verify CI passes and Azure deploy is skipped on the PR**

Follow the repository's branch review policy. Confirm Actions still skips deployment on the `develop` merge.

- [ ] **Step 3: When the approved release reaches main, observe the gated workflow**

When the approved release reaches `main`, confirm `test` passes before `deploy`. Confirm Azure OIDC login succeeds and the VM Run Command reports success.

- [ ] **Step 4: Verify the live version and persisted data**

Confirm HTTPS `/api/v1/version` returns HTTP 200 with `data.version` equal to the `main` push SHA and check that the existing database directory remains mounted. If the job fails, preserve logs, do not rerun blindly, diagnose the failed stage, and use the documented manual rollback if required.

- [ ] **Step 5: Run the project completion hook and report results**

Run the configured project completion verification and fresh Codex review. Report CI results, Azure authentication and Run Command results, live SHA check, remaining failures, and any setup step that still requires repository or Azure owner access.
