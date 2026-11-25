"""소비 분석 템플릿과 압축된 입력을 Azure OpenAI에 전송합니다."""

import asyncio
import json

import httpx

from smartbudget_server.config import Settings
from smartbudget_server.report.prompt import REPORT_PROMPT, serialize_report_input

_HTTP_FAILURE_MESSAGES = {
    400: "The AI service rejected the report generation request (HTTP 400).",
    401: "The AI service rejected the server credentials (HTTP 401).",
    403: "The AI service denied access to the server (HTTP 403).",
    404: "The AI service endpoint or deployment was not found (HTTP 404).",
    413: "The report generation request was too large for the AI service (HTTP 413).",
    422: "The AI service could not process the report generation request (HTTP 422).",
    429: "The AI service rate limit was reached (HTTP 429). Try again later.",
}


class ProviderError(Exception):
    """소비 분석 라우터가 실패 응답에 넣을 진단 데이터를 보관합니다."""

    def __init__(
        self, message: str = "", *, provider_error_body: str | None = None
    ) -> None:
        """안내 메시지와 API 키를 가린 Azure 오류 본문을 분리합니다."""
        super().__init__(message)
        self.data: dict[str, str] = {}
        if provider_error_body is not None:
            self.data["provider_error_body"] = provider_error_body


class ProviderFailureError(ProviderError):
    """소비 분석 API의 502 응답으로 매핑할 제공자 실패입니다."""


class ProviderTimeoutError(ProviderError):
    """외부 분석 요청이 전체 시간 제한을 초과했음을 나타냅니다."""


async def _request_report(
    settings: Settings, api_key: str, messages: list[dict]
) -> str:
    """준비된 시스템·사용자 메시지를 Azure에 보내고 리포트 본문을 읽습니다."""
    url = settings.azure_openai_endpoint.rstrip("/") + "/openai/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            response = await client.post(
                url,
                headers={"api-key": api_key},
                json={"model": settings.azure_openai_deployment, "messages": messages},
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ProviderTimeoutError() from exc
    except httpx.HTTPStatusError as exc:
        error_response = exc.response
        status = error_response.status_code
        message = _HTTP_FAILURE_MESSAGES.get(
            status, f"The AI service returned an unexpected HTTP status ({status})."
        )
        error_body = error_response.text if error_response.is_error else ""
        raise ProviderFailureError(
            message,
            provider_error_body=error_body.replace(api_key, "[REDACTED]") or None,
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderFailureError(
            "Could not communicate with the AI service."
        ) from exc

    try:
        text = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderFailureError(
            "The AI service returned an invalid report response."
        ) from exc
    if text is None or (isinstance(text, str) and not text.strip()):
        raise ProviderFailureError("The AI service returned an empty report.")
    if not isinstance(text, str):
        raise ProviderFailureError(
            "The AI service returned an invalid report response."
        )
    return text


def generate_report(settings: Settings, report_input: dict) -> str:
    """단일 Azure 요청을 60초로 제한하고 실패한 요청의 메시지를 보관합니다."""
    api_key = (
        settings.azure_openai_key.get_secret_value()
        if settings.azure_openai_key is not None
        else ""
    )
    if not all(
        (settings.azure_openai_endpoint, api_key, settings.azure_openai_deployment)
    ):
        raise ProviderFailureError(
            "The server configuration for the AI service is incomplete."
        )
    messages = [
        {"role": "system", "content": REPORT_PROMPT},
        {"role": "user", "content": serialize_report_input(report_input)},
    ]

    async def limited() -> str:
        """전체 호출 시간 초과를 API에 전달할 제공자 예외로 바꿉니다."""
        try:
            return await asyncio.wait_for(
                _request_report(settings, api_key, messages), timeout=60
            )
        except TimeoutError as exc:
            raise ProviderTimeoutError() from exc

    try:
        return asyncio.run(limited())
    except httpx.InvalidURL as exc:
        raise ProviderFailureError(
            "The server configuration for the AI service endpoint is invalid."
        ) from exc
    except ProviderError as exc:
        exc.data["request_prompt_fulltext"] = json.dumps(messages, ensure_ascii=False)
        raise
