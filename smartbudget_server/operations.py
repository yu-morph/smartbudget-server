"""외부 호출의 중복 방지와 사용자별 리포트 생성 잠금을 저장합니다."""

import time
from uuid import uuid4

from sqlalchemy import JSON, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from smartbudget_server.database import Base, write_session


class UserIdCounter(Base):
    """삭제된 사용자의 숫자 ID를 재사용하지 않도록 최댓값을 보관합니다."""

    __tablename__ = "user_id_counter"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_id: Mapped[int] = mapped_column(Integer)


class IdempotencyRecord(Base):
    """사용자와 작업별 키의 요청 지문과 성공 결과를 보관합니다."""

    __tablename__ = "proxy_idempotency_records"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation: Mapped[str] = mapped_column(String, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    response: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)


class ReportLock(Base):
    """동일 사용자의 같은 기간에 분석 요청이 겹치는 것을 막습니다."""

    __tablename__ = "report_generation_locks"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_type: Mapped[str] = mapped_column(String(7), primary_key=True)
    period_start: Mapped[str] = mapped_column(String(10), primary_key=True)
    owner: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[float] = mapped_column(Float)


class ScheduleState(Base):
    """예약 작업을 생성한 마지막 한국 시간 날짜를 보관합니다."""

    __tablename__ = "report_schedule_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_date: Mapped[str] = mapped_column(String(10))


class ScheduledReport(Base):
    """예약된 사용자·기간별 분석 처리 상태를 보관합니다."""

    __tablename__ = "scheduled_reports"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_type: Mapped[str] = mapped_column(String(7), primary_key=True)
    period_start: Mapped[str] = mapped_column(String(10), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))


def begin_report(
    engine,
    user_id: int,
    period_type: str,
    period_start: str,
    key: str | None,
    fingerprint: str,
):
    """요청 지문으로 키를 검사하고 기간 잠금을 한 트랜잭션에서 확보합니다."""
    now = time.time()
    owner = uuid4().hex
    with write_session(engine) as session:
        if key is not None:
            record = session.get(IdempotencyRecord, (user_id, "report", key))
            if record and now - (record.completed_at or record.created_at) >= 86400:
                session.delete(record)
                session.flush()
                record = None
            if record:
                if record.fingerprint != fingerprint:
                    return "IDEMPOTENCY_KEY_CONFLICT", None
                if record.completed_at is None:
                    return "IDEMPOTENCY_REQUEST_IN_PROGRESS", None
                return "replay", record.response
        lock = session.get(ReportLock, (user_id, period_type, period_start))
        if lock and lock.expires_at > now:
            return "REPORT_GENERATION_IN_PROGRESS", None
        if lock:
            session.delete(lock)
            session.flush()
        session.add(
            ReportLock(
                user_id=user_id,
                period_type=period_type,
                period_start=period_start,
                owner=owner,
                expires_at=now + 120,
            )
        )
        if key is not None:
            session.add(
                IdempotencyRecord(
                    user_id=user_id,
                    operation="report",
                    key=key,
                    fingerprint=fingerprint,
                    created_at=now,
                )
            )
    return "started", owner


def finish_report(
    session,
    user_id: int,
    period_type: str,
    period_start: str,
    key: str | None,
    owner: str,
    data: dict | None,
):
    """소유 잠금을 해제하고 성공 시 원 응답을 원자적으로 기록합니다."""
    lock = session.get(ReportLock, (user_id, period_type, period_start))
    if lock is None or lock.owner != owner:
        raise RuntimeError("Report generation lock lost")
    session.delete(lock)
    if key is not None:
        record = session.get(IdempotencyRecord, (user_id, "report", key))
        if record is None:
            raise RuntimeError("Idempotency record missing")
        if data is None:
            session.delete(record)
        else:
            record.completed_at = time.time()
            record.response = data


def begin_ocr(engine, user_id: int, key: str, fingerprint: str):
    """OCR 요청의 키를 원자적으로 확인하고 진행 중 상태로 예약합니다."""
    now = time.time()
    with write_session(engine) as session:
        record = session.get(IdempotencyRecord, (user_id, "ocr", key))
        if record and now - (record.completed_at or record.created_at) >= 86400:
            session.delete(record)
            session.flush()
            record = None
        if record:
            if record.fingerprint != fingerprint:
                return "IDEMPOTENCY_KEY_CONFLICT", None
            if record.completed_at is None:
                return "IDEMPOTENCY_REQUEST_IN_PROGRESS", None
            return "replay", record.response
        session.add(
            IdempotencyRecord(
                user_id=user_id,
                operation="ocr",
                key=key,
                fingerprint=fingerprint,
                created_at=now,
            )
        )
    return "started", None


def finish_ocr(engine, user_id: int, key: str, data: list | None):
    """OCR 성공 응답을 기록하거나 실패한 요청의 키를 해제합니다."""
    with write_session(engine) as session:
        record = session.get(IdempotencyRecord, (user_id, "ocr", key))
        if record is None:
            raise RuntimeError("OCR idempotency record missing")
        if data is None:
            session.delete(record)
        else:
            record.completed_at = time.time()
            record.response = data
