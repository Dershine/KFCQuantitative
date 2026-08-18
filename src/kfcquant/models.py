from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kfcquant.strategy_identity import (
    canonical_parameter_json,
    parameter_hash,
    validate_strategy_identifier,
)


class SourceTier(StrEnum):
    OFFICIAL = "official"
    MAINSTREAM = "mainstream"


class RiskSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EventDirection(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class LLMCallStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class LLMTask(StrEnum):
    RISK_EXTRACTION = "risk_extraction"


class EntityAssociationSource(StrEnum):
    PROVIDER = "provider"
    EXACT_TITLE = "exact_title"
    EXACT_CONTENT = "exact_content"
    LEGACY = "legacy"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PROPOSED = "proposed"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    DEGRADED = "degraded"
    FAILED = "failed"
    MISSED = "missed"


class ResearchRunState(StrEnum):
    CREATED = "created"
    COLLECTING_DATA = "collecting_data"
    EVALUATING = "evaluating"
    STAGED = "staged"
    PUBLISHED = "published"
    EVALUATED = "evaluated"
    DEGRADED = "degraded"
    FAILED = "failed"
    MISSED = "missed"


READABLE_RESEARCH_RUN_STATES = frozenset(
    {
        ResearchRunState.PUBLISHED,
        ResearchRunState.EVALUATED,
        ResearchRunState.DEGRADED,
        ResearchRunState.FAILED,
        ResearchRunState.MISSED,
    }
)

_RESEARCH_RUN_TRANSITIONS: dict[ResearchRunState, frozenset[ResearchRunState]] = {
    ResearchRunState.CREATED: frozenset(
        {ResearchRunState.COLLECTING_DATA, ResearchRunState.FAILED, ResearchRunState.MISSED}
    ),
    ResearchRunState.COLLECTING_DATA: frozenset({ResearchRunState.EVALUATING, ResearchRunState.FAILED}),
    ResearchRunState.EVALUATING: frozenset({ResearchRunState.STAGED, ResearchRunState.FAILED}),
    ResearchRunState.STAGED: frozenset({ResearchRunState.PUBLISHED, ResearchRunState.FAILED}),
    ResearchRunState.PUBLISHED: frozenset({ResearchRunState.EVALUATED, ResearchRunState.DEGRADED}),
    ResearchRunState.EVALUATED: frozenset(),
    ResearchRunState.DEGRADED: frozenset(),
    ResearchRunState.FAILED: frozenset(),
    ResearchRunState.MISSED: frozenset(),
}


class SignalKind(StrEnum):
    MORNING_WATCHLIST = "morning_watchlist"
    PRECLOSE_ENTRY = "preclose_entry"


class EvaluationStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"
    NOT_EVALUABLE = "not_evaluable"


class StrategyAttribution(BaseModel):
    strategy_id: str
    strategy_version: str
    parameter_hash: str
    strategy_parameters: dict[str, Any]

    @model_validator(mode="after")
    def validate_strategy_attribution(self) -> Self:
        validate_strategy_identifier("strategy_id", self.strategy_id)
        validate_strategy_identifier("strategy_version", self.strategy_version)
        canonical = canonical_parameter_json(self.strategy_parameters)
        if parameter_hash(canonical) != self.parameter_hash:
            raise ValueError("parameter_hash does not match strategy_parameters")
        return self


class Security(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ts_code: str
    symbol: str
    name: str
    exchange: str
    market: str | None = None
    list_date: date
    delist_date: date | None = None
    list_status: str = "L"


class DailyBar(BaseModel):
    ts_code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float | None = None
    volume: float
    amount: float
    adj_factor: float = 1.0
    up_limit: float | None = None
    down_limit: float | None = None
    suspended: bool = False
    is_st: bool = False


class LiveQuote(BaseModel):
    ts_code: str
    captured_at: datetime
    price: float
    open: float
    high: float
    low: float
    pre_close: float
    volume: float
    amount: float
    source: str

    @field_validator("captured_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at must include timezone")
        return value


class IntradayBar(BaseModel):
    ts_code: str
    start_at: datetime
    end_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    source: str


class LLMCallTrace(BaseModel):
    call_id: str = Field(default_factory=lambda: str(uuid4()))
    task: LLMTask = LLMTask.RISK_EXTRACTION
    document_id: str
    provider: str
    prompt_version: str
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_model: str
    response_model: str | None = None
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    duration_ms: int = Field(ge=0)
    status: LLMCallStatus
    error_type: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("started_at must include timezone")
        if self.status == LLMCallStatus.SUCCESS:
            if not self.response_model or not self.response_sha256:
                raise ValueError("successful LLM call requires response model and hash")
            if self.error_type or self.error_message:
                raise ValueError("successful LLM call cannot contain failure metadata")
        elif not self.error_type:
            raise ValueError("failed LLM call requires error_type")
        return self


class DocumentEntity(BaseModel):
    document_id: str
    ts_code: str
    relevance: float = Field(ge=0.0, le=1.0)
    association_source: EntityAssociationSource


class RiskEventEntity(BaseModel):
    event_id: str
    ts_code: str
    relevance: float = Field(ge=0.0, le=1.0)
    association_source: EntityAssociationSource


class NewsDocument(BaseModel):
    document_id: str = Field(default_factory=lambda: str(uuid4()))
    ts_code: str | None = None
    title: str
    content: str | None = None
    published_at: datetime
    source: str
    source_tier: SourceTier
    url: str | None = None
    content_hash: str
    fetched_at: datetime
    processing_status: str = "pending"
    processing_error: str | None = None
    entities: list[DocumentEntity] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_entities(self) -> Self:
        if any(entity.document_id != self.document_id for entity in self.entities):
            raise ValueError("document entities must reference their owning document")
        if len({entity.ts_code for entity in self.entities}) != len(self.entities):
            raise ValueError("document entity securities must be unique")
        return self


class RiskEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    document_id: str
    ts_code: str | None = None
    event_type: str
    direction: EventDirection
    severity: RiskSeverity
    confidence: float = Field(ge=0.0, le=1.0)
    hard_block: bool = False
    evidence: str = Field(min_length=1, max_length=500)
    source_url: str | None = None
    published_at: datetime
    extracted_at: datetime
    model_name: str
    llm_call_id: str | None = None
    entities: list[RiskEventEntity] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_entities(self) -> Self:
        if any(entity.event_id != self.event_id for entity in self.entities):
            raise ValueError("risk event entities must reference their owning event")
        if len({entity.ts_code for entity in self.entities}) != len(self.entities):
            raise ValueError("risk event entity securities must be unique")
        return self


class RiskExtractionResult(BaseModel):
    events: list[RiskEvent] = Field(default_factory=list)
    trace: LLMCallTrace | None = None

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self):
        return iter(self.events)

    def __getitem__(self, index: int) -> RiskEvent:
        return self.events[index]


class FactorBreakdown(BaseModel):
    ret_1d: float = 0.0
    ret_5d: float = 0.0
    ret_20d: float = 0.0
    intraday_strength: float = 0.0
    close_location: float = 0.0
    projected_volume_ratio: float = 0.0
    median_amount_20d: float = 0.0
    volatility_20d: float = 0.0
    gap_abs: float = 0.0
    limit_proximity: float = 0.0
    technical_score: float = 0.0
    news_score: float = 0.0
    continuity_score: float = 0.0
    positive_score: float = 0.0
    risk_penalty: float = 0.0
    morning_status: str = "not_applicable"


class CandidateScore(BaseModel):
    run_id: str
    ts_code: str
    name: str
    rank: int
    opportunity_score: float = Field(ge=0.0, le=100.0)
    factor_breakdown: FactorBreakdown
    risk_event_ids: list[str] = Field(default_factory=list)
    blocked: bool = False
    block_reasons: list[str] = Field(default_factory=list)
    quote_at: datetime


class SignalRun(StrategyAttribution):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    as_of: datetime
    signal_kind: SignalKind = SignalKind.PRECLOSE_ENTRY
    information_cutoff: datetime | None = None
    data_as_of: datetime | None = None
    status: RunStatus
    lifecycle_state: ResearchRunState | None = None
    data_fresh: bool
    official_news_healthy: bool
    mainstream_news_healthy: bool
    tradable: bool
    message: str = ""
    candidate_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def infer_legacy_lifecycle_state(self) -> Self:
        if self.lifecycle_state is not None:
            return self
        inferred = {
            RunStatus.SUCCESS: ResearchRunState.PUBLISHED,
            RunStatus.DEGRADED: ResearchRunState.PUBLISHED,
            RunStatus.FAILED: ResearchRunState.FAILED,
            RunStatus.MISSED: ResearchRunState.MISSED,
            RunStatus.RUNNING: ResearchRunState.EVALUATING,
        }[self.status]
        self.lifecycle_state = inferred
        return self

    def transition_to(self, target: ResearchRunState) -> Self:
        current = self.lifecycle_state
        if current is None:
            raise ValueError("research run lifecycle state was not initialized")
        if target == current:
            return self
        if target not in _RESEARCH_RUN_TRANSITIONS[current]:
            raise ValueError(f"illegal research run transition: {current.value} -> {target.value}")
        updates: dict[str, object] = {"lifecycle_state": target}
        if target == ResearchRunState.FAILED:
            updates.update(status=RunStatus.FAILED, tradable=False)
        elif target == ResearchRunState.MISSED:
            updates.update(status=RunStatus.MISSED, tradable=False)
        elif target == ResearchRunState.DEGRADED:
            updates.update(status=RunStatus.DEGRADED, tradable=False)
        return self.model_copy(update=updates)


class CandidateOutcome(StrategyAttribution):
    outcome_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    ts_code: str
    signal_kind: SignalKind
    status: EvaluationStatus
    baseline_at: datetime | None = None
    baseline_price: float | None = None
    target_price: float | None = None
    hit_at: datetime | None = None
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    reason: str = ""
    evaluated_at: datetime


class PaperOrder(StrategyAttribution):
    order_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    ts_code: str
    side: OrderSide
    status: OrderStatus = OrderStatus.PROPOSED
    created_at: datetime
    target_value: float
    reason: str
    position_id: str | None = None


class PaperFill(BaseModel):
    fill_id: str = Field(default_factory=lambda: str(uuid4()))
    order_id: str
    ts_code: str
    side: OrderSide
    filled_at: datetime
    shares: int
    raw_price: float
    fill_price: float
    commission: float
    stamp_duty: float
    slippage: float
    total_cash_change: float


class PaperPosition(StrategyAttribution):
    position_id: str = Field(default_factory=lambda: str(uuid4()))
    ts_code: str
    opened_at: datetime
    opened_trade_date: date
    shares: int
    entry_price: float
    cost_basis: float
    entry_fees: float
    status: str = "open"
    closed_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    realized_pnl: float | None = None


class OpportunityOutcome(StrategyAttribution):
    outcome_id: str = Field(default_factory=lambda: str(uuid4()))
    position_id: str
    ts_code: str
    entry_date: date
    first_day_hit: bool
    five_day_hit: bool
    holding_days: int
    net_return: float
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    recorded_at: datetime
