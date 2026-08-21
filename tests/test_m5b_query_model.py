from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pandas as pd

from kfcquant.application.queries import DashboardQueryModel
from kfcquant.bootstrap import build_dashboard_query_model
from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import (
    CandidateOutcome,
    CandidateScore,
    EvaluationStatus,
    EventDirection,
    FactorBreakdown,
    NewsDocument,
    OpportunityOutcome,
    OrderSide,
    PaperOrder,
    RiskEvent,
    RiskSeverity,
    RunStatus,
    SignalKind,
    SignalRun,
    SourceTier,
)
from kfcquant.query_models import DuckDBDashboardQueryModel
from kfcquant.services.portfolio import FeeModel
from tests.conftest import strategy_attribution


def test_dashboard_query_model_returns_stable_empty_projections_without_writing(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    before = hashlib.sha256(settings.database_path.read_bytes()).hexdigest()
    queries = DuckDBDashboardQueryModel(database)

    assert isinstance(queries, DashboardQueryModel)
    assert queries.latest_signal(SignalKind.MORNING_WATCHLIST) is None
    assert queries.latest_job("run-preclose") is None
    assert queries.risk_events([]).empty
    assert queries.portfolio().cash == settings.initial_cash
    assert queries.portfolio().positions.empty
    assert queries.trading_activity().orders.empty
    assert queries.trading_activity().fills.empty
    assert queries.evaluations().morning_candidates.empty
    assert queries.evaluations().preclose_candidates.empty
    assert queries.evaluations().opportunities.empty
    assert queries.data_health().jobs.empty
    assert queries.data_health().runs.empty
    assert queries.data_health().news_status_counts.empty
    assert queries.latest_report() is None
    assert hashlib.sha256(settings.database_path.read_bytes()).hexdigest() == before


def test_dashboard_query_model_projects_a_published_signal_and_candidates(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 19, 8, 30, tzinfo=SHANGHAI_TZ)
    run = SignalRun(
        **strategy_attribution(),
        run_id="dashboard-run",
        as_of=at,
        signal_kind=SignalKind.MORNING_WATCHLIST,
        status=RunStatus.SUCCESS,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=False,
        candidate_count=1,
    )
    candidate = CandidateScore(
        run_id=run.run_id,
        ts_code="600000.SH",
        name="浦发银行",
        rank=1,
        opportunity_score=80,
        factor_breakdown=FactorBreakdown(),
        quote_at=at,
    )
    database.save_signal_run(run)
    database.save_candidates([candidate])

    projection = DuckDBDashboardQueryModel(database).latest_signal(
        SignalKind.MORNING_WATCHLIST,
        at.date(),
    )

    assert projection is not None
    assert projection.run["run_id"] == run.run_id
    assert projection.candidates["ts_code"].tolist() == [candidate.ts_code]


def test_dashboard_query_model_projects_cross_context_views_behind_explicit_methods(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 19, 14, 45, tzinfo=SHANGHAI_TZ)
    attribution = strategy_attribution()
    order = PaperOrder(
        **attribution,
        order_id="dashboard-order",
        run_id="dashboard-preclose",
        ts_code="600000.SH",
        side=OrderSide.BUY,
        created_at=at,
        target_value=10_000,
        reason="dashboard fixture",
    )
    database.save_order(order)
    fill, position = FeeModel(settings).buy_fill(10.0, 100, at, order)
    database.apply_buy_fill(fill, position)
    without_quote = DuckDBDashboardQueryModel(database).portfolio()
    assert pd.isna(without_quote.positions.loc[0, "price"])
    database.insert_live_quotes(
        pd.DataFrame(
            [
                {
                    "ts_code": order.ts_code,
                    "captured_at": at,
                    "price": 10.5,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.9,
                    "pre_close": 10.0,
                    "volume": 1_000,
                    "amount": 10_500,
                    "source": "fixture",
                }
            ]
        )
    )
    database.save_candidate_outcome(
        CandidateOutcome(
            **attribution,
            run_id="dashboard-morning",
            ts_code=order.ts_code,
            signal_kind=SignalKind.MORNING_WATCHLIST,
            status=EvaluationStatus.HIT,
            evaluated_at=at,
        )
    )
    database.save_outcome(
        OpportunityOutcome(
            **attribution,
            position_id=position.position_id,
            ts_code=order.ts_code,
            entry_date=at.date(),
            first_day_hit=True,
            five_day_hit=True,
            holding_days=1,
            net_return=0.02,
            recorded_at=at,
        )
    )
    database.start_job("dashboard-job", "run-preclose", at, timedelta(minutes=15))
    database.save_news_documents(
        [
            NewsDocument(
                document_id="dashboard-document",
                title="公告",
                published_at=at,
                source="fixture",
                source_tier=SourceTier.OFFICIAL,
                content_hash="dashboard-document-hash",
                fetched_at=at,
            )
        ]
    )
    event = RiskEvent(
        event_id="dashboard-event",
        document_id="dashboard-document",
        ts_code=order.ts_code,
        event_type="investigation",
        direction=EventDirection.NEGATIVE,
        severity=RiskSeverity.HIGH,
        confidence=0.9,
        hard_block=True,
        evidence="监管调查",
        published_at=at,
        extracted_at=at,
        model_name="fixture",
    )
    database.save_risk_events([event])
    database.save_report("dashboard-report", at.date(), at, "postclose", "# report", "fixture")
    queries = build_dashboard_query_model(settings, database)

    portfolio = queries.portfolio()
    activity = queries.trading_activity()
    evaluations = queries.evaluations()
    health = queries.data_health()
    report = queries.latest_report()

    assert portfolio.positions.loc[0, "market_value"] == 1_050
    assert portfolio.positions.loc[0, "unrealized_pnl"] > 0
    assert activity.orders["order_id"].tolist() == [order.order_id]
    assert activity.fills["order_id"].tolist() == [order.order_id]
    assert evaluations.morning_candidates["status"].tolist() == [EvaluationStatus.HIT.value]
    assert evaluations.preclose_candidates.empty
    assert evaluations.opportunities["position_id"].tolist() == [position.position_id]
    assert health.jobs["job_run_id"].tolist() == ["dashboard-job"]
    assert queries.latest_job("run-preclose", at.date())["job_run_id"] == "dashboard-job"
    assert health.news_status_counts.to_dict("records") == [{"status": "pending", "count": 1}]
    assert queries.risk_events([event.event_id])["event_id"].tolist() == [event.event_id]
    assert report is not None and report["report_id"] == "dashboard-report"
