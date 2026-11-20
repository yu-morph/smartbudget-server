"""인증 계정의 생성·조회·수정·삭제와 사용자 격리를 검증합니다."""

from base64 import b64decode, b64encode
from io import BytesIO
from sqlite3 import connect

import jwt
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from smartbudget_server.auth.models import LoginAttempt, User
from smartbudget_server.config import Settings
from smartbudget_server.database import read_session
from smartbudget_server.main import create_app
from tests.auth.helpers import (
    PREFIX,
    account,
    delete_account,
    patch_account,
    signin,
    signup,
)


def test_delete_account_removes_user_and_invalidates_token(client):
    """본인 삭제 후 계정·로그인 제한·기존 JWT를 다시 사용할 수 없습니다."""
    signup(client)
    token = signin(client).json()["data"]["token"]
    assert signin(client, password="Wrong123").status_code == 401
    with read_session(client.app.state.engine) as session:
        assert session.get(LoginAttempt, "user123") is not None

    response = delete_account(client, token)

    assert response.status_code == 200
    assert response.json()["data"] is None
    assert account(client, token).status_code == 401
    with read_session(client.app.state.engine) as session:
        assert session.scalar(select(User).where(User.username == "user123")) is None
        assert session.get(LoginAttempt, "user123") is None
    assert signin(client).status_code == 401
    assert signup(client).status_code == 200
    new_token = signin(client).json()["data"]["token"]
    assert account(client, token).status_code == 401
    assert account(client, new_token).status_code == 200


def test_delete_account_keeps_other_user(client):
    """본인 삭제가 다른 사용자와 그 로그인 제한 상태에 영향을 주지 않습니다."""
    signup(client)
    signup(client, username="User456", display_name="다른사용자")
    first_token = signin(client).json()["data"]["token"]
    second_token = signin(client, username="user456").json()["data"]["token"]
    assert signin(client, password="Wrong123").status_code == 401
    assert signin(client, username="user456", password="Wrong123").status_code == 401

    assert delete_account(client, first_token).status_code == 200

    assert account(client, second_token).json()["data"] == {
        "username": "user456",
        "display_name": "다른사용자",
        "profile_image": None,
    }
    with read_session(client.app.state.engine) as session:
        assert session.scalar(select(User).where(User.username == "user456"))
        assert session.get(LoginAttempt, "user456") is not None


def test_deleted_account_data_is_not_visible_to_replacement_user(client):
    """삭제한 계정의 거래가 재사용된 사용자 ID를 통해 노출되지 않습니다."""
    signup(client)
    token = signin(client).json()["data"]["token"]
    response = client.post(
        "/api/v1/transaction",
        headers={"Authorization": "Bearer " + token},
        json={
            "date": "2026-09-16",
            "type": "expense",
            "source": "manual",
            "category": "food",
            "amount": 1000,
        },
    )
    assert response.status_code == 201
    assert delete_account(client, token).status_code == 200
    assert (
        signup(client, username="User999", display_name="새사용자").status_code == 200
    )
    new_token = signin(client, username="user999").json()["data"]["token"]
    result = client.get(
        "/api/v1/transaction", headers={"Authorization": "Bearer " + new_token}
    )
    assert result.json()["data"]["items"] == []


def test_signup_does_not_reuse_legacy_orphan_transaction_owner(client):
    """이전 버전에서 남긴 소유자 없는 거래 ID를 신규 계정에 재사용하지 않습니다."""
    from datetime import date, datetime, timezone

    from smartbudget_server.database import write_session
    from smartbudget_server.transaction.models import Transaction

    with write_session(client.app.state.engine) as session:
        session.add(
            Transaction(
                user_id=1,
                date=date(2026, 9, 16),
                type="expense",
                source="manual",
                category="food",
                amount=1000,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
    assert signup(client).status_code == 200
    token = signin(client).json()["data"]["token"]
    result = client.get(
        "/api/v1/transaction", headers={"Authorization": "Bearer " + token}
    )
    assert result.json()["data"]["items"] == []


def test_deleted_user_id_is_not_reused_during_an_inflight_request(client, monkeypatch):
    """삭제 직전 인증된 요청이 새 사용자의 거래를 읽지 못합니다."""
    from threading import Event, Thread

    from smartbudget_server.report import router as report_router

    signup(client)
    old_token = signin(client).json()["data"]["token"]
    entered, release = Event(), Event()
    original = report_router._current_user

    def wait_after_auth(request, credentials):
        """인증 직후 삭제와 재가입이 겹치는 상황을 재현합니다."""
        user = original(request, credentials)
        entered.set()
        assert release.wait(10)
        return user

    monkeypatch.setattr(report_router, "_current_user", wait_after_auth)
    monkeypatch.setattr(
        report_router, "generate_report", lambda settings, aggregate: "# test"
    )
    responses = []
    worker = Thread(
        target=lambda: responses.append(
            client.post(
                "/api/v1/report",
                headers={"Authorization": "Bearer " + old_token},
                json={"period_type": "daily", "period_start": "2026-09-16"},
            )
        )
    )
    worker.start()
    try:
        assert entered.wait(10)
        assert delete_account(client, old_token).status_code == 200
        assert (
            signup(client, username="User999", display_name="새사용자").status_code
            == 200
        )
        new_token = signin(client, username="user999").json()["data"]["token"]
        assert (
            client.post(
                "/api/v1/transaction",
                headers={"Authorization": "Bearer " + new_token},
                json={
                    "date": "2026-09-16",
                    "type": "expense",
                    "source": "manual",
                    "category": "food",
                    "amount": 4321,
                },
            ).status_code
            == 201
        )
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert responses[0].status_code in (400, 401)


def test_existing_user_id_is_remembered_before_deletion(tmp_path):
    """기존 DB의 계정을 첫 실행 직후 삭제해도 해당 ID는 재사용되지 않습니다."""
    from smartbudget_server.database import Base, build_engine, write_session
    from smartbudget_server.operations import UserIdCounter

    path = tmp_path / "legacy-user.sqlite3"
    engine = build_engine(path)
    Base.metadata.create_all(engine)
    with write_session(engine) as session:
        session.add(
            User(
                id=77,
                username="legacy",
                password_hash="unused",
                display_name="기존",
                token_version=10,
                is_active=True,
                created_at=0,
            )
        )
    engine.dispose()
    settings = Settings(_env_file=None, jwt_secret="x" * 43, database_path=path)
    with TestClient(create_app(settings)) as client:
        with write_session(client.app.state.engine) as session:
            assert session.get(UserIdCounter, 1).last_id == 77
            session.delete(session.get(User, 77))
        assert signup(client).status_code == 200
        with read_session(client.app.state.engine) as session:
            assert (
                session.scalar(select(User.id).where(User.username == "user123")) == 78
            )


def test_delete_account_authentication_and_input_contract(client):
    """DELETE는 Bearer JWT만 받고 query·본문은 요청 오류로 거부합니다."""
    signup(client)
    token = signin(client).json()["data"]["token"]
    for response in [
        client.delete(PREFIX + "/account"),
        client.delete(PREFIX + "/account", headers={"Authorization": "Bearer invalid"}),
    ]:
        assert response.status_code == 401
        assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
        assert response.headers["www-authenticate"] == "Bearer"
    for response in [
        client.delete(
            PREFIX + "/account",
            params={"token": token},
            headers={"Authorization": "Bearer " + token},
        ),
        client.request(
            "DELETE",
            PREFIX + "/account",
            headers={"Authorization": "Bearer " + token},
            json={},
        ),
    ]:
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


def test_account_lifecycle(client):
    """가입·부분 수정·갱신·비밀번호 폐기의 전체 흐름을 검증합니다."""
    result = signup(client, display_name="  홍 길동  ")
    assert result.status_code == 200
    assert result.json() == {
        "status": 200,
        "code": "SUCCESS",
        "message": "The request has been accepted and processed.",
        "data": None,
    }
    conflict = signup(client)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "USERNAME_CONFLICT"
    sign_in_data = signin(client).json()["data"]
    assert set(sign_in_data) == {"token", "expires_at", "token_type"}
    first = sign_in_data["token"]
    other = signin(client).json()["data"]["token"]
    assert account(client, first).json()["data"] == {
        "username": "user123",
        "display_name": "홍 길동",
        "profile_image": None,
    }
    renewed = client.post(PREFIX + "/refresh", json={"token": first})
    assert renewed.status_code == 200
    refresh_data = renewed.json()["data"]
    assert set(refresh_data) == {"token", "expires_at", "token_type"}
    new = refresh_data["token"]
    assert first != new
    claims = jwt.decode(
        new,
        client.app.state.settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        audience="smartbudget-server-api",
        issuer="smartbudget-server",
    )
    assert claims["exp"] - claims["iat"] == 432000
    assert account(client, first).status_code == 200
    renamed = patch_account(client, first, {"display_name": "새 이름"})
    assert renamed.status_code == 200
    assert renamed.json()["data"] == {
        "username": "user123",
        "display_name": "새 이름",
        "profile_image": None,
    }
    assert account(client, first).status_code == 200
    changed = patch_account(
        client,
        first,
        {"current_password": "Abcdef12", "password": "Changed12!"},
    )
    assert changed.status_code == 200
    assert changed.json()["data"] == {
        "username": "user123",
        "display_name": "새 이름",
        "profile_image": None,
    }
    for token in [first, other, new]:
        assert account(client, token).status_code == 401
        assert (
            client.post(PREFIX + "/refresh", json={"token": token}).status_code == 401
        )
    invalid_credentials = signin(client)
    assert invalid_credentials.status_code == 401
    assert invalid_credentials.json()["code"] == "INVALID_CREDENTIALS"
    assert signin(client, password="Changed12!").status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"display_name": None},
        {"password": None, "current_password": "Abcdef12"},
        {"password": "Changed12!", "current_password": None},
        {"password": "Changed12!"},
        {"current_password": "Abcdef12"},
        {"budget_limit": 1000},
    ],
)
def test_invalid_account_update_contract(client, payload):
    """빈 값·null·불완전 비밀번호 쌍·폐기 필드를 오류 코드 400으로 거부합니다."""
    signup(client)
    token = signin(client).json()["data"]["token"]
    result = patch_account(client, token, payload)
    assert result.status_code == 400
    assert result.json()["code"] == "INVALID_REQUEST"


def test_wrong_current_password_keeps_all_account_values(client):
    """현재 비밀번호가 틀리면 표시 이름과 비밀번호를 함께 유지합니다."""
    signup(client)
    token = signin(client).json()["data"]["token"]
    result = patch_account(
        client,
        token,
        {
            "current_password": "wrong123",
            "password": "Changed12!",
            "display_name": "다른이름",
        },
    )
    assert result.status_code == 401
    assert result.json()["code"] == "CURRENT_PASSWORD_INCORRECT"
    assert account(client, token).json()["data"] == {
        "username": "user123",
        "display_name": "홍길동",
        "profile_image": None,
    }
    assert signin(client).status_code == 200
    assert signin(client, password="Changed12!").status_code == 401


@pytest.mark.parametrize(
    "name", ["가", "가" * 11, "홍!", "홍😀", "   ", "\t홍길동", "홍\n길동"]
)
def test_invalid_display_name(client, name):
    """표시 이름의 길이·문자·제어문자 규칙을 지킵니다."""
    assert signup(client, display_name=name).status_code == 400


def test_user_isolation_and_input_contract(client):
    """다른 사용자 선택 입력과 토큰의 잘못된 전달 위치를 거부합니다."""
    signup(client)
    signup(client, username="User456", display_name="다른사용자")
    first = signin(client).json()["data"]["token"]
    second = signin(client, username="user456").json()["data"]["token"]
    assert account(client, second).json()["data"]["display_name"] == "다른사용자"
    assert patch_account(client, first, {"username": "user456"}).status_code == 400
    assert client.get(PREFIX + "/account", params={"token": first}).status_code == 400
    assert client.get(PREFIX + "/account").status_code == 401
    assert (
        client.patch(PREFIX + "/account", json={"display_name": "새 이름"}).status_code
        == 401
    )
    assert client.post(PREFIX + "/refresh", json={}).status_code == 400
    assert client.post(PREFIX + "/sign-up", json={}).status_code == 400
    assert client.patch(PREFIX + "/account", json={"token": first}).status_code == 400
    assert (
        client.patch(
            PREFIX + "/account",
            headers={"Authorization": "Bearer " + first},
            json={"token": first, "display_name": "새 이름"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            PREFIX + "/refresh",
            json={"token": first},
            headers={"Authorization": "Bearer " + first},
        ).status_code
        == 400
    )


def test_restart_keeps_user_and_revocation(tmp_path):
    """파일 DB 재시작 후에도 정보와 기존 JWT 폐기 상태를 유지합니다."""
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        database_path=tmp_path / "persistent.sqlite3",
    )
    with TestClient(create_app(settings)) as first:
        signup(first)
        token = signin(first).json()["data"]["token"]
        assert (
            patch_account(
                first,
                token,
                {"current_password": "Abcdef12", "password": "Changed12!"},
            ).status_code
            == 200
        )
    with TestClient(create_app(settings)) as second:
        assert account(second, token).status_code == 401
        new = signin(second, password="Changed12!").json()["data"]["token"]
        assert account(second, new).json()["data"] == {
            "username": "user123",
            "display_name": "홍길동",
            "profile_image": None,
        }


def test_invalid_token_does_not_hash_new_password(client, monkeypatch):
    """인증 실패한 수정 요청은 고비용 비밀번호 해시를 실행하지 않습니다."""
    from smartbudget_server.auth import security

    calls = []
    monkeypatch.setattr(security, "hash_password", lambda value: calls.append(value))
    response = client.patch(
        PREFIX + "/account",
        headers={"Authorization": "Bearer invalid"},
        json={"current_password": "Abcdef12", "password": "Changed12!"},
    )
    assert response.status_code == 401
    assert calls == []


def test_router_uses_each_apps_database_and_settings(tmp_path):
    """공유 인증 라우터가 서로 다른 앱의 DB·JWT 서명키를 혼용하지 않습니다."""
    first_settings = Settings(
        _env_file=None, jwt_secret="a" * 43, database_path=tmp_path / "first.sqlite3"
    )
    second_settings = Settings(
        _env_file=None, jwt_secret="b" * 43, database_path=tmp_path / "second.sqlite3"
    )
    with TestClient(create_app(first_settings)) as first:
        with TestClient(create_app(second_settings)) as second:
            assert signup(first, display_name="첫 사용자").status_code == 200
            assert signup(second, display_name="둘 사용자").status_code == 200
            first_token = signin(first).json()["data"]["token"]
            second_token = signin(second).json()["data"]["token"]
            for client, token, name in [
                (first, first_token, "첫 사용자"),
                (second, second_token, "둘 사용자"),
            ]:
                response = client.get(
                    PREFIX + "/account", headers={"Authorization": "Bearer " + token}
                )
                assert response.status_code == 200
                assert response.json()["data"]["display_name"] == name
            assert (
                first.get(
                    PREFIX + "/account",
                    headers={"Authorization": "Bearer " + second_token},
                ).status_code
                == 401
            )
            assert (
                second.get(
                    PREFIX + "/account",
                    headers={"Authorization": "Bearer " + first_token},
                ).status_code
                == 401
            )


def test_profile_image_is_center_cropped_and_private(client):
    """큰 이미지의 중앙 픽셀을 저장하며 조회와 수정에 현재 사용자만 적용합니다."""
    assert signup(client).status_code == 200
    token = signin(client).json()["data"]["token"]
    assert signup(client, username="User456").status_code == 200
    other = signin(client, username="user456").json()["data"]["token"]

    image = Image.new("RGB", (700, 520), "red")
    image.paste("blue", (100, 10, 600, 510))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    encoded = b64encode(buffer.getvalue()).decode("ascii")
    response = patch_account(client, token, {"profile_image": encoded})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["username"] == "user123"
    assert account(client, token).json()["data"] == data
    with Image.open(BytesIO(b64decode(data["profile_image"]))) as stored:
        assert stored.format == "PNG"
        assert stored.size == (500, 500)
        assert stored.getpixel((0, 0)) == (0, 0, 255, 255)
        assert stored.getpixel((499, 499)) == (0, 0, 255, 255)
    assert account(client, other).json()["data"]["profile_image"] is None
    assert (
        patch_account(client, token, {"display_name": "새 이름"}).json()["data"][
            "profile_image"
        ]
        == data["profile_image"]
    )
    assert (
        patch_account(client, token, {"profile_image": None}).json()["data"][
            "profile_image"
        ]
        is None
    )


def test_bad_profile_image_rejects_other_changes_and_signup(client):
    """잘못된 Base64 또는 이미지 데이터가 다른 계정 변경을 남기지 않습니다."""
    assert (
        client.post(
            PREFIX + "/sign-up",
            json={
                "username": "user123",
                "password": "Password1!",
                "display_name": "홍길동",
                "profile_image": None,
            },
        ).status_code
        == 400
    )
    assert signup(client).status_code == 200
    token = signin(client).json()["data"]["token"]
    for invalid in ("@@@", b64encode(b"not an image").decode("ascii"), ""):
        response = patch_account(
            client, token, {"profile_image": invalid, "display_name": "변경이름"}
        )
        assert response.status_code == 400
    assert account(client, token).json()["data"] == {
        "username": "user123",
        "display_name": "홍길동",
        "profile_image": None,
    }


def test_legacy_database_adds_profile_image_column_and_preserves_account(tmp_path):
    """이미지 컬럼이 없는 SQLite 파일을 이전하고 재기동 시 기존 계정을 유지합니다."""
    path = tmp_path / "old-profile.sqlite3"
    settings = Settings(_env_file=None, jwt_secret="x" * 43, database_path=path)
    with TestClient(create_app(settings)) as client:
        assert signup(client).status_code == 200
        token = signin(client).json()["data"]["token"]
    with connect(path) as connection:
        connection.execute("ALTER TABLE users DROP COLUMN profile_image")

    for _ in range(2):
        with TestClient(create_app(settings)) as client:
            assert account(client, token).json()["data"] == {
                "username": "user123",
                "display_name": "홍길동",
                "profile_image": None,
            }
