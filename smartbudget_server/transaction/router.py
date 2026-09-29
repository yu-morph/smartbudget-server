"""OpenAPI 거래 엔드포인트의 구현 대기 라우트를 등록합니다."""

from datetime import date, datetime, time, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, Path, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select

from smartbudget_server.auth import service as auth_service
from smartbudget_server.auth.router import bearer, bearer_token
from smartbudget_server.database import read_session, write_session
from smartbudget_server.http import documented_response, respond
from smartbudget_server.report.models import Report
from smartbudget_server.transaction.models import Transaction
from smartbudget_server.transaction.schemas import (
    AuthenticationRequiredEnvelope,
    CreatedEnvelope,
    IdempotencyConflictEnvelope,
    InvalidRequestEnvelope,
    ResourceNotFoundEnvelope,
    ServiceUnavailableEnvelope,
    SuccessEnvelope,
    TransactionCreate,
    TransactionData,
    TransactionPage,
    TransactionUpdate,
)

router = APIRouter(prefix="/api/v1/transaction", tags=["거래"])
KST = ZoneInfo("Asia/Seoul")


def _current_user(request: Request, credentials):
    """Bearer 토큰으로 현재 사용자를 인증합니다."""
    return auth_service.authenticate(
        request.app.state.engine, request.app.state.settings, bearer_token(credentials)
    )


def _serialize(row: Transaction) -> dict:
    """거래 모델을 API 응답 형식으로 직렬화합니다."""

    def stamp(value: datetime) -> str:
        """날짜시간을 한국 표준시 문자열로 변환합니다."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(KST).isoformat(timespec="seconds")

    return {
        "id": row.id,
        "date": row.date.isoformat(),
        "type": row.type,
        "source": row.source,
        "category": row.category,
        "store_name": row.store_name,
        "note": row.note,
        "amount": row.amount,
        "time": row.time.isoformat() if row.time else None,
        "created_at": stamp(row.created_at),
        "updated_at": stamp(row.updated_at),
    }


def _not_found():
    """거래를 찾지 못한 공통 응답을 반환합니다."""
    return respond(
        404, code="RESOURCE_NOT_FOUND", message="The requested resource was not found."
    )


INVALID_REQUEST_RESPONSE = documented_response(
    InvalidRequestEnvelope, "잘못된 요청입니다."
)
AUTHENTICATION_REQUIRED_RESPONSE = documented_response(
    AuthenticationRequiredEnvelope, "Bearer 인증이 필요합니다.", authentication=True
)
SERVICE_UNAVAILABLE_RESPONSE = documented_response(
    ServiceUnavailableEnvelope, "DB를 일시적으로 사용할 수 없습니다."
)
NOT_FOUND_RESPONSE = documented_response(
    ResourceNotFoundEnvelope, "본인 소유 거래를 찾을 수 없습니다."
)


@router.get(
    "",
    response_model=SuccessEnvelope[TransactionPage],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="거래 목록 조회",
    operation_id="get_api_v1_transaction",
)
def list_transaction(
    request: Request,
    start_date: Annotated[
        str | None, Query(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    ] = None,
    end_date: Annotated[
        str | None, Query(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """인증 사용자의 거래 목록을 조회합니다."""
    user = _current_user(request, credentials)
    start = date.fromisoformat(start_date) if start_date else None
    end = date.fromisoformat(end_date) if end_date else None
    if start and end and start > end:
        raise ValueError("Invalid date range")
    try:
        cursor_id = int(cursor) if cursor else None
    except ValueError:
        raise ValueError("Invalid cursor") from None
    with read_session(request.app.state.engine) as session:
        stmt = select(Transaction).where(Transaction.user_id == user.id)
        if start:
            stmt = stmt.where(Transaction.date >= start)
        if end:
            stmt = stmt.where(Transaction.date <= end)
        if cursor_id:
            stmt = stmt.where(Transaction.id < cursor_id)
        rows = list(
            session.scalars(
                stmt.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(
                    limit + 1
                )
            )
        )
        more = len(rows) > limit
        rows = rows[:limit]
        return respond(
            200,
            {
                "items": [_serialize(row) for row in rows],
                "next_cursor": str(rows[-1].id) if more and rows else None,
            },
        )


@router.post(
    "",
    response_model=CreatedEnvelope,
    status_code=201,
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        409: documented_response(
            IdempotencyConflictEnvelope, "중복 방지 키가 충돌했습니다."
        ),
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="거래 생성",
    operation_id="post_api_v1_transaction",
)
def create_transaction(
    payload: TransactionCreate,
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
    """검증된 거래를 생성합니다."""
    user = _current_user(request, credentials)
    now = datetime.now(timezone.utc)
    with write_session(request.app.state.engine) as session:
        row = Transaction(
            user_id=user.id,
            date=date.fromisoformat(payload.date),
            time=time.fromisoformat(payload.time) if payload.time else None,
            type=payload.type.value,
            source=payload.source.value,
            category=payload.category.value,
            store_name=payload.store_name,
            note=payload.note,
            amount=payload.amount,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        result = _serialize(row)
    return respond(201, result)


@router.get(
    "/{id}",
    response_model=SuccessEnvelope[TransactionData],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        404: NOT_FOUND_RESPONSE,
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="거래 상세 조회",
    operation_id="get_api_v1_transaction_id",
)
def get_transaction(
    id: Annotated[int, Path(ge=1)],
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """본인 소유 거래 한 건을 조회합니다."""
    user = _current_user(request, credentials)
    with read_session(request.app.state.engine) as session:
        row = session.scalar(
            select(Transaction).where(
                Transaction.id == id, Transaction.user_id == user.id
            )
        )
        return respond(200, _serialize(row)) if row else _not_found()


@router.patch(
    "/{id}",
    response_model=SuccessEnvelope[TransactionData],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        404: NOT_FOUND_RESPONSE,
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="거래 수정",
    operation_id="patch_api_v1_transaction_id",
)
def update_transaction(
    id: Annotated[int, Path(ge=1)],
    payload: TransactionUpdate,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """본인 소유 거래를 수정합니다."""
    user = _current_user(request, credentials)
    with write_session(request.app.state.engine) as session:
        row = session.scalar(
            select(Transaction).where(
                Transaction.id == id, Transaction.user_id == user.id
            )
        )
        if not row:
            return _not_found()
        old_date = row.date
        changes = payload.model_dump(exclude_unset=True)
        if "date" in changes:
            changes["date"] = date.fromisoformat(changes["date"])
        if "time" in changes and changes["time"] is not None:
            changes["time"] = time.fromisoformat(changes["time"])
        for key, value in changes.items():
            setattr(row, key, value.value if hasattr(value, "value") else value)
        row.updated_at = datetime.now(timezone.utc)
        for target in {old_date, row.date}:
            for report in session.scalars(
                select(Report).where(
                    Report.user_id == user.id,
                    Report.period_start <= target,
                    Report.period_end >= target,
                )
            ):
                report.is_stale = True
        session.flush()
        result = _serialize(row)
    return respond(200, result)


@router.delete(
    "/{id}",
    response_model=SuccessEnvelope[None],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        404: NOT_FOUND_RESPONSE,
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="거래 삭제",
    operation_id="delete_api_v1_transaction_id",
)
def delete_transaction(
    id: Annotated[int, Path(ge=1)],
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """본인 소유 거래를 삭제합니다."""
    user = _current_user(request, credentials)
    with write_session(request.app.state.engine) as session:
        row = session.scalar(
            select(Transaction).where(
                Transaction.id == id, Transaction.user_id == user.id
            )
        )
        if not row:
            return _not_found()
        for report in session.scalars(
            select(Report).where(
                Report.user_id == user.id,
                Report.period_start <= row.date,
                Report.period_end >= row.date,
            )
        ):
            report.is_stale = True
        session.delete(row)
    return respond(200)
