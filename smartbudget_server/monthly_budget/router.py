"""월별 예산 조회와 저장 API를 구현합니다."""

from typing import Annotated

from fastapi import APIRouter, Path, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select

from smartbudget_server.auth import service as auth_service
from smartbudget_server.auth.router import bearer, bearer_token
from smartbudget_server.database import read_session, write_session
from smartbudget_server.http import documented_response, respond
from smartbudget_server.monthly_budget.models import MonthlyBudget
from smartbudget_server.monthly_budget.schemas import (
    AuthenticationRequiredEnvelope,
    InvalidRequestEnvelope,
    MonthlyBudgetWrite,
    ServiceUnavailableEnvelope,
    SuccessEnvelope,
)

router = APIRouter(prefix="/api/v1/monthly-budget", tags=["월별 예산"])

RESPONSES = {
    400: documented_response(
        InvalidRequestEnvelope, "month 또는 amount 형식이 잘못되었습니다."
    ),
    401: documented_response(
        AuthenticationRequiredEnvelope, "Bearer 인증이 필요합니다.", authentication=True
    ),
    503: documented_response(
        ServiceUnavailableEnvelope, "DB를 일시적으로 사용할 수 없습니다."
    ),
}


def _current_user(request: Request, credentials):
    """Bearer 토큰으로 현재 사용자를 인증합니다."""
    return auth_service.authenticate(
        request.app.state.engine, request.app.state.settings, bearer_token(credentials)
    )


@router.get(
    "/{month}",
    response_model=SuccessEnvelope,
    responses=RESPONSES,
    summary="월별 예산 조회",
    operation_id="get_api_v1_monthly_budget_month",
)
def get_monthly_budget(
    month: Annotated[str, Path(pattern=r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")],
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """인증 사용자의 대상 월 예산을 조회합니다."""
    user = _current_user(request, credentials)
    with read_session(request.app.state.engine) as session:
        row = session.scalar(
            select(MonthlyBudget).where(
                MonthlyBudget.user_id == user.id, MonthlyBudget.month == month
            )
        )
        amount = row.amount if row is not None else None
    return respond(200, {"month": month, "amount": amount})


@router.put(
    "/{month}",
    response_model=SuccessEnvelope,
    responses=RESPONSES,
    summary="월별 예산 저장",
    operation_id="put_api_v1_monthly_budget_month",
)
def put_monthly_budget(
    month: Annotated[str, Path(pattern=r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")],
    payload: MonthlyBudgetWrite,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(bearer)
    ] = None,
):
    """인증 사용자의 대상 월 예산을 생성하거나 교체합니다."""
    user = _current_user(request, credentials)
    with write_session(request.app.state.engine) as session:
        row = session.scalar(
            select(MonthlyBudget).where(
                MonthlyBudget.user_id == user.id, MonthlyBudget.month == month
            )
        )
        if row is None:
            row = MonthlyBudget(user_id=user.id, month=month, amount=payload.amount)
            session.add(row)
        else:
            row.amount = payload.amount
    return respond(200, {"month": month, "amount": payload.amount})
