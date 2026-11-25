"""Azure 모델 호출의 전송 내용과 실패 처리를 검증합니다."""

import json

import httpx
import pytest

from smartbudget_server.config import Settings
from smartbudget_server.report.provider import (
    ProviderFailureError,
    ProviderTimeoutError,
    generate_report,
)


@pytest.fixture
def provider_settings():
    """외부 네트워크 대신 테스트 전송에 사용할 분석 서비스 설정입니다."""
    return Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        azure_openai_endpoint="https://example.org",
        azure_openai_key="test-key",
        azure_openai_deployment="test-model",
    )


def _mock_transport(monkeypatch, handler):
    """분석 제공자의 HTTP 요청을 지정한 테스트 핸들러로 전달합니다."""
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_provider_sends_xml_template_and_report_input(monkeypatch, provider_settings):
    """모델 요청에 XML 템플릿과 거래 입력을 분리하고 부분 응답도 반환합니다."""
    seen = []
    partial_report = "<report><a_word_from_ai>결과</a_word_from_ai></report>"

    def handler(request):
        """외부 서비스 응답을 대체하고 실제 HTTP 본문을 검사합니다."""
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": partial_report}}]}
        )

    _mock_transport(monkeypatch, handler)
    report_input = {
        "period_start": "2026-09-16",
        "period_end": "2026-09-16",
        "transactions": [["2026-09-16", "expense", "food", 500]],
    }
    assert generate_report(provider_settings, report_input) == partial_report
    system, user = seen[0]["messages"]
    assert system["role"] == "system"
    for tag in (
        "report",
        "a_word_from_ai",
        "top_spending_categories",
        "spending_health",
        "spending_by_day_of_the_week",
        "tips_for_saving",
        "tips_to_keep_it_up",
        "tips_for_your_interests",
        "news_driven_insights",
    ):
        assert f"<{tag}>" in system["content"]
    assert user["role"] == "user"
    assert json.loads(user["content"]) == report_input
    assert seen[0]["model"] == "test-model"


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (400, "rejected the report generation request"),
        (401, "rejected the server credentials"),
        (403, "denied access"),
        (404, "endpoint or deployment was not found"),
        (413, "request was too large"),
        (422, "could not process"),
        (429, "rate limit was reached"),
        (500, "unexpected HTTP status"),
    ],
)
def test_provider_http_failure_message(monkeypatch, provider_settings, status, detail):
    """HTTP 실패는 상태별 메시지와 키를 가린 오류 원문을 따로 보관합니다."""
    upstream_body = '{"error": {"message": "private details test-key"}}'
    seen = []

    def fail(request):
        """서버 API 키가 포함된 Azure HTTP 오류를 모의합니다."""
        seen.append(json.loads(request.content)["messages"])
        return httpx.Response(status, text=upstream_body)

    _mock_transport(monkeypatch, fail)
    with pytest.raises(ProviderFailureError, match=detail) as error:
        generate_report(provider_settings, {"interest": "교통비\n절약"})
    assert str(status) in str(error.value)
    assert "private details" not in str(error.value)
    assert error.value.data["provider_error_body"] == upstream_body.replace(
        "test-key", "[REDACTED]"
    )
    prompt = error.value.data["request_prompt_fulltext"]
    assert json.loads(prompt) == seen[0]
    assert "test-key" not in prompt


@pytest.mark.parametrize("body", ["<html>Unavailable</html>", ""])
def test_provider_preserves_non_json_http_error_body(
    monkeypatch, provider_settings, body
):
    """Azure 오류 본문은 텍스트로 보관하며 빈 본문은 생략합니다."""
    _mock_transport(monkeypatch, lambda request: httpx.Response(503, text=body))
    with pytest.raises(ProviderFailureError) as error:
        generate_report(provider_settings, {})
    assert error.value.data.get("provider_error_body") == (body or None)


@pytest.mark.parametrize(
    ("setting", "value", "detail"),
    [
        ("azure_openai_deployment", None, "configuration.*incomplete"),
        ("azure_openai_endpoint", "https://example.org:invalid", "endpoint is invalid"),
    ],
)
def test_provider_configuration_failure_message(
    monkeypatch, provider_settings, setting, value, detail
):
    """설정 누락과 잘못된 주소를 구분하고 외부로 요청하지 않습니다."""
    setattr(provider_settings, setting, value)

    _mock_transport(monkeypatch, lambda request: pytest.fail("Unexpected AI request"))
    with pytest.raises(ProviderFailureError, match=detail) as error:
        generate_report(provider_settings, {})
    assert error.value.data == {}


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"not JSON", "invalid report response"),
        (b'{"choices":[]}', "invalid report response"),
        (b'{"choices":[{"message":{"content":123}}]}', "invalid report response"),
        (b'{"choices":[{"message":{"content":null}}]}', "empty report"),
        (b'{"choices":[{"message":{"content":" "}}]}', "empty report"),
    ],
)
def test_provider_response_failure_message(
    monkeypatch, provider_settings, body, detail
):
    """정상 HTTP 응답에서 잘못된 형식과 빈 리포트를 구분합니다."""
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(ProviderFailureError, match=detail):
        generate_report(provider_settings, {})


@pytest.mark.parametrize(
    ("exception", "expected", "message"),
    [
        (
            httpx.ConnectError,
            ProviderFailureError,
            "Could not communicate with the AI service.",
        ),
        (httpx.ReadTimeout, ProviderTimeoutError, ""),
    ],
)
def test_provider_transport_failures(
    monkeypatch, provider_settings, exception, expected, message
):
    """연결 실패와 시간 초과를 라우터에서 다른 상태로 매핑할 수 있게 합니다."""

    def fail(request):
        """민감한 진단 정보가 포함된 전송 예외를 모의합니다."""
        raise exception("private details", request=request)

    _mock_transport(monkeypatch, fail)
    with pytest.raises(expected) as error:
        generate_report(provider_settings, {})
    assert str(error.value) == message
    assert len(json.loads(error.value.data["request_prompt_fulltext"])) == 2
