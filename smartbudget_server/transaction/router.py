"""인증 사용자의 단건·일괄 거래 생성, 조회, 수정 및 삭제를 처리합니다."""

import hashlib
import time as clock
from datetime import date, datetime, time, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, select, tuple_, update

from smartbudget_server.auth import service as auth_service
from smartbudget_server.auth.router import bearer, bearer_token
from smartbudget_server.database import read_session, write_session
from smartbudget_server.http import documented_response, respond
from smartbudget_server.operations import IdempotencyRecord
from smartbudget_server.report.models import Report
from smartbudget_server.transaction.models import Transaction
from smartbudget_server.transaction.schemas import (
    DATE_PATTERN,
    AuthenticationRequiredEnvelope,
    CreatedEnvelope,
    IdempotencyConflictEnvelope,
    InvalidRequestEnvelope,
    ResourceNotFoundEnvelope,
    ServiceUnavailableEnvelope,
    SuccessEnvelope,
    TransactionBatchCreate,
    TransactionBatchDeleted,
    TransactionCreate,
    TransactionData,
    TransactionPage,
    TransactionUpdate,
)

router = APIRouter(prefix="/api/v1/transaction", tags=["거래"])
KST = ZoneInfo("Asia/Seoul")
DateQuery = Annotated[str | None, Query(pattern=DATE_PATTERN)]
CursorQuery = Annotated[str | None, Query(min_length=1, max_length=32)]
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]


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


def _date_range(start_date: str | None, end_date: str | None):
    """조회와 일괄 변경에 동일한 양끝 포함 날짜 범위를 적용합니다."""
    start = date.fromisoformat(start_date) if start_date else None
    end = date.fromisoformat(end_date) if end_date else None
    if start and end and start > end:
        raise ValueError("Invalid date range")
    return start, end


def _transaction_filter(
    user_id: int, start: date | None, end: date | None, cursor: str | None
):
    """사용자·날짜·커서 조건을 조회와 일괄 삭제에 공통으로 적용합니다."""
    conditions = [Transaction.user_id == user_id]
    if start is not None:
        conditions.append(Transaction.date >= start)
    if end is not None:
        conditions.append(Transaction.date <= end)
    if cursor is not None:
        cursor_date, separator, cursor_id = cursor.rpartition(":")
        parsed_id = int(cursor_id)
        if not 1 <= parsed_id <= 2**63 - 1:
            raise ValueError("Invalid cursor ID")
        if separator:
            conditions.append(
                tuple_(Transaction.date, Transaction.id)
                < (date.fromisoformat(cursor_date), parsed_id)
            )
        else:
            conditions.append(Transaction.id < parsed_id)
    return conditions


def _ordered(statement):
    """거래를 날짜와 ID의 내림차순으로 정렬합니다."""
    return statement.order_by(Transaction.date.desc(), Transaction.id.desc())


def _next_cursor(rows, limit: int) -> str | None:
    """삭제 후에도 사용할 수 있는 날짜와 ID 커서를 생성합니다."""
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return f"{last.date.isoformat()}:{last.id}"


def _new_transaction(
    user_id: int, payload: TransactionCreate, now: datetime
) -> Transaction:
    """단건 생성과 일괄 생성에 동일한 저장 모델을 구성합니다."""
    return Transaction(
        user_id=user_id,
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


def _mark_reports_stale(session, user_id: int, dates: set[date]) -> None:
    """실제 거래 변경일을 포함하는 사용자 리포트를 갱신 대상으로 표시합니다."""
    for target in dates:
        session.execute(
            update(Report)
            .where(
                Report.user_id == user_id,
                Report.is_stale.is_(False),
                Report.period_start <= target,
                Report.period_end >= target,
            )
            .values(is_stale=True)
        )


async def _validate_batch_transport(request: Request) -> None:
    """일괄 API의 날짜 쿼리와 메서드별 본문 계약을 확인합니다."""
    allowed = {"start_date", "end_date"}
    if request.method == "DELETE":
        allowed.update(("limit", "cursor"))
        if await request.body():
            raise ValueError("DELETE does not accept a body")
    elif (
        request.headers.get("content-type", "").split(";")[0].strip().lower()
        != "application/json"
    ):
        raise ValueError("Batch creation requires JSON")
    supplied = request.query_params.multi_items()
    if (
        len(supplied) != len(request.query_params)
        or set(request.query_params) - allowed
    ):
        raise ValueError("Invalid query parameters")


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
    start_date: DateQuery = None,
    end_date: DateQuery = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: CursorQuery = None,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """인증 사용자의 거래 목록을 조회합니다."""
    user = _current_user(request, credentials)
    conditions = _transaction_filter(
        user.id, *_date_range(start_date, end_date), cursor
    )
    with read_session(request.app.state.engine) as session:
        rows = list(
            session.scalars(
                _ordered(select(Transaction).where(*conditions)).limit(limit + 1)
            )
        )
        return respond(
            200,
            {
                "items": [_serialize(row) for row in rows[:limit]],
                "next_cursor": _next_cursor(rows, limit),
            },
        )


@router.post(
    "",
    response_model=CreatedEnvelope[TransactionData],
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
    idempotency_key: IdempotencyKey = None,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """검증된 거래를 생성합니다."""
    user = _current_user(request, credentials)
    now = datetime.now(timezone.utc)
    with write_session(request.app.state.engine) as session:
        row = _new_transaction(user.id, payload, now)
        session.add(row)
        session.flush()
        _mark_reports_stale(session, user.id, {row.date})
        result = _serialize(row)
    return respond(201, result)


@router.post(
    "/batch",
    dependencies=[Depends(_validate_batch_transport)],
    response_model=CreatedEnvelope[list[TransactionData]],
    status_code=201,
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        409: documented_response(
            IdempotencyConflictEnvelope, "중복 방지 키가 충돌했습니다."
        ),
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="기간 내 거래 일괄 생성",
    operation_id="post_api_v1_transaction_batch",
)
def create_transaction_batch(
    payload: TransactionBatchCreate,
    request: Request,
    start_date: DateQuery,
    end_date: DateQuery,
    idempotency_key: IdempotencyKey = None,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """요청 기간 내 모든 거래를 한 쓰기 트랜잭션으로 생성합니다."""
    token = bearer_token(credentials)
    start, end = _date_range(start_date, end_date)
    now = datetime.now(timezone.utc)
    with write_session(request.app.state.engine) as session:
        user = auth_service.authenticate_session(
            session, request.app.state.settings, token
        )
        rows = [_new_transaction(user.id, item, now) for item in payload.items]
        dates = {row.date for row in rows}
        if min(dates) < start or max(dates) > end:
            raise ValueError("Transaction date outside batch period")

        record = None
        if idempotency_key is not None:
            fingerprint = hashlib.sha256(
                f"{start}:{end}:{payload.model_dump_json()}".encode("utf-8")
            ).hexdigest()
            timestamp = clock.time()
            record = session.get(
                IdempotencyRecord, (user.id, "transaction_batch", idempotency_key)
            )
            if record and timestamp - record.created_at < 86400:
                if record.fingerprint != fingerprint:
                    return respond(
                        409,
                        code="IDEMPOTENCY_KEY_CONFLICT",
                        message="The idempotency key has already been used for a different request.",
                    )
                return respond(201, record.response)
            if record is None:
                record = IdempotencyRecord(
                    user_id=user.id,
                    operation="transaction_batch",
                    key=idempotency_key,
                )
                session.add(record)
            record.fingerprint = fingerprint
            record.created_at = record.completed_at = timestamp

        session.add_all(rows)
        session.flush()
        _mark_reports_stale(session, user.id, dates)
        result = [_serialize(row) for row in rows]
        if record is not None:
            record.response = result
    return respond(201, result)


@router.delete(
    "/batch",
    dependencies=[Depends(_validate_batch_transport)],
    response_model=SuccessEnvelope[TransactionBatchDeleted],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="기간 내 거래 일괄 삭제",
    operation_id="delete_api_v1_transaction_batch",
)
def delete_transaction_batch(
    request: Request,
    start_date: DateQuery,
    end_date: DateQuery,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: CursorQuery = None,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """선택 기간 또는 페이지의 본인 거래와 관련 리포트 상태를 함께 변경합니다."""
    token = bearer_token(credentials)
    start, end = _date_range(start_date, end_date)
    with write_session(request.app.state.engine) as session:
        user = auth_service.authenticate_session(
            session, request.app.state.settings, token
        )
        conditions = _transaction_filter(user.id, start, end, cursor)
        next_cursor = None
        if limit is not None:
            page = _ordered(select(Transaction.id, Transaction.date).where(*conditions))
            rows = list(session.execute(page.limit(limit + 1)))
            next_cursor = _next_cursor(rows, limit)
            conditions.append(Transaction.id.in_([row.id for row in rows[:limit]]))
        affected_report = (
            select(Transaction.id)
            .where(
                *conditions,
                Transaction.date.between(Report.period_start, Report.period_end),
            )
            .exists()
        )
        session.execute(
            update(Report)
            .where(
                Report.user_id == user.id, Report.is_stale.is_(False), affected_report
            )
            .values(is_stale=True)
        )
        result = session.execute(delete(Transaction).where(*conditions))
    return respond(200, {"deleted_count": result.rowcount, "next_cursor": next_cursor})


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
        _mark_reports_stale(session, user.id, {old_date, row.date})
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
        _mark_reports_stale(session, user.id, {row.date})
        session.delete(row)
    return respond(200)
