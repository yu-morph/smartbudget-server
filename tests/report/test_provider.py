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


def test_provider_sends_xml_template_and_report_input(monkeypatch):
    """모델 요청에 XML 템플릿과 거래 입력을 분리하고 부분 응답도 반환합니다."""
    seen = []
    partial_report = "<report><a_word_from_ai>결과</a_word_from_ai></report>"

    def handler(request):
        """외부 서비스 응답을 대체하고 실제 HTTP 본문을 검사합니다."""
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": partial_report}}]}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        azure_openai_endpoint="https://example.org",
        azure_openai_key="test-key",
        azure_openai_deployment="test-model",
    )
    report_input = {
        "period_start": "2026-09-16",
        "period_end": "2026-09-16",
        "transactions": [["2026-09-16", "expense", "food", 500]],
    }
    assert generate_report(settings, report_input) == partial_report
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


def test_provider_failures(monkeypatch):
    """제공자 오류와 시간 초과를 서로 다르게 전달합니다."""
    original = httpx.AsyncClient
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        azure_openai_endpoint="https://example.org",
        azure_openai_key="test-key",
        azure_openai_deployment="test-model",
    )

    def fail(request):
        """HTTP 오류 응답을 반환합니다."""
        return httpx.Response(500)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(fail), **kwargs),
    )
    with pytest.raises(ProviderFailureError):
        generate_report(settings, {})

    def timeout(request):
        """제공자 시간 초과를 발생시킵니다."""
        raise httpx.ReadTimeout("timeout", request=request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(timeout), **kwargs),
    )
    with pytest.raises(ProviderTimeoutError):
        generate_report(settings, {})
