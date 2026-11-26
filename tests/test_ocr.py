"""영수증 OCR 프록시의 업로드와 파일별 결과 계약을 검증합니다."""

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from smartbudget_server.config import Settings
from smartbudget_server.main import create_app
from tests.auth.helpers import signin, signup


@pytest.fixture
def client(tmp_path):
    """독립 데이터베이스를 사용하는 OCR 테스트 앱을 생성합니다."""
    settings = Settings(
        _env_file=None, jwt_secret="x" * 43, database_path=tmp_path / "ocr.sqlite3"
    )
    with TestClient(create_app(settings)) as result:
        yield result


def auth_headers(client):
    """테스트 사용자의 Bearer 인증 헤더를 만듭니다."""
    assert signup(client, username="User123", display_name="홍길동").status_code == 200
    token = signin(client, username="user123").json()["data"]["token"]
    return {"Authorization": "Bearer " + token}


def png():
    """실제로 해석 가능한 테스트용 PNG를 생성합니다."""
    data = BytesIO()
    Image.new("RGB", (50, 50), "white").save(data, format="PNG")
    return data.getvalue()


def test_ocr_authentication_and_invalid_file(client):
    """인증이 필요하며 잘못된 파일은 OCR 호출 전에 거부합니다."""
    assert (
        client.post(
            "/api/v1/proxy/ocr", files=[("files", ("test.png", png(), "image/png"))]
        ).status_code
        == 401
    )
    result = client.post(
        "/api/v1/proxy/ocr",
        headers=auth_headers(client),
        files=[("files", ("test.png", b"bad", "image/png"))],
    )
    assert result.status_code == 400


def test_ocr_results_keep_input_order(client, monkeypatch):
    """파일별 성공·실패 결과가 입력 순서를 보존합니다."""

    async def recognize(settings, data, content_type):
        """외부 OCR을 대체하는 성공 결과를 반환합니다."""
        return {
            "store_name": "가게",
            "items": [],
            "date": None,
            "time": None,
            "amount": 1000,
        }

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", recognize)
    result = client.post(
        "/api/v1/proxy/ocr",
        headers=auth_headers(client),
        files=[
            ("files", ("a.png", png(), "image/png")),
            ("files", ("b.png", png(), "image/png")),
        ],
    )
    assert result.status_code == 200
    assert [row["file_name"] for row in result.json()["data"]] == ["a.png", "b.png"]
    assert [row["file_index"] for row in result.json()["data"]] == [0, 1]


def test_partial_ocr_failure_preserves_success(client, monkeypatch):
    """부분 실패에도 순서대로 성공과 실패의 Azure 원문을 반환합니다."""
    from smartbudget_server.proxy.provider import OcrProviderError

    second = BytesIO()
    Image.new("RGB", (50, 50), "black").save(second, format="PNG")
    failed_bytes = second.getvalue()
    failed_raw = {"status": "failed", "error": {"code": "InvalidContent"}}

    async def recognize(settings, data, content_type):
        """검은 이미지만 외부 처리 실패를 재현합니다."""
        if data == failed_bytes:
            raise OcrProviderError(failed_raw)
        return {
            "store_name": "가게",
            "items": ["상품"],
            "date": None,
            "time": None,
            "amount": 1000,
            "azure_raw_result": {
                "status": "succeeded",
                "analyzeResult": {"content": "상품"},
            },
        }

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", recognize)
    result = client.post(
        "/api/v1/proxy/ocr",
        headers=auth_headers(client),
        files=[
            ("files", ("a.png", png(), "image/png")),
            ("files", ("b.png", failed_bytes, "image/png")),
        ],
    )
    assert result.status_code == 200
    first, second = result.json()["data"]
    assert (first["file_index"], first["status"], first["amount"]) == (
        0,
        "success",
        1000,
    )
    assert (
        second["file_index"],
        second["status"],
        second["amount"],
        second["items"],
    ) == (1, "failed", None, [])
    assert second["error_message"]
    assert first["azure_raw_result"]["analyzeResult"]["content"] == "상품"
    assert second["azure_raw_result"] == failed_raw


def test_ocr_idempotency_replays_and_detects_conflicts(client, monkeypatch):
    """동일 키 재시도는 결과를 재사용하고 내용이 달라지면 거부합니다."""
    calls = []
    raw_result = {"status": "succeeded", "analyzeResult": {"content": "가상 영수증"}}

    async def recognize(settings, data, content_type):
        """제공자 호출 횟수를 기록합니다."""
        calls.append(data)
        return {
            "store_name": None,
            "items": [],
            "date": None,
            "time": None,
            "amount": None,
            "azure_raw_result": raw_result,
        }

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", recognize)
    headers = {**auth_headers(client), "Idempotency-Key": "ocr-key"}
    first = client.post(
        "/api/v1/proxy/ocr",
        headers=headers,
        files=[("files", ("a.png", png(), "image/png"))],
    )
    second = client.post(
        "/api/v1/proxy/ocr",
        headers=headers,
        files=[("files", ("a.png", png(), "image/png"))],
    )
    conflict = client.post(
        "/api/v1/proxy/ocr",
        headers=headers,
        files=[("files", ("b.png", png(), "image/png"))],
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.json()["data"][0]["azure_raw_result"] == raw_result
    assert len(calls) == 1
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_interrupted_key_can_be_recovered_after_restart(tmp_path, monkeypatch):
    """서버 중단 뒤 미완료 키를 해제하면 동일 요청을 다시 처리할 수 있습니다."""
    import hashlib

    from smartbudget_server.database import build_engine
    from smartbudget_server.operations import begin_ocr
    from smartbudget_server.recovery import recover_idempotency

    async def recognize(settings, data, content_type):
        """실제 외부 요청 없이 성공 결과를 반환합니다."""
        return {
            "store_name": "가게",
            "items": [],
            "date": None,
            "time": None,
            "amount": 1000,
        }

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", recognize)
    path = tmp_path / "restart.sqlite3"
    settings = Settings(_env_file=None, jwt_secret="x" * 43, database_path=path)
    data = png()
    name = b"receipt.png"
    digest = hashlib.sha256(
        len(name).to_bytes(4, "big") + name + len(data).to_bytes(8, "big") + data
    ).hexdigest()
    with TestClient(create_app(settings)) as client:
        headers = {**auth_headers(client), "Idempotency-Key": "interrupted"}
        assert (
            begin_ocr(client.app.state.engine, 1, "interrupted", digest)[0] == "started"
        )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/proxy/ocr",
            headers=headers,
            files=[("files", ("receipt.png", data, "image/png"))],
        )
        assert response.status_code == 409
        assert response.json()["code"] == "IDEMPOTENCY_REQUEST_IN_PROGRESS"
    engine = build_engine(path)
    recover_idempotency(engine, 1, "ocr", "interrupted")
    engine.dispose()
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/proxy/ocr",
            headers=headers,
            files=[("files", ("receipt.png", data, "image/png"))],
        )
        assert response.status_code == 200
        assert response.json()["data"][0]["status"] == "success"


def test_heic_is_converted_for_the_ocr_provider(client, monkeypatch):
    """HEIC 업로드를 유효한 PNG로 변환해 제공자에 전달합니다."""
    received = []

    async def recognize(settings, data, content_type):
        """제공자에 전달된 변환 결과를 확인합니다."""
        received.append((data, content_type))
        return {
            "store_name": "가게",
            "items": [],
            "date": None,
            "time": None,
            "amount": 1000,
        }

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", recognize)
    stream = BytesIO()
    Image.new("RGB", (50, 50), "white").save(stream, format="HEIF")
    response = client.post(
        "/api/v1/proxy/ocr",
        headers=auth_headers(client),
        files=[("files", ("test.heic", stream.getvalue(), "image/heic"))],
    )
    assert response.status_code == 200
    assert received[0][1] == "image/png"
    assert received[0][0].startswith(b"\x89PNG\r\n\x1a\n")


def test_ocr_internal_error_uses_documented_envelope(client, monkeypatch):
    """예상하지 못한 내부 오류는 상세 정보를 노출하지 않는 500 응답을 반환합니다."""

    async def fail(settings, data, content_type):
        """제공자 오류 처리 범위 밖의 내부 예외를 재현합니다."""
        raise RuntimeError("private detail")

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", fail)
    client._transport.raise_server_exceptions = False
    response = client.post(
        "/api/v1/proxy/ocr",
        headers=auth_headers(client),
        files=[("files", ("test.png", png(), "image/png"))],
    )
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "private detail" not in response.text


def test_existing_legacy_idempotency_table_does_not_break_ocr(tmp_path, monkeypatch):
    """기존 DB의 다른 중복 방지 스키마와 새 OCR 기록이 충돌하지 않습니다."""
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE idempotency_records (id INTEGER PRIMARY KEY, user_id INTEGER, operation TEXT, key TEXT, request_hash TEXT, state TEXT, response_status INTEGER, response_body JSON, created_at INTEGER, expires_at INTEGER)"
        )

    async def recognize(settings, data, content_type):
        """외부 OCR을 호출하지 않고 결과를 반환합니다."""
        return {
            "store_name": "가게",
            "items": [],
            "date": None,
            "time": None,
            "amount": 1000,
        }

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", recognize)
    settings = Settings(_env_file=None, jwt_secret="x" * 43, database_path=path)
    with TestClient(create_app(settings)) as client:
        headers = {**auth_headers(client), "Idempotency-Key": "legacy-check"}
        result = client.post(
            "/api/v1/proxy/ocr",
            headers=headers,
            files=[("files", ("test.png", png(), "image/png"))],
        )
        assert result.status_code == 200
        assert result.json()["data"][0]["status"] == "success"


def test_invalid_uploads_do_not_call_provider(client, monkeypatch):
    """개수·크기·정의되지 않은 필드 오류는 전체 요청을 호출 전에 거부합니다."""
    calls = []

    async def recognize(settings, data, content_type):
        """호출 여부를 기록합니다."""
        calls.append(True)
        return {
            "store_name": None,
            "items": [],
            "date": None,
            "time": None,
            "amount": None,
        }

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", recognize)
    headers = auth_headers(client)
    cases = [
        [("files", ("x.png", png(), "image/png")) for _ in range(11)],
        [("files", ("x.png", b"x" * 10_000_001, "image/png"))],
        [
            ("files", ("x.png", png(), "image/png")),
            ("unexpected", ("x.png", png(), "image/png")),
        ],
    ]
    for files in cases:
        assert (
            client.post("/api/v1/proxy/ocr", headers=headers, files=files).status_code
            == 400
        )
    assert not calls


def test_truncated_jpeg_is_rejected_before_call(client, monkeypatch):
    """파일 헤더만 정상인 잘린 JPEG는 OCR 제공자에 보내지 않습니다."""
    calls = []

    async def recognize(settings, data, content_type):
        """호출 여부를 기록합니다."""
        calls.append(True)
        return {
            "store_name": None,
            "items": [],
            "date": None,
            "time": None,
            "amount": None,
        }

    monkeypatch.setattr("smartbudget_server.proxy.router.recognize_receipt", recognize)
    stream = BytesIO()
    Image.new("RGB", (512, 512), "red").save(stream, format="JPEG")
    result = client.post(
        "/api/v1/proxy/ocr",
        headers=auth_headers(client),
        files=[("files", ("broken.jpg", stream.getvalue()[:-100], "image/jpeg"))],
    )
    assert result.status_code == 400
    assert calls == []
