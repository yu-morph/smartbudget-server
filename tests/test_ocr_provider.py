"""Azure 영수증 분석 결과의 원화 총액 변환을 검증합니다."""

import asyncio

import httpx
import pytest

from smartbudget_server.config import Settings
from smartbudget_server.proxy.provider import (
    OcrProviderError,
    _combine_documents,
    _result,
    recognize_receipt,
)


def test_only_interpretable_positive_integer_korean_won_is_returned():
    """원화가 확인된 양의 정수만 반환하고 외화와 소수는 제외합니다."""

    def document(total):
        """총액 필드를 가진 Azure 결과를 만듭니다."""
        return {"fields": {"Total": total}}

    assert (
        _result(document({"valueCurrency": {"amount": 1250, "currencyCode": "KRW"}}))[
            "amount"
        ]
        == 1250
    )
    assert (
        _result(document({"valueNumber": 1250, "content": "1,250원"}))["amount"] == 1250
    )
    assert (
        _result(document({"valueCurrency": {"amount": 10, "currencyCode": "USD"}}))[
            "amount"
        ]
        is None
    )
    assert _result(document({"valueNumber": 1250}))["amount"] is None
    assert (
        _result(document({"valueCurrency": {"amount": 1.5, "currencyCode": "KRW"}}))[
            "amount"
        ]
        is None
    )


def test_ocr_polls_same_host_and_rejects_untrusted_operation_url(monkeypatch):
    """Azure의 결과 URL이 다른 호스트이면 인증 키를 전송하지 않습니다."""
    seen = []

    def respond(request):
        """제출 응답에 외부 호스트의 결과 URL을 넣습니다."""
        seen.append(request.url.host)
        return httpx.Response(
            202, headers={"operation-location": "https://other.example/result"}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        azure_document_intelligence_endpoint="https://ocr.example",
        azure_document_intelligence_key="secret",
    )
    with pytest.raises(OcrProviderError):
        asyncio.run(recognize_receipt(settings, b"data", "image/png"))
    assert seen == ["ocr.example"]


@pytest.mark.parametrize(
    ("status_code", "body", "expected_amount"),
    [
        (
            200,
            {
                "status": "succeeded",
                "analyzeResult": {
                    "content": "영수증 1000원",
                    "documents": [
                        {
                            "fields": {
                                "Total": {"valueNumber": 1000, "content": "1000원"}
                            }
                        }
                    ],
                },
            },
            1000,
        ),
        (200, {"status": "succeeded", "analyzeResult": {"documents": []}}, None),
        (200, {"status": "failed", "error": {"code": "InvalidContent"}}, None),
        (429, {"error": {"code": "TooManyRequests"}}, None),
        (503, "Service unavailable", None),
    ],
)
def test_ocr_preserves_azure_result_even_when_processing_fails(
    monkeypatch, status_code, body, expected_amount
):
    """정규화 성공·실패와 Azure HTTP 오류에서 같은 원본 본문을 보존합니다."""

    def respond(request):
        """파일 제출과 Azure의 최종 결과 응답을 모의합니다."""
        if request.method == "POST":
            return httpx.Response(
                202, headers={"operation-location": "https://ocr.example/result"}
            )
        if isinstance(body, str):
            return httpx.Response(status_code, text=body)
        return httpx.Response(status_code, json=body)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    settings = Settings(
        _env_file=None,
        jwt_secret="x" * 43,
        azure_document_intelligence_endpoint="https://ocr.example",
        azure_document_intelligence_key="secret",
    )
    if expected_amount is None:
        with pytest.raises(OcrProviderError) as failure:
            asyncio.run(recognize_receipt(settings, b"data", "image/png"))
        assert failure.value.azure_raw_result == body
    else:
        result = asyncio.run(recognize_receipt(settings, b"data", "image/png"))
        assert result["amount"] == expected_amount
        assert result["azure_raw_result"] == body


def test_multiple_pages_can_form_one_receipt_but_multiple_receipts_fail():
    """여러 페이지의 단일 영수증은 합치고 별도 영수증이 감지되면 거부합니다."""
    first = {"fields": {"MerchantName": {"valueString": "가게"}}}
    middle = {
        "fields": {
            "Items": {
                "valueArray": [
                    {"valueObject": {"Description": {"valueString": "음식"}}}
                ]
            }
        }
    }
    last = {
        "fields": {"Total": {"valueCurrency": {"amount": 1000, "currencyCode": "KRW"}}}
    }
    result = _combine_documents([first, middle, last])
    assert result["store_name"] == "가게"
    assert result["items"] == ["음식"]
    assert result["amount"] == 1000
    assert _combine_documents([first, first, last])["store_name"] == "가게"
    with pytest.raises(OcrProviderError):
        _combine_documents(
            [first, {"fields": {"MerchantName": {"valueString": "다른 가게"}}}]
        )


def test_truncated_merchant_field_uses_its_ocr_line():
    """같은 상호가 일부만 추출돼도 원문 행이 일치하면 페이지를 결합합니다."""
    documents = [
        {
            "fields": {
                "MerchantName": {
                    "valueString": "TEST STORE",
                    "spans": [{"offset": 0, "length": 10}],
                }
            }
        },
        {
            "fields": {
                "MerchantName": {
                    "valueString": "TEST",
                    "spans": [{"offset": 20, "length": 4}],
                }
            }
        },
    ]
    pages = [
        {"lines": [{"content": "TEST STORE", "spans": [{"offset": 0, "length": 10}]}]},
        {"lines": [{"content": "TEST STORE", "spans": [{"offset": 20, "length": 10}]}]},
    ]
    assert _combine_documents(documents, pages)["store_name"] == "TEST STORE"


def test_multiple_complete_receipts_with_identical_fields_are_rejected():
    """상호와 총액이 각각 있는 두 페이지는 동일 값이어도 임의로 합치지 않습니다."""
    fields = {
        "MerchantName": {"valueString": "가상상호"},
        "TransactionDate": {"valueDate": "2026-09-16"},
        "Total": {"valueCurrency": {"amount": 1000, "currencyCode": "KRW"}},
    }
    with pytest.raises(OcrProviderError):
        _combine_documents([{"fields": fields}, {"fields": fields}])
