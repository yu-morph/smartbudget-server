"""소비 분석 리포트 API의 생성·조회·삭제·격리를 검증합니다."""

from datetime import date, datetime, timezone
from pathlib import Path
from threading import Event, Thread

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
def client(tmp_path: Path, monkeypatch):
    """독립 SQLite DB를 사용하는 테스트 클라이언트를 만듭니다."""
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        database_path=tmp_path / "report.sqlite3",
    )
    monkeypatch.setattr(
        "smartbudget_server.report.router.generate_report",
        lambda settings, aggregate: "# 소비 분석\n테스트 리포트",
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
    assert (
        response.json()["message"]
        == "No transactions were found for the requested period."
    )


def test_report_rejects_invalid_period_boundaries(client):
    """시작 경계가 틀리거나 미래인 기간은 거래 조회보다 먼저 거부합니다."""
    token = token_for(client)
    for period_type, start in [
        ("weekly", "2026-09-16"),
        ("monthly", "2026-09-16"),
        ("yearly", "2026-09-16"),
        ("daily", "9999-01-01"),
    ]:
        response = client.post(
            PREFIX,
            headers=headers(token),
            json={"period_type": period_type, "period_start": start},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"age": 9},
        {"age": 151},
        {"age": "35"},
        {"age": True},
        {"news": "기사 제목"},
        {"news": [123]},
        {"loan": 10000},
        {"transactions": []},
    ],
)
def test_report_rejects_invalid_context(client, invalid_fields):
    """앱 입력의 자료형·나이 범위를 검증하고 거래는 서버에서만 추가합니다."""
    token = token_for(client)
    response = client.post(
        PREFIX,
        headers=headers(token),
        json={
            "period_type": "daily",
            "period_start": "2026-09-16",
            **invalid_fields,
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


def test_report_provider_failure_keeps_existing_report(client, monkeypatch):
    """외부 분석 실패는 기존 리포트를 덮어쓰지 않습니다."""
    from smartbudget_server.report.provider import ProviderFailureError

    token = token_for(client)
    add_transaction(client)
    body = {"period_type": "daily", "period_start": "2026-09-16"}
    first = client.post(PREFIX, headers=headers(token), json=body).json()["data"]

    def fail(settings, aggregate):
        """외부 서비스 오류를 재현합니다."""
        raise ProviderFailureError()

    monkeypatch.setattr("smartbudget_server.report.router.generate_report", fail)
    failed = client.post(PREFIX, headers=headers(token), json=body)
    assert failed.status_code == 502
    assert failed.json()["code"] == "REPORT_PROVIDER_ERROR"
    assert (
        client.get(f"{PREFIX}/{first['id']}", headers=headers(token)).json()["data"][
            "full_report"
        ]
        == first["full_report"]
    )


def test_report_idempotency_replays_and_rejects_conflicting_requests(
    client, monkeypatch
):
    """동일 키와 요청은 재호출 없이 응답을 재사용하고 다른 요청은 거부합니다."""
    token = token_for(client)
    add_transaction(client)
    calls = []

    def provider(settings, aggregate):
        """실제 분석 대신 호출 수를 추적합니다."""
        calls.append(aggregate)
        return "# 결과"

    monkeypatch.setattr("smartbudget_server.report.router.generate_report", provider)
    request_headers = {**headers(token), "Idempotency-Key": "same-key"}
    context = {
        "gender": "여성",
        "age": 35,
        "job": "공무원",
        "company": "공공기관",
        "education": "대학교 졸업",
        "loan": "대출 없음",
        "approach": "절약",
        "interest": "교통비",
        "news": ["2026-09-15/대중교통 요금 동결"],
    }
    body = {"period_type": "daily", "period_start": "2026-09-16", **context}
    first = client.post(PREFIX, headers=request_headers, json=body)
    second = client.post(PREFIX, headers=request_headers, json=body)
    conflicting = client.post(
        PREFIX,
        headers=request_headers,
        json={**body, "interest": "여행비"},
    )
    assert first.status_code == 200
    assert second.json() == first.json()
    assert len(calls) == 1
    assert calls[0] == {
        "period_start": "2026-09-16",
        "period_end": "2026-09-16",
        **context,
        "transactions": [("2026-09-16", "expense", "food", 1000)],
    }
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_report_concurrent_same_period_is_rejected(client, monkeypatch):
    """같은 기간의 병렬 분석 요청은 두 번째 외부 호출 없이 거부합니다."""
    token = token_for(client)
    add_transaction(client)
    entered, release = Event(), Event()

    def provider(settings, aggregate):
        """첫 요청이 진행 중인 상황을 만듭니다."""
        entered.set()
        assert release.wait(10)
        return "# 결과"

    monkeypatch.setattr("smartbudget_server.report.router.generate_report", provider)
    body = {"period_type": "daily", "period_start": "2026-09-16"}
    responses = []
    worker = Thread(
        target=lambda: responses.append(
            client.post(PREFIX, headers=headers(token), json=body)
        )
    )
    worker.start()
    try:
        assert entered.wait(10)
        second = client.post(PREFIX, headers=headers(token), json=body)
        assert second.status_code == 409
        assert second.json()["code"] == "REPORT_GENERATION_IN_PROGRESS"
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert responses[0].status_code == 200


def test_report_does_not_save_if_transactions_change_during_generation(
    client, monkeypatch
):
    """분석 중 거래가 바뀌면 오래된 결과를 최신 리포트로 저장하지 않습니다."""
    token = token_for(client)
    add_transaction(client)
    body = {"period_type": "daily", "period_start": "2026-09-16"}
    first = client.post(PREFIX, headers=headers(token), json=body).json()["data"]

    def provider(settings, aggregate):
        """모델 호출 중 같은 기간 거래가 추가된 상황을 재현합니다."""
        add_transaction(client, amount=5000)
        return "# 오래된 결과"

    monkeypatch.setattr("smartbudget_server.report.router.generate_report", provider)
    result = client.post(PREFIX, headers=headers(token), json=body)
    assert result.status_code == 503
    saved = client.get(f"{PREFIX}/{first['id']}", headers=headers(token)).json()["data"]
    assert saved["full_report"] == first["full_report"]


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


def test_report_pagination_uses_generated_order_after_regeneration(client):
    """오래된 ID가 재생성되어 최신이 되어도 다음 페이지에서 누락되지 않습니다."""
    from datetime import timedelta

    from smartbudget_server.report.models import Report

    token = token_for(client)
    add_transaction(client, day=date(2026, 9, 16))
    add_transaction(client, day=date(2026, 9, 17))
    first = client.post(
        PREFIX,
        headers=headers(token),
        json={"period_type": "daily", "period_start": "2026-09-16"},
    ).json()["data"]
    second = client.post(
        PREFIX,
        headers=headers(token),
        json={"period_type": "daily", "period_start": "2026-09-17"},
    ).json()["data"]
    with write_session(client.app.state.engine) as session:
        session.get(Report, first["id"]).generated_at = datetime.now(
            timezone.utc
        ) + timedelta(seconds=1)
    page = client.get(PREFIX, headers=headers(token), params={"limit": 1}).json()[
        "data"
    ]
    assert page["items"][0]["id"] == first["id"]
    next_page = client.get(
        PREFIX,
        headers=headers(token),
        params={"limit": 1, "cursor": page["next_cursor"]},
    ).json()["data"]
    assert [item["id"] for item in next_page["items"]] == [second["id"]]
