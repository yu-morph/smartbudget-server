"""계정 생성·로그인 제한·JWT 인증과 본인 정보 변경을 처리합니다."""

import secrets
import time
from base64 import b64encode

import jwt
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from smartbudget_server.auth import security
from smartbudget_server.auth.models import LoginAttempt, User
from smartbudget_server.auth.profile_image import prepare_profile_image
from smartbudget_server.config import Settings
from smartbudget_server.database import read_session, write_session
from smartbudget_server.monthly_budget.models import MonthlyBudget
from smartbudget_server.operations import (
    IdempotencyRecord,
    ReportLock,
    ScheduledReport,
    UserIdCounter,
)
from smartbudget_server.report.models import Report
from smartbudget_server.transaction.models import Transaction

INVALID_LOGIN = "The username or password is incorrect."
INVALID_TOKEN = "Authentication is required or the token is invalid."
INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
CURRENT_PASSWORD_INCORRECT = "CURRENT_PASSWORD_INCORRECT"
CURRENT_PASSWORD_MESSAGE = "The current password is incorrect."
USERNAME_CONFLICT = "USERNAME_CONFLICT"
SIGN_IN_RATE_LIMITED = "SIGN_IN_RATE_LIMITED"


class AuthError(Exception):
    """인증 처리 결과를 비밀 없는 HTTP 응답으로 전달합니다."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retry_after: int | None = None,
    ):
        """HTTP 상태·공개 코드·메시지 및 재시도 시간을 보관합니다."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


def now_seconds() -> int:
    """실패 집계와 가입 시각에 사용할 UTC epoch 초를 반환합니다."""
    return int(time.time())


def _user_id_counter(session: Session) -> UserIdCounter:
    """기존 계정과 고아 데이터의 최대 ID를 지속 카운터에 반영합니다."""
    columns = (
        User.id,
        Transaction.user_id,
        MonthlyBudget.user_id,
        Report.user_id,
        IdempotencyRecord.user_id,
        ReportLock.user_id,
        ScheduledReport.user_id,
    )
    max_existing = max(
        (session.scalar(select(func.max(column))) or 0) for column in columns
    )
    counter = session.get(UserIdCounter, 1)
    if counter is None:
        counter = UserIdCounter(id=1, last_id=max_existing)
        session.add(counter)
    else:
        counter.last_id = max(counter.last_id, max_existing)
    return counter


def initialize_accounts(engine: Engine) -> None:
    """기존 계정 테이블을 이전하고 계정 ID 재사용 방지 카운터를 초기화합니다."""
    with write_session(engine) as session:
        connection = session.connection()
        columns = inspect(connection).get_columns(User.__tablename__)
        if not any(column["name"] == "profile_image" for column in columns):
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN profile_image BLOB"
            )
        _user_id_counter(session)


def register(engine: Engine, username: str, password: str, display_name: str) -> None:
    """정책을 검증하고 고유 아이디·비밀번호 해시·표시 이름을 저장합니다."""
    username = security.normalize_username(username)
    security.validate_password(password)
    display_name = security.validate_display_name(display_name)
    digest = security.hash_password(password)
    try:
        with write_session(engine) as session:
            counter = _user_id_counter(session)
            next_id = counter.last_id + 1
            counter.last_id = next_id
            session.add(
                User(
                    id=next_id,
                    username=username,
                    password_hash=digest,
                    display_name=display_name,
                    token_version=secrets.randbelow(2**63 - 1) + 1,
                    created_at=now_seconds(),
                )
            )
    except IntegrityError as error:
        raise AuthError(
            409, USERNAME_CONFLICT, "The username is already in use."
        ) from error


def sign_in(engine: Engine, settings: Settings, username: str, password: str) -> str:
    """실패 기록을 커밋하고 현재 계정 버전의 JWT를 발급합니다."""
    username = security.normalize_username(username)
    now = now_seconds()
    error = None
    token = None
    # ponytail: 인증 쓰기를 직렬 처리, 처리량이 부족하면 해시 검증 후 상태 재확인으로 분리
    with write_session(engine) as session:
        session.execute(delete(LoginAttempt).where(LoginAttempt.expires_at <= now))
        attempt = session.get(LoginAttempt, username)
        if attempt and attempt.blocked_until and now < attempt.blocked_until:
            error = AuthError(
                429,
                SIGN_IN_RATE_LIMITED,
                "Too many sign-in attempts. Please try again later.",
                attempt.blocked_until - now,
            )
        else:
            user = session.scalar(select(User).where(User.username == username))
            digest = user.password_hash if user else security.DUMMY_PASSWORD_HASH
            valid = security.verify_password(password, digest)
            if valid and user and user.is_active:
                if attempt:
                    session.delete(attempt)
                token = security.issue_token(user.id, user.token_version, settings)
            else:
                if attempt is None:
                    attempt = LoginAttempt(
                        username=username, failures=[], expires_at=now + 300
                    )
                    session.add(attempt)
                attempt.failures = [
                    stamp for stamp in attempt.failures if stamp > now - 300
                ] + [now]
                if len(attempt.failures) >= 10:
                    attempt.blocked_until = now + 180
                    error = AuthError(
                        429,
                        SIGN_IN_RATE_LIMITED,
                        "Too many sign-in attempts. Please try again later.",
                        180,
                    )
                else:
                    error = AuthError(401, INVALID_CREDENTIALS, INVALID_LOGIN)
                attempt.expires_at = attempt.blocked_until or now + 300
    if error:
        raise error
    return token


def _claims(token: str, settings: Settings) -> dict:
    """JWT 검증 오류를 공통 인증 오류로 변환합니다."""
    try:
        return security.decode_token(token, settings)
    except (jwt.PyJWTError, ValueError, TypeError) as error:
        raise AuthError(401, AUTHENTICATION_REQUIRED, INVALID_TOKEN) from error


def _user(session: Session, claims: dict) -> User:
    """토큰의 사용자와 현재 활성·폐기 버전 상태를 대조합니다."""
    user = session.get(User, int(claims["sub"]))
    if not user or not user.is_active or user.token_version != claims["ver"]:
        raise AuthError(401, AUTHENTICATION_REQUIRED, INVALID_TOKEN)
    return user


def authenticate(engine: Engine, settings: Settings, token: str) -> User:
    """JWT와 DB 상태로 인증된 사용자를 읽기 트랜잭션에서 반환합니다."""
    with read_session(engine) as session:
        return authenticate_session(session, settings, token)


def authenticate_session(session: Session, settings: Settings, token: str) -> User:
    """거래 등 상태 변경과 같은 트랜잭션에서 토큰과 계정 상태를 확인합니다."""
    return _user(session, _claims(token, settings))


def refresh(engine: Engine, settings: Settings, token: str) -> str:
    """유효 JWT의 현재 사용자 버전을 재확인하고 새 5일 JWT를 발급합니다."""
    with write_session(engine) as session:
        user = _user(session, _claims(token, settings))
        return security.issue_token(user.id, user.token_version, settings)


def _account_data(user: User) -> dict[str, str | None]:
    """계정 정보를 공개 JSON 필드와 Base64 이미지로 변환합니다."""
    image = user.profile_image
    return {
        "username": user.username,
        "display_name": user.display_name,
        "profile_image": b64encode(image).decode("ascii")
        if image is not None
        else None,
    }


def get_account(
    engine: Engine, settings: Settings, token: str
) -> dict[str, str | None]:
    """인증된 본인의 아이디, 표시 이름과 프로필 이미지를 반환합니다."""
    with read_session(engine) as session:
        return _account_data(authenticate_session(session, settings, token))


def update_account(
    engine: Engine, settings: Settings, token: str, changes: dict
) -> dict[str, str | None]:
    """현재 비밀번호 확인과 본인 정보 변경을 한 쓰기 트랜잭션에 처리합니다."""
    values = dict(changes)
    current_password = values.pop("current_password", None)
    if "display_name" in values:
        values["display_name"] = security.validate_display_name(values["display_name"])
    if "password" in values:
        security.validate_password(values["password"])
    if values.get("profile_image") is not None:
        authenticate(engine, settings, token)
        values["profile_image"] = prepare_profile_image(values["profile_image"])
    with write_session(engine) as session:
        user = authenticate_session(session, settings, token)
        if "password" in values:
            if not security.verify_password(current_password, user.password_hash):
                raise AuthError(
                    401, CURRENT_PASSWORD_INCORRECT, CURRENT_PASSWORD_MESSAGE
                )
            user.password_hash = security.hash_password(values["password"])
            user.token_version += 1
        if "display_name" in values:
            user.display_name = values["display_name"]
        if "profile_image" in values:
            user.profile_image = values["profile_image"]
        result = _account_data(user)
    return result


def delete_account(engine: Engine, settings: Settings, token: str) -> None:
    """인증 사용자와 관련 거래·예산·리포트 및 인증·작업 상태를 함께 삭제합니다."""
    with write_session(engine) as session:
        user = _user(session, _claims(token, settings))
        session.execute(
            delete(LoginAttempt).where(LoginAttempt.username == user.username)
        )
        session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.user_id == user.id)
        )
        session.execute(delete(ReportLock).where(ReportLock.user_id == user.id))
        session.execute(
            delete(ScheduledReport).where(ScheduledReport.user_id == user.id)
        )
        session.execute(delete(Report).where(Report.user_id == user.id))
        session.execute(delete(Transaction).where(Transaction.user_id == user.id))
        session.execute(delete(MonthlyBudget).where(MonthlyBudget.user_id == user.id))
        session.delete(user)
