"""월별 예산 API의 조회·저장·검증·사용자 격리를 검증합니다."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartbudget_server.config import Settings
from smartbudget_server.main import create_app
from tests.auth.helpers import signin, signup

PREFIX = "/api/v1/monthly-budget"


@pytest.fixture
def client(tmp_path: Path):
    """독립 SQLite DB를 사용하는 테스트 클라이언트를 만듭니다."""
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        database_path=tmp_path / "monthly-budget.sqlite3",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def token_for(client, username="User123", display_name="홍길동"):
    """테스트 사용자를 등록하고 인증 토큰을 반환합니다."""
    assert (
        signup(client, username=username, display_name=display_name).status_code == 200
    )
    return signin(client, username=username.lower()).json()["data"]["token"]


def headers(token):
    """Bearer 인증 헤더를 만듭니다."""
    return {"Authorization": "Bearer " + token}


def test_monthly_budget_defaults_to_unset(client):
    """저장된 예산이 없으면 amount null을 반환합니다."""
    token = token_for(client)
    response = client.get(f"{PREFIX}/2026-09", headers=headers(token))

    assert response.status_code == 200
    assert response.json()["data"] == {"month": "2026-09", "amount": None}


def test_monthly_budget_create_replace_and_unset(client):
    """예산을 생성·교체하고 null로 미설정 상태를 저장할 수 있습니다."""
    token = token_for(client)

    created = client.put(
        f"{PREFIX}/2026-09", headers=headers(token), json={"amount": 500000}
    )
    assert created.status_code == 200
    assert created.json()["data"] == {"month": "2026-09", "amount": 500000}

    replaced = client.put(
        f"{PREFIX}/2026-09", headers=headers(token), json={"amount": 700000}
    )
    assert replaced.status_code == 200
    assert replaced.json()["data"]["amount"] == 700000

    unset = client.put(
        f"{PREFIX}/2026-09", headers=headers(token), json={"amount": None}
    )
    assert unset.status_code == 200
    assert unset.json()["data"]["amount"] is None

    fetched = client.get(f"{PREFIX}/2026-09", headers=headers(token))
    assert fetched.json()["data"]["amount"] is None


def test_monthly_budget_requires_bearer(client):
    """조회와 저장 모두 Bearer 인증을 요구합니다."""
    assert client.get(f"{PREFIX}/2026-09").status_code == 401
    assert client.put(f"{PREFIX}/2026-09", json={"amount": 1}).status_code == 401


def test_monthly_budget_isolated_by_user(client):
    """같은 월의 예산도 사용자별로 독립적으로 저장됩니다."""
    first = token_for(client, "First123", "첫째")
    second = token_for(client, "Second123", "둘째")

    assert (
        client.put(
            f"{PREFIX}/2026-09", headers=headers(first), json={"amount": 100000}
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"{PREFIX}/2026-09", headers=headers(second), json={"amount": 200000}
        ).status_code
        == 200
    )

    assert (
        client.get(f"{PREFIX}/2026-09", headers=headers(first)).json()["data"]["amount"]
        == 100000
    )
    assert (
        client.get(f"{PREFIX}/2026-09", headers=headers(second)).json()["data"][
            "amount"
        ]
        == 200000
    )


def test_monthly_budget_validates_month_and_amount(client):
    """월 형식과 예산 금액 범위를 검증합니다."""
    token = token_for(client)

    assert client.get(f"{PREFIX}/2026-13", headers=headers(token)).status_code == 400
    assert (
        client.put(
            f"{PREFIX}/2026-09", headers=headers(token), json={"amount": -1}
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"{PREFIX}/2026-09", headers=headers(token), json={"amount": 10000001}
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"{PREFIX}/2026-09", headers=headers(token), json={"amount": "500000"}
        ).status_code
        == 400
    )
    assert (
        client.put(f"{PREFIX}/2026-09", headers=headers(token), json={}).status_code
        == 400
    )
