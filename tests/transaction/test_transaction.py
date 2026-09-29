"""거래 API의 CRUD, 인증, 사용자 격리와 리포트 stale 처리를 검증합니다."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from smartbudget_server.config import Settings
from smartbudget_server.database import read_session, write_session
from smartbudget_server.main import create_app
from smartbudget_server.report.models import Report
from smartbudget_server.transaction.models import Transaction
from tests.auth.helpers import signin, signup

PREFIX = "/api/v1/transaction"


@pytest.fixture
def client(tmp_path):
    """독립 테스트 앱과 클라이언트를 생성합니다."""
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        database_path=tmp_path / "transaction.sqlite3",
    )
    with TestClient(create_app(settings)) as result:
        yield result


def token_for(client, username="User123", display_name="홍길동"):
    """테스트 사용자를 등록하고 Bearer 토큰을 반환합니다."""
    assert (
        signup(client, username=username, display_name=display_name).status_code == 200
    )
    return signin(client, username=username.lower()).json()["data"]["token"]


def headers(token):
    """Bearer 인증 헤더를 생성합니다."""
    return {"Authorization": "Bearer " + token}


def payload(**changes):
    """기본 거래 요청 데이터에 변경 값을 적용합니다."""
    data = {
        "date": "2026-09-27",
        "type": "expense",
        "source": "manual",
        "category": "food",
        "store_name": "테스트 식당",
        "note": "점심",
        "amount": 12000,
        "time": "12:30:00",
    }
    data.update(changes)
    return data


def create(client, token, **changes):
    """인증된 거래 생성 요청을 전송합니다."""
    return client.post(PREFIX, headers=headers(token), json=payload(**changes))


def test_transaction_crud(client):
    """거래 생성·조회·수정·삭제 흐름을 검증합니다."""
    token = token_for(client)

    created = create(client, token)
    assert created.status_code == 201, created.text
    item = created.json()["data"]
    transaction_id = item["id"]
    assert item["amount"] == 12000
    assert item["source"] == "manual"

    listed = client.get(PREFIX, headers=headers(token))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]["items"]] == [transaction_id]

    detail = client.get(f"{PREFIX}/{transaction_id}", headers=headers(token))
    assert detail.status_code == 200
    assert detail.json()["data"]["store_name"] == "테스트 식당"

    updated = client.patch(
        f"{PREFIX}/{transaction_id}",
        headers=headers(token),
        json={"amount": 15000, "note": "수정"},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["amount"] == 15000
    assert updated.json()["data"]["note"] == "수정"

    deleted = client.delete(f"{PREFIX}/{transaction_id}", headers=headers(token))
    assert deleted.status_code == 200
    assert deleted.json()["data"] is None
    assert (
        client.get(f"{PREFIX}/{transaction_id}", headers=headers(token)).status_code
        == 404
    )


def test_transaction_requires_bearer(client):
    """모든 거래 API가 Bearer 인증을 요구하는지 검증합니다."""
    for response in [
        client.get(PREFIX),
        client.post(PREFIX, json=payload()),
        client.get(f"{PREFIX}/1"),
        client.patch(f"{PREFIX}/1", json={"amount": 1}),
        client.delete(f"{PREFIX}/1"),
    ]:
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_transaction_isolated_by_user(client):
    """다른 사용자의 거래에 접근할 수 없는지 검증합니다."""
    first = token_for(client)
    second = token_for(client, username="User456", display_name="다른사용자")
    transaction_id = create(client, first).json()["data"]["id"]

    assert (
        client.get(f"{PREFIX}/{transaction_id}", headers=headers(second)).status_code
        == 404
    )
    assert (
        client.patch(
            f"{PREFIX}/{transaction_id}", headers=headers(second), json={"amount": 1}
        ).status_code
        == 404
    )
    assert (
        client.delete(f"{PREFIX}/{transaction_id}", headers=headers(second)).status_code
        == 404
    )
    assert client.get(PREFIX, headers=headers(second)).json()["data"]["items"] == []


def test_transaction_filters_dates_and_paginates(client):
    """날짜 필터와 커서 페이지네이션을 검증합니다."""
    token = token_for(client)
    ids = [
        create(client, token, date="2026-09-25", amount=1000).json()["data"]["id"],
        create(client, token, date="2026-09-26", amount=2000).json()["data"]["id"],
        create(client, token, date="2026-09-27", amount=3000).json()["data"]["id"],
    ]

    filtered = client.get(
        PREFIX,
        headers=headers(token),
        params={"start_date": "2026-09-26", "end_date": "2026-09-27"},
    )
    assert [row["id"] for row in filtered.json()["data"]["items"]] == [ids[2], ids[1]]

    first_page = client.get(PREFIX, headers=headers(token), params={"limit": 2}).json()[
        "data"
    ]
    assert len(first_page["items"]) == 2
    assert first_page["next_cursor"] is not None
    second_page = client.get(
        PREFIX,
        headers=headers(token),
        params={"limit": 2, "cursor": first_page["next_cursor"]},
    ).json()["data"]
    assert [row["id"] for row in second_page["items"]] == [ids[0]]

    assert (
        client.get(
            PREFIX,
            headers=headers(token),
            params={"start_date": "2026-09-28", "end_date": "2026-09-27"},
        ).status_code
        == 400
    )


def test_transaction_validation(client):
    """잘못된 거래 입력이 거부되는지 검증합니다."""
    token = token_for(client)
    for invalid in [
        payload(amount=0),
        payload(type="expense", category="salary"),
        payload(date="not-a-date"),
    ]:
        response = client.post(PREFIX, headers=headers(token), json=invalid)
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


def test_update_and_delete_mark_related_report_stale(client):
    """거래 수정·삭제 시 관련 리포트가 stale 처리되는지 검증합니다."""
    token = token_for(client)
    transaction_id = create(client, token).json()["data"]["id"]

    with read_session(client.app.state.engine) as session:
        transaction = session.get(Transaction, transaction_id)
        user_id = transaction.user_id

    with write_session(client.app.state.engine) as session:
        report = Report(
            user_id=user_id,
            period_type="monthly",
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            full_report="test",
            requested_at=datetime.now(timezone.utc),
            generated_at=datetime.now(timezone.utc),
            processing_time_ms=1,
            is_stale=False,
        )
        session.add(report)
        session.flush()
        report_id = report.id

    assert (
        client.patch(
            f"{PREFIX}/{transaction_id}", headers=headers(token), json={"amount": 13000}
        ).status_code
        == 200
    )
    with read_session(client.app.state.engine) as session:
        assert session.get(Report, report_id).is_stale is True

    with write_session(client.app.state.engine) as session:
        session.get(Report, report_id).is_stale = False

    assert (
        client.delete(f"{PREFIX}/{transaction_id}", headers=headers(token)).status_code
        == 200
    )
    with read_session(client.app.state.engine) as session:
        assert session.get(Report, report_id).is_stale is True
