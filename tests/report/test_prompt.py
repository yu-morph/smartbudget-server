"""외부 분석에 전달하는 거래 행과 한 줄 직렬화를 검증합니다."""

import json
from datetime import date

from smartbudget_server.report.prompt import build_report_input, serialize_report_input
from smartbudget_server.transaction.models import Transaction


def test_report_input_uses_compact_transaction_rows():
    """요일 분석에 필요한 날짜와 거래 유형을 유지하고 식별 정보를 제외합니다."""
    rows = [
        Transaction(
            user_id=1,
            date=date(2026, 9, 16),
            type="expense",
            category="food",
            amount=1000,
            store_name="비밀 상호",
            note="비밀 메모",
        ),
        Transaction(
            user_id=1,
            date=date(2026, 9, 17),
            type="expense",
            category="food",
            amount=500,
            store_name=None,
            note=None,
        ),
        Transaction(
            user_id=1,
            date=date(2026, 9, 18),
            type="income",
            category="salary",
            amount=2000,
            store_name=None,
            note=None,
        ),
    ]
    context = {"interest": "교통비\n절약"}
    result = build_report_input(rows, date(2026, 9, 16), date(2026, 9, 18), context)
    serialized = serialize_report_input(result)
    assert json.loads(serialized) == {
        "period_start": "2026-09-16",
        "period_end": "2026-09-18",
        **context,
        "transactions": [
            ["2026-09-16", "expense", "food", 1000],
            ["2026-09-17", "expense", "food", 500],
            ["2026-09-18", "income", "salary", 2000],
        ],
    }
    assert "\n" not in serialized
    assert "비밀" not in serialized
