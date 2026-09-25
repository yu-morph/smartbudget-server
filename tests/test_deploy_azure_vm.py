"""Azure VM 배포 스크립트의 입력 검증을 확인합니다."""

import subprocess
from pathlib import Path

DEPLOY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_azure_vm.sh"


def test_deployment_rejects_malformed_commit_sha_before_checkout_access():
    """커밋 SHA 형식이 잘못되면 VM checkout을 검사하기 전에 중단합니다."""
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "/tmp/nonexistent", "invalid-sha"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "commit SHA" in result.stderr


def test_deployment_rejects_checkout_without_compose_file():
    """Compose 파일이 없는 경로를 배포 checkout으로 받아들이지 않습니다."""
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "/tmp/nonexistent", "0" * 40],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "docker-compose.yml" in result.stderr
