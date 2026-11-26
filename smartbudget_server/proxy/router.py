"""OpenAPI 영수증 OCR 프록시 엔드포인트의 구현 대기 라우트를 등록합니다."""

import asyncio
import hashlib
from io import BytesIO
from tempfile import SpooledTemporaryFile
from typing import Annotated

from fastapi import APIRouter, Header, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from PIL import Image
from pillow_heif import register_heif_opener
from pypdf import PdfReader
from starlette.datastructures import UploadFile

from smartbudget_server.auth import service as auth_service
from smartbudget_server.auth.router import bearer, bearer_token
from smartbudget_server.http import INVALID_REQUEST, documented_response, respond
from smartbudget_server.operations import begin_ocr, finish_ocr
from smartbudget_server.proxy.provider import OcrProviderError, recognize_receipt
from smartbudget_server.proxy.schemas import (
    AuthenticationRequiredEnvelope,
    IdempotencyConflictEnvelope,
    InternalErrorEnvelope,
    InvalidRequestEnvelope,
    OcrResult,
    SuccessEnvelope,
)

router = APIRouter(prefix="/api/v1/proxy", tags=["외부 API 프록시"])
register_heif_opener()


def _prepare_file(data: bytes) -> tuple[bytes, str]:
    """파일 내용을 확인하고 HEIC는 Azure에 전달할 PNG로 변환합니다."""
    if data.startswith(b"%PDF-"):
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted or not len(reader.pages):
            raise ValueError("Invalid PDF")
        return data, "application/pdf"
    with Image.open(BytesIO(data)) as image:
        format_name = image.format
        image.verify()
    with Image.open(BytesIO(data)) as image:
        image.load()
    if format_name == "JPEG":
        return data, "image/jpeg"
    if format_name == "PNG":
        return data, "image/png"
    if format_name == "HEIF":
        with Image.open(BytesIO(data)) as image:
            converted = BytesIO()
            image.convert("RGB").save(converted, format="PNG")
            return converted.getvalue(), "image/png"
    raise ValueError("Unsupported image")


OCR_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "description": (
            "multipart/form-data의 files 필드에 JPEG, PNG, HEIC 또는 PDF "
            "파일 1~10개를 첨부합니다. 파일당 최대 크기는 10,000,000바이트입니다."
        ),
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["files"],
                    "properties": {
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 10,
                            "items": {"type": "string", "format": "binary"},
                        }
                    },
                    "additionalProperties": False,
                }
            }
        },
    }
}


@router.post(
    "/ocr",
    response_model=SuccessEnvelope,
    responses={
        400: documented_response(
            InvalidRequestEnvelope, "업로드 요청이 잘못되었습니다."
        ),
        401: documented_response(
            AuthenticationRequiredEnvelope,
            "Bearer 인증이 필요합니다.",
            authentication=True,
        ),
        409: documented_response(
            IdempotencyConflictEnvelope, "중복 방지 키가 충돌했습니다."
        ),
        500: documented_response(
            InternalErrorEnvelope, "OCR 결과를 구성하지 못했습니다."
        ),
    },
    operation_id="post_api_v1_proxy_ocr",
    summary="OCR API 프록시",
    openapi_extra=OCR_REQUEST_BODY,
)
async def proxy_ocr(
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        ),
    ] = None,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """파일을 검증한 뒤 외부 OCR의 파일별 결과를 입력 순서대로 반환합니다."""
    user = auth_service.authenticate(
        request.app.state.engine, request.app.state.settings, bearer_token(credentials)
    )
    if request.query_params or not request.headers.get(
        "content-type", ""
    ).lower().startswith("multipart/form-data"):
        return respond(400, code="INVALID_REQUEST", message=INVALID_REQUEST)
    received = 0
    original_receive = request.receive

    async def bounded_receive():
        """파일 본문을 파싱하기 전에도 전체 업로드 바이트를 제한합니다."""
        nonlocal received
        message = await original_receive()
        received += len(message.get("body", b""))
        if received > 101_000_000:
            raise ValueError("Multipart request is too large")
        return message

    request._receive = bounded_receive
    files = []
    try:
        async with request.form(max_files=10, max_fields=0) as form:
            entries = form.multi_items()
            if not 1 <= len(entries) <= 10 or any(
                name != "files" or not isinstance(item, UploadFile) or not item.filename
                for name, item in entries
            ):
                raise ValueError("Invalid files")
            digest = hashlib.sha256()
            for _, upload in entries:
                original = await upload.read(10_000_001)
                if not original or len(original) > 10_000_000:
                    raise ValueError("Invalid file size")
                async with request.app.state.ocr_prepare_semaphore:
                    converted, content_type = await asyncio.to_thread(
                        _prepare_file, original
                    )
                name_bytes = upload.filename.encode("utf-8")
                digest.update(len(name_bytes).to_bytes(4, "big"))
                digest.update(name_bytes)
                digest.update(len(original).to_bytes(8, "big"))
                digest.update(original)
                temporary = SpooledTemporaryFile(max_size=1_000_000, mode="w+b")
                files.append((upload.filename, temporary, content_type))
                temporary.write(converted)
                temporary.seek(0)
                del original, converted
    except BaseException as exc:
        for _, temporary, _ in files:
            temporary.close()
        if isinstance(exc, asyncio.CancelledError):
            raise
        if not isinstance(exc, Exception):
            raise
        return respond(400, code="INVALID_REQUEST", message=INVALID_REQUEST)
    try:
        return await _dispatch_ocr(
            request, user.id, idempotency_key, digest.hexdigest(), files
        )
    finally:
        for _, temporary, _ in files:
            temporary.close()


async def _dispatch_ocr(
    request: Request, user_id: int, idempotency_key: str | None, fingerprint: str, files
):
    """검증된 파일을 중복 방지 규칙에 따라 제공자에 전달합니다."""
    if idempotency_key:
        state, cached = begin_ocr(
            request.app.state.engine, user_id, idempotency_key, fingerprint
        )
        if state == "replay":
            for item in cached:
                item.setdefault("azure_raw_result", None)
            return respond(200, cached)
        if state != "started":
            message = (
                "The idempotency key was already used with a different request."
                if state == "IDEMPOTENCY_KEY_CONFLICT"
                else "A request with this idempotency key is still being processed."
            )
            return respond(409, code=state, message=message)
    success = False
    semaphore = request.app.state.ocr_semaphore

    async def process(index: int, name: str, temporary, content_type: str):
        """한 파일의 실패를 다른 파일과 격리하고 결과 스키마를 검증합니다."""
        async with semaphore:
            azure_raw_result = None
            try:
                data = await asyncio.to_thread(temporary.read)
                values = await recognize_receipt(
                    request.app.state.settings, data, content_type
                )
                azure_raw_result = values.pop("azure_raw_result", None)
                return OcrResult(
                    file_name=name,
                    file_index=index,
                    status="success",
                    error_message=None,
                    azure_raw_result=azure_raw_result,
                    **values,
                ).model_dump(mode="json")
            except OcrProviderError as exc:
                azure_raw_result = exc.azure_raw_result
            except ValueError:
                pass
            return OcrResult(
                file_name=name,
                file_index=index,
                status="failed",
                error_message="Receipt recognition failed.",
                store_name=None,
                items=[],
                date=None,
                time=None,
                amount=None,
                azure_raw_result=azure_raw_result,
            ).model_dump(mode="json")

    try:
        result = await asyncio.gather(
            *(process(index, *item) for index, item in enumerate(files))
        )
        if idempotency_key:
            finish_ocr(request.app.state.engine, user_id, idempotency_key, result)
        success = True
        return respond(200, result)
    finally:
        if idempotency_key and not success:
            finish_ocr(request.app.state.engine, user_id, idempotency_key, None)
