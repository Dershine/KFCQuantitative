from __future__ import annotations

import hashlib
import math
from datetime import date, datetime

import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.experiments import (
    CriterionMetric,
    CriterionOperator,
    ExperimentArm,
    ExperimentConclusion,
    ExperimentCriterion,
    ExperimentDataset,
    ExperimentMetricsCalculator,
    ExperimentRecord,
    ExperimentViolation,
    SignalMetricObservation,
)
from kfcquant.historical_simulator import (
    HistoricalEquityPoint,
    HistoricalSimulationFill,
    HistoricalSimulationRejection,
    HistoricalSimulationResult,
)
from kfcquant.models import OrderSide
from tests.conftest import strategy_attribution


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _dataset() -> ExperimentDataset:
    return ExperimentDataset.create(
        input_snapshot_sha256s=(_sha("quotes"), _sha("bars")),
        market_data_sha256=_sha("historical-bars"),
        trading_sessions=(date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)),
    )


def _simulation(equities: tuple[float, ...], *, rejected: bool = False) -> HistoricalSimulationResult:
    sessions = (date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12))
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    fill = HistoricalSimulationFill(
        fill_id="fill-1",
        order_id="order-1",
        run_id="run-1",
        ts_code="600000.SH",
        side=OrderSide.BUY,
        filled_at=at,
        shares=1_000,
        raw_price=10.0,
        fill_price=10.01,
        commission=5.0,
        stamp_duty=0.0,
        slippage=10.0,
        total_cash_change=-10_015.0,
        reason="candidate_entry",
    )
    rejections = (
        HistoricalSimulationRejection(
            run_id="run-1",
            ts_code="000001.SZ",
            side=OrderSide.BUY,
            session=sessions[0],
            reason="missing_bar",
        ),
    ) if rejected else ()
    return HistoricalSimulationResult(
        initial_cash=100_000.0,
        ending_cash=equities[-1],
        fills=(fill,),
        positions=(),
        rejections=rejections,
        equity_curve=tuple(
            HistoricalEquityPoint(
                session=session,
                cash=equity,
                market_value=0.0,
                total_equity=equity,
                marked_positions=0,
                open_positions=0,
                complete=True,
            )
            for session, equity in zip(sessions, equities, strict=True)
        ),
    )


def _observations(candidate_return: float) -> tuple[SignalMetricObservation, ...]:
    return (
        SignalMetricObservation(
            observation_id="low",
            opportunity_score=55,
            realized_return=-0.01,
            max_favorable_excursion=0.01,
            max_adverse_excursion=-0.03,
        ),
        SignalMetricObservation(
            observation_id="middle",
            opportunity_score=72,
            realized_return=candidate_return,
            max_favorable_excursion=0.06,
            max_adverse_excursion=-0.01,
        ),
        SignalMetricObservation(
            observation_id="high",
            opportunity_score=88,
            realized_return=candidate_return + 0.02,
            max_favorable_excursion=0.08,
            max_adverse_excursion=-0.005,
        ),
        SignalMetricObservation(
            observation_id="missing",
            opportunity_score=90,
            evaluable=False,
            issue="missing_future_bar",
        ),
    )


def _arm(version: str, metrics, result_identity: str) -> ExperimentArm:
    attribution = strategy_attribution(version=version, parameters={"threshold": 70 if version == "v1" else 75})
    return ExperimentArm(
        **attribution,
        source_sha="eb5801d9c5b3a7842f4e9194ae71aade67dfea65",
        result_sha256=_sha(result_identity),
        metrics=metrics,
    )


def test_dataset_identity_is_canonical_and_rejects_ambiguous_inputs():
    first = _dataset()
    reordered = ExperimentDataset.create(
        input_snapshot_sha256s=tuple(reversed(first.input_snapshot_sha256s)),
        market_data_sha256=first.market_data_sha256,
        trading_sessions=first.trading_sessions,
    )

    assert first == reordered
    assert first.dataset_id == _dataset().dataset_id
    with pytest.raises(ValueError, match="strictly increasing"):
        ExperimentDataset.create(
            input_snapshot_sha256s=(_sha("bars"),),
            market_data_sha256=_sha("market"),
            trading_sessions=(date(2026, 8, 11), date(2026, 8, 10)),
        )
    with pytest.raises(ValueError, match="SHA-256"):
        ExperimentDataset.create(
            input_snapshot_sha256s=("not-a-hash",),
            market_data_sha256=_sha("market"),
            trading_sessions=(date(2026, 8, 10),),
        )
    with pytest.raises(ValueError, match="at least one"):
        ExperimentDataset.create(
            input_snapshot_sha256s=(),
            market_data_sha256=_sha("market"),
            trading_sessions=(date(2026, 8, 10),),
        )
    with pytest.raises(ValueError, match="canonical"):
        ExperimentDataset(
            dataset_version=first.dataset_version,
            input_snapshot_sha256s=(first.input_snapshot_sha256s[0], first.input_snapshot_sha256s[0]),
            market_data_sha256=first.market_data_sha256,
            trading_sessions=first.trading_sessions,
            dataset_id=first.dataset_id,
        )
    with pytest.raises(ValueError, match="does not match"):
        ExperimentDataset(**{**first.model_dump(), "dataset_id": _sha("tampered")})


@pytest.mark.parametrize(
    "values",
    [
        {"observation_id": "", "realized_return": 0.0, "max_favorable_excursion": 0.0, "max_adverse_excursion": 0.0},
        {
            "observation_id": "bad",
            "realized_return": math.nan,
            "max_favorable_excursion": 0.0,
            "max_adverse_excursion": 0.0,
        },
        {
            "observation_id": "partial",
            "evaluable": False,
            "realized_return": 0.0,
            "max_favorable_excursion": None,
            "max_adverse_excursion": None,
            "issue": "missing",
        },
        {"observation_id": "missing-issue", "evaluable": False},
        {
            "observation_id": "evaluable-with-issue",
            "realized_return": 0.0,
            "max_favorable_excursion": 0.0,
            "max_adverse_excursion": 0.0,
            "issue": "unexpected",
        },
    ],
)
def test_signal_metric_observation_rejects_partial_or_ambiguous_values(values):
    with pytest.raises(ValueError):
        SignalMetricObservation(opportunity_score=50, **values)


def test_metric_calculator_rejects_duplicate_observation_identity():
    observation = _observations(0.02)[0]
    with pytest.raises(ExperimentViolation, match="unique"):
        ExperimentMetricsCalculator.calculate((observation, observation), _simulation((100_000, 100_000, 100_000)))


def test_metrics_cover_stratified_returns_excursions_drawdown_turnover_and_quality():
    metrics = ExperimentMetricsCalculator.calculate(
        _observations(0.03),
        _simulation((100_000.0, 98_000.0, 103_000.0), rejected=True),
    )

    assert metrics.signal.candidate_count == 4
    assert metrics.signal.evaluable_count == 3
    assert metrics.signal.evaluability_rate == pytest.approx(0.75)
    assert [item.band.value for item in metrics.signal.stratified_returns] == ["low", "medium", "high"]
    assert metrics.signal.stratified_returns[-1].mean_return == pytest.approx(0.05)
    assert metrics.signal.mean_mfe == pytest.approx(0.05)
    assert metrics.signal.mean_mae == pytest.approx(-0.015)
    assert metrics.portfolio.total_return == pytest.approx(0.03)
    assert metrics.portfolio.max_drawdown == pytest.approx(0.02)
    assert metrics.portfolio.turnover == pytest.approx(0.1001)
    assert metrics.data_quality.candidate_evaluability_rate == pytest.approx(0.75)
    assert metrics.data_quality.equity_evaluability_rate == 1.0
    assert metrics.data_quality.rejection_counts[0].reason == "missing_bar"


def test_missing_equity_marks_are_explicitly_not_evaluable():
    result = _simulation((100_000.0, 99_000.0, 101_000.0))
    points = list(result.equity_curve)
    points[1] = HistoricalEquityPoint(
        session=points[1].session,
        cash=points[1].cash,
        market_value=None,
        total_equity=None,
        marked_positions=0,
        open_positions=1,
        complete=False,
    )
    incomplete = HistoricalSimulationResult(
        initial_cash=result.initial_cash,
        ending_cash=result.ending_cash,
        fills=result.fills,
        positions=result.positions,
        rejections=result.rejections,
        equity_curve=tuple(points),
    )

    metrics = ExperimentMetricsCalculator.calculate(_observations(0.02), incomplete)

    assert metrics.portfolio.evaluable is False
    assert metrics.portfolio.total_return is None
    assert metrics.portfolio.max_drawdown is None
    assert metrics.data_quality.equity_evaluability_rate == pytest.approx(2 / 3)


def test_experiment_compares_two_arms_on_one_dataset_and_is_hash_stable():
    baseline = _arm(
        "v1",
        ExperimentMetricsCalculator.calculate(_observations(0.01), _simulation((100_000.0, 98_000.0, 102_000.0))),
        "baseline",
    )
    candidate = _arm(
        "v2",
        ExperimentMetricsCalculator.calculate(_observations(0.03), _simulation((100_000.0, 99_000.0, 105_000.0))),
        "candidate",
    )
    criteria = (
        ExperimentCriterion(
            metric=CriterionMetric.PORTFOLIO_TOTAL_RETURN,
            operator=CriterionOperator.MINIMUM_DELTA,
            threshold=0.02,
        ),
        ExperimentCriterion(
            metric=CriterionMetric.PORTFOLIO_MAX_DRAWDOWN,
            operator=CriterionOperator.MAXIMUM_CANDIDATE_VALUE,
            threshold=0.02,
        ),
        ExperimentCriterion(
            metric=CriterionMetric.CANDIDATE_EVALUABILITY,
            operator=CriterionOperator.MINIMUM_CANDIDATE_VALUE,
            threshold=0.75,
        ),
    )
    created_at = datetime(2026, 8, 18, 22, 0, tzinfo=SHANGHAI_TZ)

    record = ExperimentRecord.create(
        experiment_id="m4d-acceptance",
        hypothesis="提高阈值会在不降低可评估率的前提下改善回报与回撤。",
        dataset=_dataset(),
        baseline=baseline,
        candidate=candidate,
        criteria=criteria,
        conclusion_reason="候选满足全部声明标准。",
        created_at=created_at,
    )
    repeated = ExperimentRecord.model_validate_json(record.canonical_json)

    assert record.conclusion == ExperimentConclusion.ACCEPT_CANDIDATE
    assert all(item.passed for item in record.criterion_results)
    assert record.record_sha256 == repeated.record_sha256
    assert record == repeated

    with pytest.raises(ValueError, match="must differ"):
        ExperimentRecord.create(
            experiment_id="same-arm",
            hypothesis="invalid",
            dataset=_dataset(),
            baseline=baseline,
            candidate=baseline,
            criteria=criteria,
            conclusion_reason="invalid",
            created_at=created_at,
        )

    rejected = ExperimentRecord.create(
        experiment_id="m4d-rejected",
        hypothesis="候选必须达到不现实的最低增量。",
        dataset=_dataset(),
        baseline=baseline,
        candidate=candidate,
        criteria=(
            ExperimentCriterion(
                metric=CriterionMetric.PORTFOLIO_TOTAL_RETURN,
                operator=CriterionOperator.MINIMUM_DELTA,
                threshold=0.50,
            ),
            ExperimentCriterion(
                metric=CriterionMetric.PORTFOLIO_TURNOVER,
                operator=CriterionOperator.MAXIMUM_DELTA,
                threshold=0.0,
            ),
        ),
        conclusion_reason="至少一项声明标准失败。",
        created_at=created_at,
    )
    assert rejected.conclusion == ExperimentConclusion.KEEP_BASELINE
    assert rejected.criterion_results[0].passed is False


def test_experiment_models_reject_nonfinite_or_tampered_audit_fields():
    metrics = ExperimentMetricsCalculator.calculate(
        _observations(0.02), _simulation((100_000.0, 99_000.0, 102_000.0))
    )
    with pytest.raises(ValueError, match="finite"):
        ExperimentCriterion(
            metric=CriterionMetric.PORTFOLIO_TOTAL_RETURN,
            operator=CriterionOperator.MINIMUM_DELTA,
            threshold=math.inf,
        )
    attribution = strategy_attribution()
    with pytest.raises(ValueError, match="source_sha"):
        ExperimentArm(
            **attribution,
            source_sha="",
            result_sha256=_sha("result"),
            metrics=metrics,
        )

    baseline = _arm("v1", metrics, "validation-baseline")
    candidate = _arm("v2", metrics, "validation-candidate")
    record = ExperimentRecord.create(
        experiment_id="validation",
        hypothesis="验证不可变记录。",
        dataset=_dataset(),
        baseline=baseline,
        candidate=candidate,
        criteria=(
            ExperimentCriterion(
                metric=CriterionMetric.CANDIDATE_EVALUABILITY,
                operator=CriterionOperator.MINIMUM_CANDIDATE_VALUE,
                threshold=0.5,
            ),
        ),
        conclusion_reason="验证。",
        created_at=datetime(2026, 8, 18, 22, 0, tzinfo=SHANGHAI_TZ),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        ExperimentRecord(**{**record.model_dump(), "created_at": record.created_at.replace(tzinfo=None)})
    with pytest.raises(ValueError, match="must not be blank"):
        ExperimentRecord(**{**record.model_dump(), "hypothesis": ""})
    with pytest.raises(ValueError, match="criterion results"):
        ExperimentRecord(**{**record.model_dump(), "criterion_results": ()})
    with pytest.raises(ValueError, match="at least one"):
        ExperimentRecord(**{**record.model_dump(), "criteria": (), "criterion_results": ()})
    with pytest.raises(ValueError, match="unique"):
        ExperimentRecord(
            **{
                **record.model_dump(),
                "criteria": (*record.criteria, *record.criteria),
                "criterion_results": (*record.criterion_results, *record.criterion_results),
            }
        )
    with pytest.raises(ValueError, match="conclusion"):
        ExperimentRecord(
            **{**record.model_dump(), "conclusion": ExperimentConclusion.KEEP_BASELINE}
        )
    with pytest.raises(ValueError, match="record hash"):
        ExperimentRecord(**{**record.model_dump(), "record_sha256": _sha("tampered")})


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "cash": -1.0,
            "market_value": 0.0,
            "total_equity": -1.0,
            "marked_positions": 0,
            "open_positions": 0,
            "complete": True,
        },
        {
            "market_value": 0.0,
            "total_equity": 100_000.0,
            "marked_positions": 2,
            "open_positions": 1,
            "complete": True,
        },
        {
            "market_value": None,
            "total_equity": None,
            "marked_positions": 1,
            "open_positions": 1,
            "complete": True,
        },
        {
            "market_value": -1.0,
            "total_equity": 99_999.0,
            "marked_positions": 1,
            "open_positions": 1,
            "complete": True,
        },
        {
            "market_value": 0.0,
            "total_equity": -1.0,
            "marked_positions": 1,
            "open_positions": 1,
            "complete": True,
        },
        {
            "market_value": 1.0,
            "total_equity": 100_002.0,
            "marked_positions": 1,
            "open_positions": 1,
            "complete": True,
        },
        {
            "market_value": 1.0,
            "total_equity": 100_001.0,
            "marked_positions": 0,
            "open_positions": 1,
            "complete": False,
        },
    ],
)
def test_equity_points_reject_partial_or_inconsistent_values(kwargs):
    values = dict(kwargs)
    with pytest.raises(ValueError):
        HistoricalEquityPoint(
            session=date(2026, 8, 10),
            cash=values.pop("cash", 100_000.0),
            **values,
        )


def test_unavailable_metric_fails_closed_instead_of_approving_candidate():
    incomplete = ExperimentMetricsCalculator.calculate(
        _observations(0.02),
        HistoricalSimulationResult(
            initial_cash=100_000.0,
            ending_cash=100_000.0,
            fills=(),
            positions=(),
            rejections=(),
            equity_curve=(
                HistoricalEquityPoint(
                    session=date(2026, 8, 10),
                    cash=100_000.0,
                    market_value=None,
                    total_equity=None,
                    marked_positions=0,
                    open_positions=1,
                    complete=False,
                ),
            ),
        ),
    )
    baseline = _arm("v1", incomplete, "baseline-incomplete")
    candidate = _arm("v2", incomplete, "candidate-incomplete")

    with pytest.raises(ExperimentViolation, match="not evaluable"):
        ExperimentRecord.create(
            experiment_id="fail-closed",
            hypothesis="unprovable",
            dataset=_dataset(),
            baseline=baseline,
            candidate=candidate,
            criteria=(
                ExperimentCriterion(
                    metric=CriterionMetric.PORTFOLIO_TOTAL_RETURN,
                    operator=CriterionOperator.MINIMUM_DELTA,
                    threshold=0,
                ),
            ),
            conclusion_reason="must not be accepted",
            created_at=datetime(2026, 8, 18, 22, 0, tzinfo=SHANGHAI_TZ),
        )


def test_experiment_persistence_is_immutable_idempotent_and_atomic(settings):
    database = Database(settings.database_path)
    database.initialize()
    baseline = _arm(
        "v1",
        ExperimentMetricsCalculator.calculate(_observations(0.01), _simulation((100_000.0, 99_000.0, 102_000.0))),
        "persisted-baseline",
    )
    candidate = _arm(
        "v2",
        ExperimentMetricsCalculator.calculate(_observations(0.02), _simulation((100_000.0, 99_500.0, 104_000.0))),
        "persisted-candidate",
    )
    record = ExperimentRecord.create(
        experiment_id="persisted-experiment",
        hypothesis="候选产生更高总回报。",
        dataset=_dataset(),
        baseline=baseline,
        candidate=candidate,
        criteria=(
            ExperimentCriterion(
                metric=CriterionMetric.PORTFOLIO_TOTAL_RETURN,
                operator=CriterionOperator.MINIMUM_DELTA,
                threshold=0.01,
            ),
        ),
        conclusion_reason="总回报增量达到标准。",
        created_at=datetime(2026, 8, 18, 22, 0, tzinfo=SHANGHAI_TZ),
    )

    database.save_experiment(record)
    database.save_experiment(record)
    assert database.get_experiment(record.experiment_id)["record"] == record
    assert len(database.table("experiments")) == 1

    conflicting = ExperimentRecord.create(
        experiment_id=record.experiment_id,
        hypothesis="篡改后的假设。",
        dataset=record.dataset,
        baseline=record.baseline,
        candidate=record.candidate,
        criteria=record.criteria,
        conclusion_reason=record.conclusion_reason,
        created_at=record.created_at,
    )
    with pytest.raises(ValueError, match="immutable"):
        database.save_experiment(conflicting)
    assert database.get_experiment(record.experiment_id)["record"] == record

    atomic = ExperimentRecord.create(
        experiment_id="atomic",
        hypothesis=record.hypothesis,
        dataset=record.dataset,
        baseline=record.baseline,
        candidate=record.candidate,
        criteria=record.criteria,
        conclusion_reason=record.conclusion_reason,
        created_at=record.created_at,
    )
    with pytest.raises(RuntimeError, match="injected"):
        database.save_experiment(
            atomic,
            event_hook=lambda stage: (_ for _ in ()).throw(RuntimeError("injected"))
            if stage == "before_commit"
            else None,
        )
    assert database.get_experiment("atomic") is None
