"""Azure VM 배포 스크립트의 입력 검증을 확인합니다."""

import subprocess
from pathlib import Path

DEPLOY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_azure_vm.sh"


def test_deployment_rejects_malformed_commit_sha_before_checkout_access():
    """커밋 SHA 형식이 잘못되면 VM checkout을 검사하기 전에 중단합니다."""
    result = subprocess.run(
        [
            "bash",
            str(DEPLOY_SCRIPT),
            "/tmp/nonexistent",
            "invalid-sha",
            "yu-morph/smartbudget-server",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "commit SHA" in result.stderr


def test_deployment_rejects_checkout_without_compose_file():
    """Compose 파일이 없는 경로를 배포 checkout으로 받아들이지 않습니다."""
    result = subprocess.run(
        [
            "bash",
            str(DEPLOY_SCRIPT),
            "/tmp/nonexistent",
            "0" * 40,
            "yu-morph/smartbudget-server",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "docker-compose.yml" in result.stderr


def test_deployment_rejects_repository_value_outside_github_path():
    """GitHub 경로가 아닌 원격 값을 사용해 배포하지 않습니다."""
    result = subprocess.run(
        [
            "bash",
            str(DEPLOY_SCRIPT),
            "/tmp/nonexistent",
            "0" * 40,
            "https://attacker.invalid/repo",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "GitHub repository" in result.stderr


def test_deployment_waits_for_the_compose_service_health():
    """스크립트는 서비스 health 확인이 끝난 뒤 배포 성공을 표시합니다."""
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "--wait --wait-timeout 120 smartbudget-server" in script
    assert script.index("--wait --wait-timeout") < script.index("DEPLOYED_SHA=")
