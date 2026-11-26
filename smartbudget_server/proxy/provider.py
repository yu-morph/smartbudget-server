"""Azure Document Intelligence 영수증 분석과 공개 결과 변환을 담당합니다."""

import asyncio
from datetime import date, time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from smartbudget_server.config import Settings


class OcrProviderError(Exception):
    """파일별 OCR 실패와 확인 가능한 Azure 응답을 라우터에 전달합니다."""

    def __init__(self, azure_raw_result: JsonValue = None) -> None:
        """Azure 본문을 공개용 실패 메시지와 별도로 보관합니다."""
        super().__init__()
        self.azure_raw_result = azure_raw_result


def _response_body(response: httpx.Response) -> JsonValue:
    """Azure 응답을 JSON 값으로 읽고 JSON이 아니면 원문 텍스트를 보존합니다."""
    try:
        return response.json()
    except ValueError:
        return response.text or None


def _value(fields: dict, name: str, kind: str):
    """Azure의 타입별 필드에서 인식된 값을 가져옵니다."""
    return (fields.get(name) or {}).get(kind)


def _merchant_name(field: dict, pages: list[dict]) -> str | None:
    """상호 필드가 OCR 행 앞부분만 포함하면 같은 행의 전체 문자를 사용합니다."""
    name = field.get("valueString")
    if not isinstance(name, str):
        return None
    spans = field.get("spans") or []
    if not spans:
        return name
    offset = spans[0].get("offset")
    for page in pages:
        for line in page.get("lines", []):
            text = line.get("content")
            for span in line.get("spans", []):
                if (
                    span.get("offset") == offset
                    and isinstance(text, str)
                    and text.startswith(name)
                    and len(text) <= 200
                ):
                    return text
    return name


def _result(document: dict, pages: list[dict] | None = None) -> dict:
    """영수증 문서 필드를 API의 선택적 인식 필드로 바꿉니다."""
    fields = document.get("fields", {})
    items = []
    for item in _value(fields, "Items", "valueArray") or []:
        description = _value(item.get("valueObject", {}), "Description", "valueString")
        if isinstance(description, str):
            items.append(description)
    amount = None
    total = fields.get("Total") or {}
    currency = total.get("valueCurrency") or {}
    currency_code = currency.get("currencyCode")
    content = total.get("content") or ""
    is_won = currency_code == "KRW" or (
        currency_code is None and any(marker in content for marker in ("₩", "￦", "원"))
    )
    if is_won:
        try:
            number = Decimal(str(currency.get("amount", total.get("valueNumber"))))
            if (
                number.is_finite()
                and number >= 1
                and number == number.to_integral_value()
            ):
                amount = int(number)
        except InvalidOperation, ValueError:
            pass
    merchant = _merchant_name(fields.get("MerchantName") or {}, pages or [])
    date_value = _value(fields, "TransactionDate", "valueDate")
    time_value = _value(fields, "TransactionTime", "valueTime")
    try:
        date_value = date.fromisoformat(date_value).isoformat() if date_value else None
    except TypeError, ValueError:
        date_value = None
    try:
        parsed_time = time.fromisoformat(time_value) if time_value else None
        time_value = (
            parsed_time.isoformat(timespec="seconds")
            if parsed_time and parsed_time.tzinfo is None
            else None
        )
    except TypeError, ValueError:
        time_value = None
    return {
        "store_name": merchant if isinstance(merchant, str) else None,
        "items": items,
        "date": date_value,
        "time": time_value,
        "amount": amount,
    }


def _combine_documents(documents: list[dict], pages: list[dict] | None = None) -> dict:
    """페이지별 부분 인식을 합치고 서로 모순되는 핵심 필드는 거부합니다."""
    if not documents:
        raise OcrProviderError()
    complete_receipts = sum(
        bool(
            _merchant_name(
                (document.get("fields") or {}).get("MerchantName") or {}, pages or []
            )
        )
        and bool((document.get("fields") or {}).get("Total"))
        for document in documents
    )
    if complete_receipts > 1:
        raise OcrProviderError()
    combined = {
        "store_name": None,
        "items": [],
        "date": None,
        "time": None,
        "amount": None,
    }
    for document in documents:
        part = _result(document, pages)
        combined["items"].extend(part["items"])
        for key in ("store_name", "date", "time", "amount"):
            if (
                combined[key] is not None
                and part[key] is not None
                and combined[key] != part[key]
            ):
                raise OcrProviderError()
            if combined[key] is None and part[key] is not None:
                combined[key] = part[key]
    if not any(combined.values()):
        raise OcrProviderError()
    return combined


async def _analyze(settings: Settings, data: bytes, content_type: str) -> dict:
    """단일 파일을 제출하고 같은 Azure 호스트의 결과만 폴링합니다."""
    endpoint = settings.azure_document_intelligence_endpoint
    key = settings.azure_document_intelligence_key
    if not endpoint or not key:
        raise OcrProviderError()
    url = (
        endpoint.rstrip("/")
        + "/documentintelligence/documentModels/prebuilt-receipt:analyze?api-version=2024-11-30"
    )
    headers = {"Ocp-Apim-Subscription-Key": key.get_secret_value()}
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        response = await client.post(
            url, headers={**headers, "Content-Type": content_type}, content=data
        )
        response.raise_for_status()
        operation = response.headers.get("operation-location", "")
        target = urlsplit(operation)
        origin = urlsplit(endpoint)
        if target.scheme != "https" or target.netloc.lower() != origin.netloc.lower():
            raise OcrProviderError()
        while True:
            await asyncio.sleep(1)
            result = await client.get(operation, headers=headers)
            result.raise_for_status()
            body = _response_body(result)
            if not isinstance(body, dict):
                raise OcrProviderError(body)
            if body.get("status") == "succeeded":
                try:
                    analysis = body.get("analyzeResult", {})
                    fields = _combine_documents(
                        analysis.get("documents", []), analysis.get("pages", [])
                    )
                except (
                    OcrProviderError,
                    AttributeError,
                    ValueError,
                    TypeError,
                    KeyError,
                ) as exc:
                    raise OcrProviderError(body) from exc
                return {**fields, "azure_raw_result": body}
            if body.get("status") not in ("running", "notStarted"):
                raise OcrProviderError(body)


async def recognize_receipt(settings: Settings, data: bytes, content_type: str) -> dict:
    """단일 외부 분석을 재시도 없이 최대 60초 동안 실행합니다."""
    try:
        return await asyncio.wait_for(
            _analyze(settings, data, content_type), timeout=60
        )
    except httpx.HTTPStatusError as exc:
        raise OcrProviderError(_response_body(exc.response)) from exc
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise OcrProviderError() from exc
