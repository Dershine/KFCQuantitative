from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import EventDirection, RiskEvent, RiskSeverity


def test_risk_query_enforces_as_of_boundary(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    cutoff = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    before = RiskEvent(
        event_id="before",
        document_id="doc-before",
        ts_code="600000.SH",
        event_type="regulatory_penalty",
        direction=EventDirection.NEGATIVE,
        severity=RiskSeverity.HIGH,
        confidence=0.9,
        hard_block=True,
        evidence="处罚",
        published_at=cutoff - timedelta(minutes=1),
        extracted_at=cutoff,
        model_name="fixture",
    )
    after = before.model_copy(
        update={"event_id": "after", "document_id": "doc-after", "published_at": cutoff + timedelta(minutes=1)}
    )
    database.save_risk_events([before, after])
    result = database.get_risk_events(cutoff - timedelta(days=1), cutoff)
    assert result["event_id"].tolist() == ["before"]


def test_trading_day_lookback_uses_open_days_only(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [
                {"cal_date": date(2026, 8, 6), "is_open": True, "pretrade_date": date(2026, 8, 5)},
                {"cal_date": date(2026, 8, 7), "is_open": True, "pretrade_date": date(2026, 8, 6)},
                {"cal_date": date(2026, 8, 8), "is_open": False, "pretrade_date": date(2026, 8, 7)},
                {"cal_date": date(2026, 8, 9), "is_open": False, "pretrade_date": date(2026, 8, 7)},
                {"cal_date": date(2026, 8, 10), "is_open": True, "pretrade_date": date(2026, 8, 7)},
            ]
        )
    )
    assert database.trading_day_lookback(date(2026, 8, 10), 3) == date(2026, 8, 6)
    assert database.previous_trading_day(date(2026, 8, 10)) == date(2026, 8, 7)
