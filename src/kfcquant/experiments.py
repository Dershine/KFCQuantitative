from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kfcquant.historical_simulator import HistoricalSimulationResult
from kfcquant.models import StrategyAttribution

_SHA256_LENGTH = 64


class ExperimentViolation(ValueError):
    """An experiment cannot prove a deterministic, comparable conclusion."""


def _validate_sha256(value: str, label: str) -> str:
    if len(value) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class ExperimentDataset(BaseModel):
    """Content identity shared by every Strategy arm in one comparison."""

    model_config = ConfigDict(frozen=True)

    dataset_version: str = "experiment-dataset-v1"
    input_snapshot_sha256s: tuple[str, ...]
    market_data_sha256: str
    trading_sessions: tuple[date, ...]
    dataset_id: str

    @classmethod
    def create(
        cls,
        *,
        input_snapshot_sha256s: Sequence[str],
        market_data_sha256: str,
        trading_sessions: Sequence[date],
    ) -> ExperimentDataset:
        snapshots = tuple(sorted(set(input_snapshot_sha256s)))
        sessions = tuple(trading_sessions)
        values = {
            "dataset_version": "experiment-dataset-v1",
            "input_snapshot_sha256s": snapshots,
            "market_data_sha256": market_data_sha256,
            "trading_sessions": sessions,
        }
        serializable = cls.model_construct(**values, dataset_id="").model_dump(
            mode="json", exclude={"dataset_id"}
        )
        return cls(**values, dataset_id=hashlib.sha256(_canonical_json(serializable).encode()).hexdigest())

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if not self.input_snapshot_sha256s:
            raise ValueError("experiment dataset requires at least one input snapshot")
        if tuple(sorted(set(self.input_snapshot_sha256s))) != self.input_snapshot_sha256s:
            raise ValueError("experiment input snapshot hashes must be unique and canonical")
        for value in self.input_snapshot_sha256s:
            _validate_sha256(value, "input snapshot identity")
        _validate_sha256(self.market_data_sha256, "market data identity")
        _validate_sha256(self.dataset_id, "dataset_id")
        if not self.trading_sessions or any(
            current <= previous
            for previous, current in zip(self.trading_sessions, self.trading_sessions[1:], strict=False)
        ):
            raise ValueError("experiment trading sessions must be non-empty and strictly increasing")
        payload = self.model_dump(mode="json", exclude={"dataset_id"})
        expected = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
        if expected != self.dataset_id:
            raise ValueError("dataset_id does not match the canonical experiment dataset")
        return self


class ScoreBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SignalMetricObservation(BaseModel):
    """One time-bounded candidate outcome used by the offline metric calculator."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    opportunity_score: float = Field(ge=0.0, le=100.0)
    realized_return: float | None = None
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    evaluable: bool = True
    issue: str | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if not self.observation_id.strip():
            raise ValueError("signal observation identity must not be blank")
        values = (self.realized_return, self.max_favorable_excursion, self.max_adverse_excursion)
        if self.evaluable:
            if any(value is None or not math.isfinite(value) for value in values):
                raise ValueError("evaluable signal observation requires finite return, MFE and MAE")
            if self.issue is not None:
                raise ValueError("evaluable signal observation cannot contain a data-quality issue")
        else:
            if any(value is not None for value in values):
                raise ValueError("non-evaluable signal observation cannot publish partial metrics")
            if self.issue is None or not self.issue.strip():
                raise ValueError("non-evaluable signal observation requires an issue")
        return self


class ScoreBandMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    band: ScoreBand
    candidate_count: int = Field(ge=0)
    evaluable_count: int = Field(ge=0)
    mean_return: float | None


class SignalEvaluationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_count: int = Field(ge=0)
    evaluable_count: int = Field(ge=0)
    evaluability_rate: float = Field(ge=0.0, le=1.0)
    mean_return: float | None
    mean_mfe: float | None
    mean_mae: float | None
    stratified_returns: tuple[ScoreBandMetrics, ...]


class PortfolioEvaluationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluable: bool
    initial_cash: float = Field(gt=0)
    final_equity: float | None
    total_return: float | None
    max_drawdown: float | None
    turnover: float = Field(ge=0)
    fill_count: int = Field(ge=0)
    closed_position_count: int = Field(ge=0)


class RejectionCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: str
    count: int = Field(gt=0)


class DataQualityMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    expected_candidates: int = Field(ge=0)
    evaluable_candidates: int = Field(ge=0)
    candidate_evaluability_rate: float = Field(ge=0.0, le=1.0)
    expected_equity_sessions: int = Field(ge=0)
    evaluable_equity_sessions: int = Field(ge=0)
    equity_evaluability_rate: float = Field(ge=0.0, le=1.0)
    rejection_counts: tuple[RejectionCount, ...]


class ExperimentMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    metrics_version: str = "experiment-metrics-v1"
    signal: SignalEvaluationMetrics
    portfolio: PortfolioEvaluationMetrics
    data_quality: DataQualityMetrics


class ExperimentMetricsCalculator:
    """Calculate deterministic signal, portfolio and data-quality metrics."""

    @staticmethod
    def _mean(values: Sequence[float]) -> float | None:
        return sum(values) / len(values) if values else None

    @staticmethod
    def _band(score: float) -> ScoreBand:
        if score < 60:
            return ScoreBand.LOW
        if score < 80:
            return ScoreBand.MEDIUM
        return ScoreBand.HIGH

    @classmethod
    def calculate(
        cls,
        observations: Sequence[SignalMetricObservation],
        simulation: HistoricalSimulationResult,
    ) -> ExperimentMetrics:
        normalized = tuple(observations)
        identities = [item.observation_id for item in normalized]
        if len(identities) != len(set(identities)):
            raise ExperimentViolation("signal metric observations must have unique identities")
        evaluable = tuple(item for item in normalized if item.evaluable)
        candidate_rate = len(evaluable) / len(normalized) if normalized else 0.0
        strata: list[ScoreBandMetrics] = []
        for band in ScoreBand:
            candidates = tuple(item for item in normalized if cls._band(item.opportunity_score) == band)
            returns = [item.realized_return for item in candidates if item.evaluable]
            strata.append(
                ScoreBandMetrics(
                    band=band,
                    candidate_count=len(candidates),
                    evaluable_count=len(returns),
                    mean_return=cls._mean([float(value) for value in returns]),
                )
            )
        signal = SignalEvaluationMetrics(
            candidate_count=len(normalized),
            evaluable_count=len(evaluable),
            evaluability_rate=candidate_rate,
            mean_return=cls._mean([float(item.realized_return) for item in evaluable]),
            mean_mfe=cls._mean([float(item.max_favorable_excursion) for item in evaluable]),
            mean_mae=cls._mean([float(item.max_adverse_excursion) for item in evaluable]),
            stratified_returns=tuple(strata),
        )

        equity_points = simulation.equity_curve
        complete_points = tuple(point for point in equity_points if point.complete)
        equity_rate = len(complete_points) / len(equity_points) if equity_points else 0.0
        portfolio_evaluable = bool(equity_points) and len(complete_points) == len(equity_points)
        final_equity: float | None = None
        total_return: float | None = None
        max_drawdown: float | None = None
        if portfolio_evaluable:
            equities = [simulation.initial_cash, *(float(point.total_equity) for point in equity_points)]
            final_equity = equities[-1]
            total_return = final_equity / simulation.initial_cash - 1.0
            peak = equities[0]
            drawdowns: list[float] = []
            for equity in equities:
                peak = max(peak, equity)
                drawdowns.append((peak - equity) / peak if peak else 0.0)
            max_drawdown = max(drawdowns, default=0.0)
        turnover = sum(abs(fill.fill_price * fill.shares) for fill in simulation.fills) / simulation.initial_cash
        portfolio = PortfolioEvaluationMetrics(
            evaluable=portfolio_evaluable,
            initial_cash=simulation.initial_cash,
            final_equity=final_equity,
            total_return=total_return,
            max_drawdown=max_drawdown,
            turnover=turnover,
            fill_count=len(simulation.fills),
            closed_position_count=len(simulation.closed_positions),
        )
        rejection_counts = tuple(
            RejectionCount(reason=reason, count=count)
            for reason, count in sorted(Counter(item.reason for item in simulation.rejections).items())
        )
        quality = DataQualityMetrics(
            expected_candidates=len(normalized),
            evaluable_candidates=len(evaluable),
            candidate_evaluability_rate=candidate_rate,
            expected_equity_sessions=len(equity_points),
            evaluable_equity_sessions=len(complete_points),
            equity_evaluability_rate=equity_rate,
            rejection_counts=rejection_counts,
        )
        return ExperimentMetrics(signal=signal, portfolio=portfolio, data_quality=quality)


class CriterionMetric(StrEnum):
    SIGNAL_MEAN_RETURN = "signal_mean_return"
    SIGNAL_MEAN_MFE = "signal_mean_mfe"
    SIGNAL_MEAN_MAE = "signal_mean_mae"
    PORTFOLIO_TOTAL_RETURN = "portfolio_total_return"
    PORTFOLIO_MAX_DRAWDOWN = "portfolio_max_drawdown"
    PORTFOLIO_TURNOVER = "portfolio_turnover"
    CANDIDATE_EVALUABILITY = "candidate_evaluability"
    EQUITY_EVALUABILITY = "equity_evaluability"


class CriterionOperator(StrEnum):
    MINIMUM_CANDIDATE_VALUE = "minimum_candidate_value"
    MAXIMUM_CANDIDATE_VALUE = "maximum_candidate_value"
    MINIMUM_DELTA = "minimum_delta"
    MAXIMUM_DELTA = "maximum_delta"


class ExperimentCriterion(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: CriterionMetric
    operator: CriterionOperator
    threshold: float

    @model_validator(mode="after")
    def validate_threshold(self) -> Self:
        if not math.isfinite(self.threshold):
            raise ValueError("experiment criterion threshold must be finite")
        return self


class CriterionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: CriterionMetric
    operator: CriterionOperator
    threshold: float
    baseline_value: float
    candidate_value: float
    delta: float
    passed: bool


class ExperimentArm(StrategyAttribution):
    model_config = ConfigDict(frozen=True)

    source_sha: str
    result_sha256: str
    metrics: ExperimentMetrics

    @model_validator(mode="after")
    def validate_arm(self) -> Self:
        if not self.source_sha.strip() or len(self.source_sha) > 128:
            raise ValueError("experiment source_sha must be a non-empty revision identity")
        _validate_sha256(self.result_sha256, "experiment result identity")
        return self


class ExperimentConclusion(StrEnum):
    ACCEPT_CANDIDATE = "accept_candidate"
    KEEP_BASELINE = "keep_baseline"
    INCONCLUSIVE = "inconclusive"


class ExperimentRecord(BaseModel):
    """Immutable, hash-verifiable comparison of two Strategy arms on one dataset."""

    model_config = ConfigDict(frozen=True)

    experiment_version: str = "strategy-experiment-v1"
    experiment_id: str
    hypothesis: str
    dataset: ExperimentDataset
    baseline: ExperimentArm
    candidate: ExperimentArm
    criteria: tuple[ExperimentCriterion, ...]
    criterion_results: tuple[CriterionResult, ...]
    conclusion: ExperimentConclusion
    conclusion_reason: str
    created_at: datetime
    record_sha256: str

    @staticmethod
    def _metric_value(metrics: ExperimentMetrics, metric: CriterionMetric) -> float | None:
        values = {
            CriterionMetric.SIGNAL_MEAN_RETURN: metrics.signal.mean_return,
            CriterionMetric.SIGNAL_MEAN_MFE: metrics.signal.mean_mfe,
            CriterionMetric.SIGNAL_MEAN_MAE: metrics.signal.mean_mae,
            CriterionMetric.PORTFOLIO_TOTAL_RETURN: metrics.portfolio.total_return,
            CriterionMetric.PORTFOLIO_MAX_DRAWDOWN: metrics.portfolio.max_drawdown,
            CriterionMetric.PORTFOLIO_TURNOVER: metrics.portfolio.turnover,
            CriterionMetric.CANDIDATE_EVALUABILITY: metrics.data_quality.candidate_evaluability_rate,
            CriterionMetric.EQUITY_EVALUABILITY: metrics.data_quality.equity_evaluability_rate,
        }
        return values[metric]

    @classmethod
    def _evaluate_criteria(
        cls,
        baseline: ExperimentArm,
        candidate: ExperimentArm,
        criteria: Sequence[ExperimentCriterion],
    ) -> tuple[CriterionResult, ...]:
        results: list[CriterionResult] = []
        for criterion in criteria:
            baseline_value = cls._metric_value(baseline.metrics, criterion.metric)
            candidate_value = cls._metric_value(candidate.metrics, criterion.metric)
            if baseline_value is None or candidate_value is None:
                raise ExperimentViolation(f"criterion metric is not evaluable: {criterion.metric.value}")
            delta = candidate_value - baseline_value
            passed = {
                CriterionOperator.MINIMUM_CANDIDATE_VALUE: candidate_value >= criterion.threshold,
                CriterionOperator.MAXIMUM_CANDIDATE_VALUE: candidate_value <= criterion.threshold,
                CriterionOperator.MINIMUM_DELTA: delta >= criterion.threshold,
                CriterionOperator.MAXIMUM_DELTA: delta <= criterion.threshold,
            }[criterion.operator]
            results.append(
                CriterionResult(
                    metric=criterion.metric,
                    operator=criterion.operator,
                    threshold=criterion.threshold,
                    baseline_value=baseline_value,
                    candidate_value=candidate_value,
                    delta=delta,
                    passed=passed,
                )
            )
        return tuple(results)

    @classmethod
    def create(
        cls,
        *,
        experiment_id: str,
        hypothesis: str,
        dataset: ExperimentDataset,
        baseline: ExperimentArm,
        candidate: ExperimentArm,
        criteria: Sequence[ExperimentCriterion],
        conclusion_reason: str,
        created_at: datetime,
    ) -> ExperimentRecord:
        normalized_criteria = tuple(criteria)
        results = cls._evaluate_criteria(baseline, candidate, normalized_criteria)
        conclusion = (
            ExperimentConclusion.ACCEPT_CANDIDATE
            if results and all(item.passed for item in results)
            else ExperimentConclusion.KEEP_BASELINE
        )
        values: dict[str, object] = {
            "experiment_version": "strategy-experiment-v1",
            "experiment_id": experiment_id,
            "hypothesis": hypothesis,
            "dataset": dataset,
            "baseline": baseline,
            "candidate": candidate,
            "criteria": normalized_criteria,
            "criterion_results": results,
            "conclusion": conclusion,
            "conclusion_reason": conclusion_reason,
            "created_at": created_at,
        }
        serializable = cls.model_construct(**values, record_sha256="").model_dump(
            mode="json", exclude={"record_sha256"}
        )
        values["record_sha256"] = hashlib.sha256(_canonical_json(serializable).encode()).hexdigest()
        return cls(**values)

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if not self.experiment_id.strip() or not self.hypothesis.strip() or not self.conclusion_reason.strip():
            raise ValueError("experiment identity, hypothesis and conclusion reason must not be blank")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("experiment created_at must be timezone-aware")
        baseline_identity = (
            self.baseline.strategy_id,
            self.baseline.strategy_version,
            self.baseline.parameter_hash,
            self.baseline.source_sha,
        )
        candidate_identity = (
            self.candidate.strategy_id,
            self.candidate.strategy_version,
            self.candidate.parameter_hash,
            self.candidate.source_sha,
        )
        if baseline_identity == candidate_identity:
            raise ValueError("baseline and candidate Strategy identities must differ")
        if not self.criteria:
            raise ValueError("experiment requires at least one declared criterion")
        keys = [(item.metric, item.operator) for item in self.criteria]
        if len(keys) != len(set(keys)):
            raise ValueError("experiment criteria must be unique by metric and operator")
        expected_results = self._evaluate_criteria(self.baseline, self.candidate, self.criteria)
        if expected_results != self.criterion_results:
            raise ValueError("experiment criterion results do not match arm metrics")
        expected_conclusion = (
            ExperimentConclusion.ACCEPT_CANDIDATE
            if all(item.passed for item in expected_results)
            else ExperimentConclusion.KEEP_BASELINE
        )
        if self.conclusion != expected_conclusion:
            raise ValueError("experiment conclusion does not match criterion results")
        _validate_sha256(self.record_sha256, "experiment record identity")
        payload = self.model_dump(mode="json", exclude={"record_sha256"})
        expected_hash = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
        if expected_hash != self.record_sha256:
            raise ValueError("experiment record hash does not match its canonical payload")
        return self

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))
