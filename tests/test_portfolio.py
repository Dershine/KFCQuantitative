from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import (
    CandidateScore,
    FactorBreakdown,
    IntradayBar,
    RunStatus,
    SignalRun,
)
from kfcquant.services.portfolio import FeeModel, PortfolioService


class FakeLive:
    def __init__(self):
        self.quotes = pd.DataFrame()
        self.bars: list[IntradayBar] = []

    def fetch_quotes(self, ts_codes=None):
        frame = self.quotes.copy()
        return frame if not ts_codes else frame[frame["ts_code"].isin(ts_codes)]

    def fetch_intraday_bars(self, ts_code, start, end, frequency_minutes=5):
        return [bar for bar in self.bars if bar.ts_code == ts_code]


def factor():
    return FactorBreakdown(
        ret_5d=0.02,
        ret_20d=0.05,
        intraday_strength=0.01,
        close_location=0.8,
        projected_volume_ratio=1.2,
        median_amount_20d=200_000_000,
        volatility_20d=0.02,
        gap_abs=0.003,
        limit_proximity=0.0,
        positive_score=80,
        risk_penalty=5,
    )


def test_buy_fill_uses_incremental_vwap_and_is_idempotent(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    live = FakeLive()
    service = PortfolioService(database, settings, live)
    signal_at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = SignalRun(
        run_id="run-buy",
        as_of=signal_at,
        status=RunStatus.SUCCESS,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=True,
        candidate_count=1,
    )
    database.save_signal_run(run)
    candidate = CandidateScore(
        run_id=run.run_id,
        ts_code="600000.SH",
        name="浦发银行",
        rank=1,
        opportunity_score=75,
        factor_breakdown=factor(),
        quote_at=signal_at,
    )
    database.save_candidates([candidate])
    service.create_candidate_orders(run, [candidate])
    baseline = pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "captured_at": signal_at,
                "price": 10.0,
                "open": 9.9,
                "high": 10.1,
                "low": 9.8,
                "pre_close": 9.8,
                "volume": 1_000_000,
                "amount": 10_000_000,
                "source": "fixture",
            }
        ]
    )
    database.insert_live_quotes(baseline)
    fill_at = signal_at + timedelta(minutes=5)
    current = baseline.copy()
    current["captured_at"] = fill_at
    current["price"] = 10.1
    current["volume"] = 1_100_000
    current["amount"] = 11_010_000  # interval VWAP = 10.10
    database.insert_live_quotes(current)

    fills = service.capture_buy_fills(run.run_id, fill_at, current)
    assert len(fills) == 1
    assert fills[0].raw_price == 10.1
    assert fills[0].shares % 100 == 0
    assert service.capture_buy_fills(run.run_id, fill_at, current) == []


def test_same_bar_stop_has_priority_and_t_plus_one(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    live = FakeLive()
    service = PortfolioService(database, settings, live)
    entry_at = datetime(2026, 8, 10, 14, 45, tzinfo=SHANGHAI_TZ)
    run = SignalRun(
        run_id="entry",
        as_of=entry_at - timedelta(minutes=5),
        status=RunStatus.SUCCESS,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=True,
    )
    database.save_signal_run(run)
    candidate = CandidateScore(
        run_id=run.run_id,
        ts_code="000001.SZ",
        name="平安银行",
        rank=1,
        opportunity_score=80,
        factor_breakdown=factor(),
        quote_at=run.as_of,
    )
    database.save_candidates([candidate])
    order = service.create_candidate_orders(run, [candidate])[0]
    fill, position = FeeModel(settings).buy_fill(10.0, 1000, entry_at, order)
    database.apply_buy_fill(fill, position)
    target, stop = service.fees.trigger_prices(position)
    next_day = entry_at + timedelta(days=1)
    live.bars = [
        IntradayBar(
            ts_code=position.ts_code,
            start_at=next_day.replace(hour=9, minute=30),
            end_at=next_day.replace(hour=9, minute=35),
            open=position.cost_basis,
            high=target + 0.1,
            low=stop - 0.1,
            close=position.cost_basis,
            volume=100_000,
            amount=1_000_000,
            source="fixture",
        )
    ]
    assert service.monitor_positions(entry_at.replace(hour=14, minute=50)) == []
    closed = service.monitor_positions(next_day.replace(hour=9, minute=35))
    assert len(closed) == 1
    positions = database.table("paper_positions")
    assert positions.iloc[0]["exit_reason"] == "stop_loss"


def test_buy_is_rejected_without_interval_volume(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    service = PortfolioService(database, settings, FakeLive())
    signal_at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = SignalRun(
        run_id="no-volume",
        as_of=signal_at,
        status=RunStatus.SUCCESS,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=True,
    )
    database.save_signal_run(run)
    candidate = CandidateScore(
        run_id=run.run_id,
        ts_code="600000.SH",
        name="浦发银行",
        rank=1,
        opportunity_score=75,
        factor_breakdown=factor(),
        quote_at=signal_at,
    )
    database.save_candidates([candidate])
    service.create_candidate_orders(run, [candidate])
    quote = pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "captured_at": signal_at,
                "price": 10.0,
                "open": 9.9,
                "high": 10.1,
                "low": 9.8,
                "pre_close": 9.8,
                "volume": 1_000_000,
                "amount": 10_000_000,
                "source": "fixture",
            }
        ]
    )
    database.insert_live_quotes(quote)
    quote["captured_at"] = signal_at + timedelta(minutes=5)
    assert service.capture_buy_fills(run.run_id, signal_at + timedelta(minutes=5), quote) == []
    assert database.proposed_orders(run.run_id).empty


def test_missing_today_signal_does_not_force_score_exit(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    live = FakeLive()
    service = PortfolioService(database, settings, live)
    entry_at = datetime(2026, 8, 10, 14, 45, tzinfo=SHANGHAI_TZ)
    run = SignalRun(
        run_id="old-signal",
        as_of=entry_at - timedelta(minutes=5),
        status=RunStatus.SUCCESS,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=True,
    )
    database.save_signal_run(run)
    candidate = CandidateScore(
        run_id=run.run_id,
        ts_code="000001.SZ",
        name="平安银行",
        rank=1,
        opportunity_score=80,
        factor_breakdown=factor(),
        quote_at=run.as_of,
    )
    database.save_candidates([candidate])
    order = service.create_candidate_orders(run, [candidate])[0]
    fill, position = FeeModel(settings).buy_fill(10.0, 1000, entry_at, order)
    database.apply_buy_fill(fill, position)

    result = service.monitor_positions(entry_at + timedelta(days=1))
    assert result == []
    assert database.get_open_positions().iloc[0]["status"] == "open"


def test_order_reserve_count_uses_shared_selection_policy(settings):
    configured = settings.model_copy(
        update={
            "max_positions": 2,
            "position_fraction": 0.5,
            "selection": settings.selection.model_copy(update={"top_n": 2, "candidate_limit": 3}),
        }
    )
    database = Database(configured.database_path, configured.initial_cash)
    database.initialize()
    service = PortfolioService(database, configured, FakeLive())
    signal_at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = SignalRun(
        run_id="selection-limit",
        as_of=signal_at,
        status=RunStatus.SUCCESS,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=True,
    )
    candidates = [
        CandidateScore(
            run_id=run.run_id,
            ts_code=code,
            name=code,
            rank=rank,
            opportunity_score=80 - rank,
            factor_breakdown=factor(),
            quote_at=signal_at,
        )
        for rank, code in enumerate(("600000.SH", "601001.SH", "603001.SH"), start=1)
    ]

    created = service.create_candidate_orders(run, candidates)

    assert [order.ts_code for order in created] == ["600000.SH", "601001.SH"]
