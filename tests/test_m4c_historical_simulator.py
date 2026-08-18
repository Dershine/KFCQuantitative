from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.historical_simulator import (
    HistoricalExecutionSimulator,
    HistoricalSignal,
    HistoricalSimulationConfig,
    HistoricalSimulationViolation,
)
from kfcquant.models import (
    CandidateScore,
    DailyBar,
    FactorBreakdown,
    OrderSide,
    PaperOrder,
    RunStatus,
    SignalKind,
    SignalRun,
)
from kfcquant.services.portfolio import FeeModel
from tests.conftest import strategy_attribution


def _run(
    at: datetime,
    *,
    run_id: str = "historical-run",
    tradable: bool = True,
) -> SignalRun:
    return SignalRun(
        **strategy_attribution(),
        run_id=run_id,
        as_of=at,
        information_cutoff=at,
        status=RunStatus.SUCCESS,
        data_fresh=True,
        official_news_healthy=True,
        mainstream_news_healthy=True,
        tradable=tradable,
    )


def _candidate(run: SignalRun, *, blocked: bool = False, rank: int = 1) -> CandidateScore:
    return CandidateScore(
        run_id=run.run_id,
        ts_code="600000.SH",
        name="浦发银行",
        rank=rank,
        opportunity_score=80,
        factor_breakdown=FactorBreakdown(technical_score=80),
        blocked=blocked,
        block_reasons=["fixture-risk"] if blocked else [],
        quote_at=run.as_of,
    )


def _bar(
    session: date,
    *,
    open_price: float = 10.0,
    high: float = 10.1,
    low: float = 9.9,
    close: float = 10.0,
    volume: float = 1_000_000,
    amount: float = 10_000_000,
    up_limit: float = 11.0,
    down_limit: float = 9.0,
    suspended: bool = False,
) -> DailyBar:
    return DailyBar(
        ts_code="600000.SH",
        trade_date=session,
        open=open_price,
        high=high,
        low=low,
        close=close,
        pre_close=10.0,
        volume=volume,
        amount=amount,
        adj_factor=1.0,
        up_limit=up_limit,
        down_limit=down_limit,
        suspended=suspended,
        is_st=False,
    )


def test_simulation_config_is_explicit_and_rejects_t_plus_zero(settings):
    config = HistoricalSimulationConfig.from_settings(settings)

    assert config.initial_cash == settings.initial_cash
    assert config.commission_rate == settings.commission_rate
    assert config.min_commission == settings.min_commission
    assert config.stamp_duty_rate == settings.stamp_duty_rate
    assert config.slippage_rate == settings.slippage_rate
    assert config.settlement_delay_sessions == 1
    assert config.reject_suspended
    assert config.require_positive_turnover
    assert config.reject_at_price_limit

    with pytest.raises(ValueError, match=r"T\+1"):
        HistoricalSimulationConfig(settlement_delay_sessions=0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"initial_cash": 0},
        {"max_positions": 0},
        {"position_fraction": 0},
        {"max_positions": 2, "position_fraction": 0.6},
        {"commission_rate": -0.01},
        {"take_profit_net": 1.0},
    ],
)
def test_simulation_config_rejects_unsafe_financial_assumptions(overrides):
    with pytest.raises(ValueError):
        HistoricalSimulationConfig(**overrides)


def test_multiday_simulation_preserves_t_plus_one_costs_cash_and_stop_priority(settings):
    first = date(2026, 8, 10)
    second = date(2026, 8, 11)
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at)
    candidate = _candidate(run)
    bars = [
        _bar(first, high=10.5, low=9.5),
        _bar(second, open_price=10.0, high=10.5, low=9.5, close=10.1),
    ]
    simulator = HistoricalExecutionSimulator(HistoricalSimulationConfig.from_settings(settings))

    result = simulator.simulate(
        [HistoricalSignal(run=run, candidates=(candidate,))],
        bars,
        sessions=[first, second],
    )

    assert [fill.side for fill in result.fills] == [OrderSide.BUY, OrderSide.SELL]
    assert result.fills[0].filled_at.date() == first
    assert result.fills[1].filled_at.date() == second
    assert result.fills[1].reason == "stop_loss"  # same bar crosses both; stop is conservative.
    assert result.open_positions == ()
    assert result.closed_positions[0].exit_reason == "stop_loss"
    assert result.ending_cash == pytest.approx(
        result.initial_cash + sum(fill.total_cash_change for fill in result.fills)
    )
    assert result.ending_cash >= 0


def test_historical_cost_math_matches_live_fee_model(settings):
    session = date(2026, 8, 10)
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at)
    candidate = _candidate(run)
    config = HistoricalSimulationConfig.from_settings(settings)
    simulator = HistoricalExecutionSimulator(config)
    result = simulator.simulate(
        [HistoricalSignal(run=run, candidates=(candidate,))],
        [_bar(session)],
        sessions=[session],
    )
    historical = result.fills[0]
    order = PaperOrder(
        **strategy_attribution(),
        order_id=historical.order_id,
        run_id=run.run_id,
        ts_code=candidate.ts_code,
        side=OrderSide.BUY,
        created_at=run.as_of,
        target_value=config.initial_cash * config.position_fraction,
        reason="fixture",
    )
    live_fill, _ = FeeModel(settings).buy_fill(
        historical.raw_price,
        historical.shares,
        historical.filled_at,
        order,
    )

    assert historical.fill_price == live_fill.fill_price
    assert historical.commission == live_fill.commission
    assert historical.stamp_duty == live_fill.stamp_duty
    assert historical.slippage == live_fill.slippage
    assert historical.total_cash_change == live_fill.total_cash_change


def test_take_profit_sell_costs_match_live_fee_model(settings):
    first = date(2026, 8, 10)
    second = date(2026, 8, 11)
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id="take-profit")
    simulator = HistoricalExecutionSimulator(HistoricalSimulationConfig.from_settings(settings))
    result = simulator.simulate(
        [HistoricalSignal(run=run, candidates=(_candidate(run),))],
        [
            _bar(first),
            _bar(second, open_price=10.1, high=10.5, low=10.0, close=10.2),
        ],
        sessions=[first, second],
    )
    historical = result.fills[-1]
    position = result.closed_positions[0]
    order = PaperOrder(
        **strategy_attribution(),
        order_id=historical.order_id,
        run_id=run.run_id,
        ts_code=historical.ts_code,
        side=OrderSide.SELL,
        created_at=historical.filled_at,
        target_value=historical.raw_price * historical.shares,
        reason="take_profit",
        position_id=position.position_id,
    )
    live_fill = FeeModel(settings).sell_fill(
        historical.raw_price,
        historical.shares,
        historical.filled_at,
        order,
    )

    assert historical.reason == "take_profit"
    assert historical.fill_price == live_fill.fill_price
    assert historical.commission == live_fill.commission
    assert historical.stamp_duty == live_fill.stamp_duty
    assert historical.slippage == live_fill.slippage
    assert historical.total_cash_change == live_fill.total_cash_change


@pytest.mark.parametrize(
    ("run_tradable", "blocked", "bar", "reason"),
    [
        (False, False, _bar(date(2026, 8, 10)), "run_not_tradable"),
        (True, True, _bar(date(2026, 8, 10)), "candidate_blocked"),
        (True, False, _bar(date(2026, 8, 10), suspended=True), "suspended"),
        (True, False, _bar(date(2026, 8, 10), volume=0, amount=0), "no_turnover"),
        (
            True,
            False,
            _bar(date(2026, 8, 10), close=10.95, high=11.0, up_limit=11.0),
            "buy_at_price_limit",
        ),
    ],
)
def test_buy_fails_closed_for_safety_gates(run_tradable, blocked, bar, reason, settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id=f"gate-{reason}", tradable=run_tradable)
    simulator = HistoricalExecutionSimulator(HistoricalSimulationConfig.from_settings(settings))

    result = simulator.simulate(
        [HistoricalSignal(run=run, candidates=(_candidate(run, blocked=blocked),))],
        [bar],
        sessions=[bar.trade_date],
    )

    assert result.fills == ()
    assert result.open_positions == ()
    assert result.ending_cash == result.initial_cash
    assert result.rejections[0].reason == reason


def test_morning_watchlist_never_creates_a_historical_buy(settings):
    session = date(2026, 8, 10)
    at = datetime(2026, 8, 10, 8, 30, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id="morning").model_copy(
        update={"signal_kind": SignalKind.MORNING_WATCHLIST}
    )
    result = HistoricalExecutionSimulator(
        HistoricalSimulationConfig.from_settings(settings)
    ).simulate(
        [HistoricalSignal(run=run, candidates=(_candidate(run),))],
        [_bar(session)],
        sessions=[session],
    )

    assert result.fills == ()
    assert result.rejections[0].reason == "signal_not_buyable"


def test_down_limit_exit_is_deferred_without_breaking_cash(settings):
    sessions = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id="down-limit")
    simulator = HistoricalExecutionSimulator(HistoricalSimulationConfig.from_settings(settings))
    result = simulator.simulate(
        [HistoricalSignal(run=run, candidates=(_candidate(run),))],
        [
            _bar(sessions[0]),
            _bar(sessions[1], open_price=9.0, high=9.1, low=9.0, close=9.0, down_limit=9.0),
            _bar(sessions[2], open_price=9.7, high=9.8, low=9.6, close=9.7, down_limit=9.0),
        ],
        sessions=sessions,
    )

    assert [item.reason for item in result.rejections] == ["sell_at_price_limit"]
    assert [fill.side for fill in result.fills] == [OrderSide.BUY, OrderSide.SELL]
    assert result.fills[-1].filled_at.date() == sessions[2]
    assert result.ending_cash == pytest.approx(
        result.initial_cash + sum(fill.total_cash_change for fill in result.fills)
    )


def test_max_holding_uses_explicit_trading_sessions_not_calendar_days(settings):
    sessions = [date(2026, 8, 7), date(2026, 8, 10), date(2026, 8, 11)]
    at = datetime(2026, 8, 7, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id="max-holding")
    config = replace(
        HistoricalSimulationConfig.from_settings(settings),
        max_holding_sessions=2,
    )
    result = HistoricalExecutionSimulator(config).simulate(
        [HistoricalSignal(run=run, candidates=(_candidate(run),))],
        [_bar(session) for session in sessions],
        sessions=sessions,
    )

    assert result.fills[-1].filled_at.date() == sessions[-1]
    assert result.fills[-1].reason == "max_holding_sessions"


def test_missing_bar_duplicate_position_portfolio_limit_and_insufficient_cash_fail_closed(settings):
    first = date(2026, 8, 10)
    second = date(2026, 8, 11)
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id="first")
    same_code_run = _run(at + timedelta(days=1), run_id="duplicate")
    other = _candidate(run).model_copy(update={"ts_code": "000001.SZ", "name": "平安银行", "rank": 2})
    config = replace(HistoricalSimulationConfig.from_settings(settings), max_positions=1, position_fraction=1.0)
    simulator = HistoricalExecutionSimulator(config)
    result = simulator.simulate(
        [
            HistoricalSignal(run=run, candidates=(_candidate(run), other)),
            HistoricalSignal(run=same_code_run, candidates=(_candidate(same_code_run),)),
        ],
        [_bar(first), _bar(second, high=10.1, low=9.9)],
        sessions=[first, second],
    )

    assert [item.reason for item in result.rejections] == ["portfolio_full", "position_already_open"]

    poor = replace(
        HistoricalSimulationConfig.from_settings(settings),
        initial_cash=50,
        max_positions=1,
        position_fraction=1.0,
    )
    poor_result = HistoricalExecutionSimulator(poor).simulate(
        [HistoricalSignal(run=run, candidates=(_candidate(run),))],
        [_bar(first)],
        sessions=[first],
    )
    assert poor_result.rejections[0].reason == "insufficient_cash"

    missing_result = simulator.simulate(
        [HistoricalSignal(run=run, candidates=(_candidate(run),))],
        [],
        sessions=[first],
    )
    assert missing_result.rejections[0].reason == "missing_bar"


def test_simulation_is_deterministic_and_failure_does_not_publish_partial_result(settings):
    session = date(2026, 8, 10)
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id="deterministic")
    signals = [HistoricalSignal(run=run, candidates=(_candidate(run),))]
    bars = [_bar(session)]
    simulator = HistoricalExecutionSimulator(HistoricalSimulationConfig.from_settings(settings))

    first = simulator.simulate(signals, bars, sessions=[session])
    second = simulator.simulate(signals, bars, sessions=[session])
    assert first == second

    def fail_before_buy(stage: str) -> None:
        if stage == "before_buy_commit":
            raise RuntimeError("buy commit failure")

    with pytest.raises(RuntimeError, match="buy commit failure"):
        simulator.simulate(signals, bars, sessions=[session], event_hook=fail_before_buy)

    def fail_before_publish(stage: str) -> None:
        if stage == "before_result_publish":
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        simulator.simulate(signals, bars, sessions=[session], event_hook=fail_before_publish)

    # The simulator owns no mutable account or persistence; retry is the same complete result.
    assert simulator.simulate(signals, bars, sessions=[session]) == first


def test_failure_before_sell_commit_is_atomic_and_retryable(settings):
    sessions = [date(2026, 8, 10), date(2026, 8, 11)]
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id="sell-failure")
    signals = [HistoricalSignal(run=run, candidates=(_candidate(run),))]
    bars = [_bar(sessions[0]), _bar(sessions[1], high=10.5, low=10.0)]
    simulator = HistoricalExecutionSimulator(HistoricalSimulationConfig.from_settings(settings))

    def fail(stage: str) -> None:
        if stage == "before_sell_commit":
            raise RuntimeError("sell commit failure")

    with pytest.raises(RuntimeError, match="sell commit failure"):
        simulator.simulate(signals, bars, sessions=sessions, event_hook=fail)

    complete = simulator.simulate(signals, bars, sessions=sessions)
    assert [fill.side for fill in complete.fills] == [OrderSide.BUY, OrderSide.SELL]


def test_simulation_rejects_ambiguous_calendar_or_future_signal(settings):
    session = date(2026, 8, 10)
    run = _run(datetime(2026, 8, 11, 14, 40, tzinfo=SHANGHAI_TZ), run_id="future")
    simulator = HistoricalExecutionSimulator(HistoricalSimulationConfig.from_settings(settings))

    with pytest.raises(HistoricalSimulationViolation, match="signal session"):
        simulator.simulate(
            [HistoricalSignal(run=run, candidates=(_candidate(run),))],
            [_bar(session)],
            sessions=[session],
        )

    with pytest.raises(HistoricalSimulationViolation, match="strictly increasing"):
        simulator.simulate([], [], sessions=[session, session])


def test_simulation_validates_signal_and_market_contracts(settings):
    session = date(2026, 8, 10)
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    run = _run(at, run_id="contracts")
    candidate = _candidate(run)
    simulator = HistoricalExecutionSimulator(HistoricalSimulationConfig.from_settings(settings))

    with pytest.raises(HistoricalSimulationViolation, match="timezone-aware"):
        HistoricalSignal(
            run=run.model_copy(update={"as_of": at.replace(tzinfo=None)}),
            candidates=(),
        )
    with pytest.raises(HistoricalSimulationViolation, match="belong"):
        HistoricalSignal(run=run, candidates=(candidate.model_copy(update={"run_id": "other"}),))
    with pytest.raises(HistoricalSimulationViolation, match="unique"):
        HistoricalSignal(run=run, candidates=(candidate, candidate.model_copy(update={"rank": 2})))
    with pytest.raises(HistoricalSimulationViolation, match="information cutoff"):
        HistoricalSignal(
            run=run,
            candidates=(candidate.model_copy(update={"quote_at": at + timedelta(seconds=1)}),),
        )
    with pytest.raises(HistoricalSimulationViolation, match="OHLC"):
        simulator.simulate(
            [],
            [_bar(session, open_price=10.0, high=9.9, low=9.8, close=10.0)],
            sessions=[session],
        )
    with pytest.raises(HistoricalSimulationViolation, match="prices"):
        simulator.simulate([], [_bar(session, low=0)], sessions=[session])
    with pytest.raises(HistoricalSimulationViolation, match="turnover"):
        simulator.simulate([], [_bar(session, volume=-1)], sessions=[session])
    with pytest.raises(HistoricalSimulationViolation, match="unique"):
        simulator.simulate([], [_bar(session), _bar(session)], sessions=[session])
    with pytest.raises(HistoricalSimulationViolation, match="outside"):
        simulator.simulate([], [_bar(date(2026, 8, 11))], sessions=[session])
