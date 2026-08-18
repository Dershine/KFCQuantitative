from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from kfcquant.config import Settings
from kfcquant.models import CandidateScore, DailyBar, OrderSide, SignalKind, SignalRun


class HistoricalSimulationViolation(ValueError):
    """Historical inputs or configuration cannot prove a safe deterministic result."""


@dataclass(frozen=True, slots=True)
class HistoricalSimulationConfig:
    """Explicit execution assumptions for the isolated historical account."""

    initial_cash: float = 100_000.0
    max_positions: int = 5
    position_fraction: float = 0.20
    lot_size: int = 100
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.0005
    take_profit_net: float = 0.015
    stop_loss_net: float = 0.02
    max_holding_sessions: int = 5
    settlement_delay_sessions: Literal[1] = 1
    limit_distance_fraction: float = 0.01
    reject_suspended: bool = True
    require_positive_turnover: bool = True
    reject_at_price_limit: bool = True

    def __post_init__(self) -> None:
        finite_nonnegative = {
            "initial_cash": self.initial_cash,
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_duty_rate": self.stamp_duty_rate,
            "slippage_rate": self.slippage_rate,
            "limit_distance_fraction": self.limit_distance_fraction,
        }
        for label, value in finite_nonnegative.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be finite and non-negative")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.max_positions < 1 or self.lot_size < 1 or self.max_holding_sessions < 1:
            raise ValueError("position, lot and holding limits must be positive")
        if not math.isfinite(self.position_fraction) or not 0 < self.position_fraction <= 1:
            raise ValueError("position_fraction must be in (0, 1]")
        if self.max_positions * self.position_fraction > 1.0 + 1e-12:
            raise ValueError("max_positions * position_fraction must not exceed initial cash")
        if self.settlement_delay_sessions != 1:
            raise ValueError("historical execution must enforce A-share T+1 settlement")
        for label, value in {
            "commission_rate": self.commission_rate,
            "stamp_duty_rate": self.stamp_duty_rate,
            "slippage_rate": self.slippage_rate,
            "limit_distance_fraction": self.limit_distance_fraction,
            "take_profit_net": self.take_profit_net,
            "stop_loss_net": self.stop_loss_net,
        }.items():
            if not math.isfinite(value) or not 0 <= value < 1:
                raise ValueError(f"{label} must be finite and in [0, 1)")

    @classmethod
    def from_settings(cls, settings: Settings) -> HistoricalSimulationConfig:
        return cls(
            initial_cash=settings.initial_cash,
            max_positions=settings.max_positions,
            position_fraction=settings.position_fraction,
            lot_size=settings.lot_size,
            commission_rate=settings.commission_rate,
            min_commission=settings.min_commission,
            stamp_duty_rate=settings.stamp_duty_rate,
            slippage_rate=settings.slippage_rate,
            take_profit_net=settings.take_profit_net,
            stop_loss_net=settings.stop_loss_net,
            max_holding_sessions=settings.max_holding_days,
            settlement_delay_sessions=1,
            limit_distance_fraction=settings.limit_distance_fraction,
        )


@dataclass(frozen=True, slots=True)
class HistoricalSignal:
    run: SignalRun
    candidates: tuple[CandidateScore, ...]

    def __post_init__(self) -> None:
        if self.run.as_of.tzinfo is None or self.run.as_of.utcoffset() is None:
            raise HistoricalSimulationViolation("Signal Run as_of must be timezone-aware")
        if any(candidate.run_id != self.run.run_id for candidate in self.candidates):
            raise HistoricalSimulationViolation("all candidates must belong to their Signal Run")
        codes = [candidate.ts_code for candidate in self.candidates]
        if len(codes) != len(set(codes)):
            raise HistoricalSimulationViolation("candidate securities must be unique within a Signal Run")
        cutoff = self.run.information_cutoff or self.run.as_of
        if any(
            candidate.quote_at.tzinfo is None
            or candidate.quote_at.utcoffset() is None
            or candidate.quote_at > cutoff
            for candidate in self.candidates
        ):
            raise HistoricalSimulationViolation("candidate quote must not be after the information cutoff")


@dataclass(frozen=True, slots=True)
class HistoricalSimulationFill:
    fill_id: str
    order_id: str
    run_id: str
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
    reason: str


@dataclass(frozen=True, slots=True)
class HistoricalSimulationPosition:
    position_id: str
    run_id: str
    strategy_id: str
    strategy_version: str
    parameter_hash: str
    strategy_parameters: dict[str, object]
    ts_code: str
    opened_at: datetime
    opened_session_index: int
    shares: int
    entry_price: float
    cost_basis: float
    entry_fees: float
    closed_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    realized_pnl: float | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True, slots=True)
class HistoricalSimulationRejection:
    run_id: str
    ts_code: str
    side: OrderSide
    session: date
    reason: str


@dataclass(frozen=True, slots=True)
class HistoricalSimulationResult:
    initial_cash: float
    ending_cash: float
    fills: tuple[HistoricalSimulationFill, ...]
    positions: tuple[HistoricalSimulationPosition, ...]
    rejections: tuple[HistoricalSimulationRejection, ...]

    @property
    def open_positions(self) -> tuple[HistoricalSimulationPosition, ...]:
        return tuple(position for position in self.positions if position.is_open)

    @property
    def closed_positions(self) -> tuple[HistoricalSimulationPosition, ...]:
        return tuple(position for position in self.positions if not position.is_open)


EventHook = Callable[[str], None]


class HistoricalExecutionSimulator:
    """Pure historical portfolio execution, isolated from live providers and DuckDB."""

    def __init__(self, config: HistoricalSimulationConfig):
        self.config = config

    @staticmethod
    def _stable_id(kind: str, *parts: object) -> str:
        identity = "|".join(["kfcquant-historical", kind, *(str(part) for part in parts)])
        return str(uuid5(NAMESPACE_URL, identity))

    @staticmethod
    def _emit(event_hook: EventHook | None, stage: str) -> None:
        if event_hook is not None:
            event_hook(stage)

    @staticmethod
    def _exit_at(session: date, timezone) -> datetime:
        return datetime.combine(session, time(15, 0), tzinfo=timezone)

    def _commission(self, notional: float) -> float:
        return max(notional * self.config.commission_rate, self.config.min_commission)

    def _trigger_prices(self, position: HistoricalSimulationPosition) -> tuple[float, float]:
        sell_cost_rate = (
            self.config.commission_rate
            + self.config.stamp_duty_rate
            + self.config.slippage_rate
        )
        target = position.cost_basis * (1.0 + self.config.take_profit_net + sell_cost_rate)
        stop = position.cost_basis * (1.0 - self.config.stop_loss_net + sell_cost_rate)
        return round(target, 2), round(stop, 2)

    def _market_rejection(
        self,
        bar: DailyBar,
        side: OrderSide,
        raw_price: float,
    ) -> str | None:
        if self.config.reject_suspended and bar.suspended:
            return "suspended"
        if self.config.require_positive_turnover and (bar.volume <= 0 or bar.amount <= 0):
            return "no_turnover"
        if self.config.reject_at_price_limit:
            if (
                side == OrderSide.BUY
                and bar.up_limit is not None
                and raw_price >= bar.up_limit * (1 - self.config.limit_distance_fraction)
            ):
                return "buy_at_price_limit"
            if (
                side == OrderSide.SELL
                and bar.down_limit is not None
                and raw_price <= bar.down_limit * (1 + self.config.limit_distance_fraction)
            ):
                return "sell_at_price_limit"
        return None

    @staticmethod
    def _validate_market_bar(bar: DailyBar) -> None:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            raise HistoricalSimulationViolation("historical bar prices must be finite and positive")
        if bar.high < max(bar.open, bar.low, bar.close) or bar.low > min(bar.open, bar.high, bar.close):
            raise HistoricalSimulationViolation("historical bar OHLC relationship is invalid")
        if not math.isfinite(bar.volume) or not math.isfinite(bar.amount):
            raise HistoricalSimulationViolation("historical bar turnover must be finite")
        if bar.volume < 0 or bar.amount < 0:
            raise HistoricalSimulationViolation("historical bar turnover must be non-negative")

    @classmethod
    def _validate_inputs(
        cls,
        signals: Sequence[HistoricalSignal],
        bars: Sequence[DailyBar],
        sessions: Sequence[date],
    ) -> tuple[tuple[date, ...], dict[tuple[date, str], DailyBar]]:
        normalized_sessions = tuple(sessions)
        if not normalized_sessions or any(
            current <= previous
            for previous, current in zip(normalized_sessions, normalized_sessions[1:], strict=False)
        ):
            raise HistoricalSimulationViolation("trading sessions must be non-empty and strictly increasing")
        session_set = set(normalized_sessions)
        signal_dates = {signal.run.as_of.date() for signal in signals}
        if not signal_dates <= session_set:
            raise HistoricalSimulationViolation("every signal session must exist in the trading calendar")
        indexed: dict[tuple[date, str], DailyBar] = {}
        for bar in bars:
            cls._validate_market_bar(bar)
            if bar.trade_date not in session_set:
                raise HistoricalSimulationViolation("historical bar session is outside the trading calendar")
            key = (bar.trade_date, bar.ts_code)
            if key in indexed:
                raise HistoricalSimulationViolation("historical bars must be unique by session and security")
            indexed[key] = bar
        return normalized_sessions, indexed

    def _buy_fill(
        self,
        *,
        run: SignalRun,
        candidate: CandidateScore,
        bar: DailyBar,
        session_index: int,
        cash: float,
    ) -> tuple[HistoricalSimulationFill, HistoricalSimulationPosition] | None:
        raw_price = bar.close
        target_value = min(self.config.initial_cash * self.config.position_fraction, cash)
        estimated_price = raw_price * (1 + self.config.slippage_rate)
        shares = math.floor(target_value / estimated_price / self.config.lot_size) * self.config.lot_size
        while shares >= self.config.lot_size:
            fill_price = round(raw_price * (1.0 + self.config.slippage_rate) + 1e-10, 2)
            notional = fill_price * shares
            commission = self._commission(notional)
            total = notional + commission
            if total <= cash + 1e-9:
                break
            shares -= self.config.lot_size
        if shares < self.config.lot_size:
            return None
        filled_at = run.as_of
        order_id = self._stable_id("buy-order", run.run_id, candidate.ts_code)
        fill = HistoricalSimulationFill(
            fill_id=self._stable_id("buy-fill", order_id),
            order_id=order_id,
            run_id=run.run_id,
            ts_code=candidate.ts_code,
            side=OrderSide.BUY,
            filled_at=filled_at,
            shares=shares,
            raw_price=raw_price,
            fill_price=fill_price,
            commission=commission,
            stamp_duty=0.0,
            slippage=max(fill_price - raw_price, 0.0) * shares,
            total_cash_change=-total,
            reason="candidate_entry",
        )
        position = HistoricalSimulationPosition(
            position_id=self._stable_id("position", order_id),
            run_id=run.run_id,
            strategy_id=run.strategy_id,
            strategy_version=run.strategy_version,
            parameter_hash=run.parameter_hash,
            strategy_parameters=dict(run.strategy_parameters),
            ts_code=candidate.ts_code,
            opened_at=filled_at,
            opened_session_index=session_index,
            shares=shares,
            entry_price=fill_price,
            cost_basis=total / shares,
            entry_fees=commission,
        )
        return fill, position

    def _sell_fill(
        self,
        position: HistoricalSimulationPosition,
        bar: DailyBar,
        raw_price: float,
        reason: str,
    ) -> tuple[HistoricalSimulationFill, HistoricalSimulationPosition]:
        filled_at = self._exit_at(bar.trade_date, position.opened_at.tzinfo)
        order_id = self._stable_id(
            "sell-order",
            position.position_id,
            bar.trade_date.isoformat(),
            reason,
        )
        fill_price = round(raw_price * (1.0 - self.config.slippage_rate) + 1e-10, 2)
        notional = fill_price * position.shares
        commission = self._commission(notional)
        stamp_duty = notional * self.config.stamp_duty_rate
        proceeds = notional - commission - stamp_duty
        fill = HistoricalSimulationFill(
            fill_id=self._stable_id("sell-fill", order_id),
            order_id=order_id,
            run_id=position.run_id,
            ts_code=position.ts_code,
            side=OrderSide.SELL,
            filled_at=filled_at,
            shares=position.shares,
            raw_price=raw_price,
            fill_price=fill_price,
            commission=commission,
            stamp_duty=stamp_duty,
            slippage=max(raw_price - fill_price, 0.0) * position.shares,
            total_cash_change=proceeds,
            reason=reason,
        )
        closed = replace(
            position,
            closed_at=filled_at,
            exit_price=fill_price,
            exit_reason=reason,
            realized_pnl=proceeds - position.cost_basis * position.shares,
        )
        return fill, closed

    def simulate(
        self,
        signals: Sequence[HistoricalSignal],
        bars: Sequence[DailyBar],
        *,
        sessions: Sequence[date],
        event_hook: EventHook | None = None,
    ) -> HistoricalSimulationResult:
        """Return one complete result; exceptions publish no partial account state."""
        normalized_sessions, bar_map = self._validate_inputs(signals, bars, sessions)
        signals_by_date: dict[date, list[HistoricalSignal]] = {}
        for signal in sorted(signals, key=lambda item: (item.run.as_of, item.run.run_id)):
            signals_by_date.setdefault(signal.run.as_of.date(), []).append(signal)

        cash = self.config.initial_cash
        positions: dict[str, HistoricalSimulationPosition] = {}
        fills: list[HistoricalSimulationFill] = []
        rejections: list[HistoricalSimulationRejection] = []
        self._emit(event_hook, "after_input_validation")

        for session_index, session in enumerate(normalized_sessions):
            for position_id, position in sorted(positions.items()):
                if not position.is_open:
                    continue
                held_sessions = session_index - position.opened_session_index
                if held_sessions < self.config.settlement_delay_sessions:
                    continue
                bar = bar_map.get((session, position.ts_code))
                if bar is None:
                    continue
                target, stop = self._trigger_prices(position)
                reason: str | None = None
                raw_price: float | None = None
                if bar.low <= stop:
                    reason = "stop_loss"
                    raw_price = min(bar.open, stop)
                elif bar.high >= target:
                    reason = "take_profit"
                    raw_price = max(bar.open, target)
                elif held_sessions >= self.config.max_holding_sessions:
                    reason = "max_holding_sessions"
                    raw_price = bar.close
                if reason is None or raw_price is None:
                    continue
                rejection = self._market_rejection(bar, OrderSide.SELL, raw_price)
                if rejection:
                    rejections.append(
                        HistoricalSimulationRejection(
                            run_id=position.run_id,
                            ts_code=position.ts_code,
                            side=OrderSide.SELL,
                            session=session,
                            reason=rejection,
                        )
                    )
                    continue
                fill, closed = self._sell_fill(position, bar, raw_price, reason)
                draft_cash = cash + fill.total_cash_change
                if draft_cash < -1e-8:
                    raise HistoricalSimulationViolation("sell execution produced negative cash")
                self._emit(event_hook, "before_sell_commit")
                cash = draft_cash
                fills.append(fill)
                positions[position_id] = closed

            for signal in signals_by_date.get(session, []):
                candidates = sorted(signal.candidates, key=lambda item: (item.rank, item.ts_code))
                for candidate in candidates:
                    if signal.run.signal_kind != SignalKind.PRECLOSE_ENTRY:
                        rejection = "signal_not_buyable"
                    elif not signal.run.tradable:
                        rejection = "run_not_tradable"
                    elif candidate.blocked:
                        rejection = "candidate_blocked"
                    elif any(
                        position.is_open and position.ts_code == candidate.ts_code
                        for position in positions.values()
                    ):
                        rejection = "position_already_open"
                    elif sum(position.is_open for position in positions.values()) >= self.config.max_positions:
                        rejection = "portfolio_full"
                    else:
                        bar = bar_map.get((session, candidate.ts_code))
                        if bar is None:
                            rejection = "missing_bar"
                        else:
                            rejection = self._market_rejection(bar, OrderSide.BUY, bar.close)
                    if rejection:
                        rejections.append(
                            HistoricalSimulationRejection(
                                run_id=signal.run.run_id,
                                ts_code=candidate.ts_code,
                                side=OrderSide.BUY,
                                session=session,
                                reason=rejection,
                            )
                        )
                        continue
                    execution = self._buy_fill(
                        run=signal.run,
                        candidate=candidate,
                        bar=bar,
                        session_index=session_index,
                        cash=cash,
                    )
                    if execution is None:
                        rejections.append(
                            HistoricalSimulationRejection(
                                run_id=signal.run.run_id,
                                ts_code=candidate.ts_code,
                                side=OrderSide.BUY,
                                session=session,
                                reason="insufficient_cash",
                            )
                        )
                        continue
                    fill, position = execution
                    draft_cash = cash + fill.total_cash_change
                    if draft_cash < -1e-8:
                        raise HistoricalSimulationViolation("buy execution produced negative cash")
                    self._emit(event_hook, "before_buy_commit")
                    cash = draft_cash
                    fills.append(fill)
                    positions[position.position_id] = position

        expected_cash = self.config.initial_cash + sum(fill.total_cash_change for fill in fills)
        if not math.isclose(cash, expected_cash, rel_tol=0.0, abs_tol=1e-8) or cash < -1e-8:
            raise HistoricalSimulationViolation("historical cash ledger is inconsistent")
        result = HistoricalSimulationResult(
            initial_cash=self.config.initial_cash,
            ending_cash=cash,
            fills=tuple(fills),
            positions=tuple(sorted(positions.values(), key=lambda item: (item.opened_at, item.ts_code))),
            rejections=tuple(rejections),
        )
        self._emit(event_hook, "before_result_publish")
        return result
