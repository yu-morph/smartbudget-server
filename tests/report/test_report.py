"""소비 분석 리포트 API의 생성·조회·삭제·격리를 검증합니다."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from smartbudget_server.auth.models import User
from smartbudget_server.config import Settings
from smartbudget_server.database import read_session, write_session
from smartbudget_server.main import create_app
from smartbudget_server.transaction.models import Transaction
from tests.auth.helpers import signin, signup

PREFIX = "/api/v1/report"


@pytest.fixture
def client(tmp_path: Path):
    """독립 SQLite DB를 사용하는 테스트 클라이언트를 만듭니다."""
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        database_path=tmp_path / "report.sqlite3",
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


def add_transaction(client, username="user123", day=date(2026, 9, 16), amount=1000):
    """리포트 생성에 사용할 거래 한 건을 DB에 추가합니다."""
    with read_session(client.app.state.engine) as session:
        user_id = session.scalar(select(User.id).where(User.username == username))
    now = datetime.now(timezone.utc)
    with write_session(client.app.state.engine) as session:
        session.add(
            Transaction(
                user_id=user_id,
                date=day,
                time=None,
                type="expense",
                source="manual",
                category="food",
                store_name=None,
                note=None,
                amount=amount,
                created_at=now,
                updated_at=now,
            )
        )


def test_report_requires_bearer(client):
    """리포트 API는 Bearer 인증을 요구합니다."""
    assert client.get(PREFIX).status_code == 401
    assert (
        client.post(
            PREFIX, json={"period_type": "daily", "period_start": "2026-09-16"}
        ).status_code
        == 401
    )


def test_report_returns_no_transactions(client):
    """대상 기간에 거래가 없으면 REPORT_NO_TRANSACTIONS를 반환합니다."""
    token = token_for(client)
    response = client.post(
        PREFIX,
        headers=headers(token),
        json={"period_type": "daily", "period_start": "2026-09-16"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "REPORT_NO_TRANSACTIONS"


def test_report_create_list_get_and_delete(client):
    """리포트를 생성한 뒤 목록·상세 조회하고 삭제할 수 있습니다."""
    token = token_for(client)
    add_transaction(client)

    created = client.post(
        PREFIX,
        headers=headers(token),
        json={"period_type": "daily", "period_start": "2026-09-16"},
    )
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["period_start"] == "2026-09-16"
    assert data["period_end"] == "2026-09-16"
    assert data["is_stale"] is False
    assert data["full_report"]

    listed = client.get(PREFIX, headers=headers(token))
    assert listed.status_code == 200
    assert len(listed.json()["data"]["items"]) == 1
    assert "full_report" not in listed.json()["data"]["items"][0]

    report_id = data["id"]
    detail = client.get(f"{PREFIX}/{report_id}", headers=headers(token))
    assert detail.status_code == 200
    assert detail.json()["data"]["full_report"] == data["full_report"]

    deleted = client.delete(f"{PREFIX}/{report_id}", headers=headers(token))
    assert deleted.status_code == 200
    assert (
        client.get(f"{PREFIX}/{report_id}", headers=headers(token)).status_code == 404
    )


def test_report_regeneration_reuses_id_and_clears_stale(client):
    """같은 기간 재생성은 기존 리포트 ID를 유지하고 stale을 해제합니다."""
    token = token_for(client)
    add_transaction(client)

    payload = {"period_type": "daily", "period_start": "2026-09-16"}
    first = client.post(PREFIX, headers=headers(token), json=payload).json()["data"]

    from smartbudget_server.report.models import Report

    with write_session(client.app.state.engine) as session:
        row = session.get(Report, first["id"])
        row.is_stale = True

    second = client.post(PREFIX, headers=headers(token), json=payload)
    assert second.status_code == 200
    assert second.json()["data"]["id"] == first["id"]
    assert second.json()["data"]["is_stale"] is False


def test_report_isolated_by_user(client):
    """다른 사용자의 리포트는 조회하거나 삭제할 수 없습니다."""
    first = token_for(client, "First123", "첫째")
    second = token_for(client, "Second123", "둘째")
    add_transaction(client, username="first123")

    created = client.post(
        PREFIX,
        headers=headers(first),
        json={"period_type": "daily", "period_start": "2026-09-16"},
    )
    report_id = created.json()["data"]["id"]

    assert (
        client.get(f"{PREFIX}/{report_id}", headers=headers(second)).status_code == 404
    )
    assert (
        client.delete(f"{PREFIX}/{report_id}", headers=headers(second)).status_code
        == 404
    )


def test_report_filters_require_period_pair(client):
    """기간 필터는 period_type과 period_start를 함께 요구합니다."""
    token = token_for(client)
    assert (
        client.get(
            PREFIX, headers=headers(token), params={"period_type": "daily"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            PREFIX, headers=headers(token), params={"period_start": "2026-09-16"}
        ).status_code
        == 400
    )
