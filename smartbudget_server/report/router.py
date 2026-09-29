"""소비 분석 리포트의 생성·조회·삭제 API를 구현합니다."""

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
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
from smartbudget_server.report.schemas import (
    AuthenticationRequiredEnvelope,
    GeneratedReport,
    IdempotencyConflictEnvelope,
    InvalidRequestEnvelope,
    PeriodType,
    ProviderErrorEnvelope,
    ProviderTimeoutEnvelope,
    ReportDetail,
    ReportPage,
    ReportRequest,
    ResourceNotFoundEnvelope,
    ServiceUnavailableEnvelope,
    SuccessEnvelope,
)
from smartbudget_server.transaction.models import Transaction

router = APIRouter(prefix="/api/v1/report", tags=["소비 분석"])
KST = ZoneInfo("Asia/Seoul")

INVALID_REQUEST_RESPONSE = documented_response(
    InvalidRequestEnvelope, "분석 요청이 잘못되었습니다."
)
AUTHENTICATION_REQUIRED_RESPONSE = documented_response(
    AuthenticationRequiredEnvelope, "Bearer 인증이 필요합니다.", authentication=True
)
SERVICE_UNAVAILABLE_RESPONSE = documented_response(
    ServiceUnavailableEnvelope, "DB를 일시적으로 사용할 수 없습니다."
)
NOT_FOUND_RESPONSE = documented_response(
    ResourceNotFoundEnvelope, "본인 소유 리포트를 찾을 수 없습니다."
)


def _current_user(request: Request, credentials):
    """Bearer 토큰으로 현재 사용자를 인증합니다."""
    return auth_service.authenticate(
        request.app.state.engine, request.app.state.settings, bearer_token(credentials)
    )


def _stamp(value: datetime) -> str:
    """날짜시간을 한국 표준시 ISO 문자열로 변환합니다."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).isoformat(timespec="seconds")


def _period(period_type: PeriodType, start: date) -> tuple[date, date]:
    """기간 종류에 따라 포함 범위의 마지막 날짜를 계산합니다."""
    if period_type == PeriodType.DAILY:
        return start, start
    if period_type == PeriodType.WEEKLY:
        return start, start + timedelta(days=6)
    if period_type == PeriodType.MONTHLY:
        return start, start.replace(day=monthrange(start.year, start.month)[1])
    return start, start.replace(month=12, day=31)


def _summary(row: Report) -> dict:
    """목록 응답에 필요한 리포트 요약을 직렬화합니다."""
    return {
        "id": row.id,
        "generated_at": _stamp(row.generated_at),
        "period_type": row.period_type,
        "period_start": row.period_start.isoformat(),
        "period_end": row.period_end.isoformat(),
        "is_stale": row.is_stale,
    }


def _detail(row: Report) -> dict:
    """리포트 상세 응답을 직렬화합니다."""
    return {**_summary(row), "full_report": row.full_report}


def _not_found():
    """본인 소유 리포트가 없을 때 공통 404 응답을 반환합니다."""
    return respond(
        404, code="RESOURCE_NOT_FOUND", message="The requested resource was not found."
    )


def _report_text(rows: list[Transaction], start: date, end: date) -> str:
    """개인 식별 정보를 제외한 거래 집계로 분석 문장을 생성합니다."""
    expense = sum(row.amount for row in rows if row.type == "expense")
    income = sum(row.amount for row in rows if row.type == "income")
    categories: dict[str, int] = {}
    for row in rows:
        categories[row.category] = categories.get(row.category, 0) + row.amount
    category_text = ", ".join(
        f"{category}: {amount}원"
        for category, amount in sorted(categories.items(), key=lambda item: -item[1])
    )
    return (
        f"{start.isoformat()}~{end.isoformat()} 소비 분석: "
        f"수입 {income}원, 지출 {expense}원. 카테고리 합계: {category_text}."
    )


@router.get(
    "",
    response_model=SuccessEnvelope[ReportPage],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="분석 조회",
    operation_id="get_api_v1_report",
)
def list_report(
    request: Request,
    period_type: Annotated[PeriodType | None, Query()] = None,
    period_start: Annotated[
        str | None, Query(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """인증 사용자의 소비 분석 목록을 최신순으로 조회합니다."""
    user = _current_user(request, credentials)
    if (period_type is None) != (period_start is None):
        raise ValueError("period_type and period_start must be supplied together")
    start = date.fromisoformat(period_start) if period_start else None
    try:
        cursor_id = int(cursor) if cursor is not None else None
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc

    with read_session(request.app.state.engine) as session:
        stmt = select(Report).where(Report.user_id == user.id)
        if period_type is not None:
            stmt = stmt.where(
                Report.period_type == period_type.value, Report.period_start == start
            )
        if cursor_id is not None:
            stmt = stmt.where(Report.id < cursor_id)
        rows = list(
            session.scalars(
                stmt.order_by(Report.generated_at.desc(), Report.id.desc()).limit(
                    limit + 1
                )
            )
        )
    more = len(rows) > limit
    rows = rows[:limit]
    return respond(
        200,
        {
            "items": [_summary(row) for row in rows],
            "next_cursor": str(rows[-1].id) if more and rows else None,
        },
    )


@router.post(
    "",
    response_model=SuccessEnvelope[GeneratedReport],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        409: documented_response(
            IdempotencyConflictEnvelope, "리포트 생성 요청이 충돌했습니다."
        ),
        502: documented_response(
            ProviderErrorEnvelope, "외부 LLM 호출에 실패했습니다."
        ),
        503: SERVICE_UNAVAILABLE_RESPONSE,
        504: documented_response(
            ProviderTimeoutEnvelope, "외부 LLM 호출 시간이 초과되었습니다."
        ),
    },
    summary="분석 요청",
    operation_id="post_api_v1_report",
)
def create_report(
    payload: ReportRequest,
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
    """지정 기간의 거래를 집계해 소비 분석 리포트를 생성합니다."""
    del idempotency_key
    user = _current_user(request, credentials)
    start = date.fromisoformat(payload.period_start)
    start, end = _period(payload.period_type, start)
    requested_at = datetime.now(timezone.utc)
    timer = perf_counter()

    with read_session(request.app.state.engine) as session:
        transactions = list(
            session.scalars(
                select(Transaction).where(
                    Transaction.user_id == user.id,
                    Transaction.date >= start,
                    Transaction.date <= end,
                )
            )
        )
    if not transactions:
        return respond(
            400,
            code="REPORT_NO_TRANSACTIONS",
            message="There are no transactions in the requested period.",
        )

    full_report = _report_text(transactions, start, end)
    generated_at = datetime.now(timezone.utc)
    processing_time_ms = max(0, int((perf_counter() - timer) * 1000))

    with write_session(request.app.state.engine) as session:
        row = session.scalar(
            select(Report).where(
                Report.user_id == user.id,
                Report.period_type == payload.period_type.value,
                Report.period_start == start,
            )
        )
        if row is None:
            row = Report(
                user_id=user.id,
                period_type=payload.period_type.value,
                period_start=start,
                period_end=end,
                full_report=full_report,
                requested_at=requested_at,
                generated_at=generated_at,
                processing_time_ms=processing_time_ms,
                is_stale=False,
            )
            session.add(row)
            session.flush()
        else:
            row.period_end = end
            row.full_report = full_report
            row.requested_at = requested_at
            row.generated_at = generated_at
            row.processing_time_ms = processing_time_ms
            row.is_stale = False
        data = {
            **_detail(row),
            "requested_at": _stamp(row.requested_at),
            "processing_time_ms": row.processing_time_ms,
        }
    return respond(200, data)


@router.get(
    "/{id}",
    response_model=SuccessEnvelope[ReportDetail],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        404: NOT_FOUND_RESPONSE,
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="분석 상세 조회",
    operation_id="get_api_v1_report_id",
)
def get_report(
    id: Annotated[int, Path(ge=1)],
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """본인 소유 소비 분석 한 건을 조회합니다."""
    user = _current_user(request, credentials)
    with read_session(request.app.state.engine) as session:
        row = session.scalar(
            select(Report).where(Report.id == id, Report.user_id == user.id)
        )
        if row is None:
            return _not_found()
        data = _detail(row)
    return respond(200, data)


@router.delete(
    "/{id}",
    response_model=SuccessEnvelope[None],
    responses={
        400: INVALID_REQUEST_RESPONSE,
        401: AUTHENTICATION_REQUIRED_RESPONSE,
        404: NOT_FOUND_RESPONSE,
        503: SERVICE_UNAVAILABLE_RESPONSE,
    },
    summary="분석 삭제",
    operation_id="delete_api_v1_report_id",
)
def delete_report(
    id: Annotated[int, Path(ge=1)],
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """본인 소유 소비 분석을 삭제합니다."""
    user = _current_user(request, credentials)
    with write_session(request.app.state.engine) as session:
        row = session.scalar(
            select(Report).where(Report.id == id, Report.user_id == user.id)
        )
        if row is None:
            return _not_found()
        session.delete(row)
    return respond(200)
