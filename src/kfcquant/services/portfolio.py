from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from kfcquant.config import Settings
from kfcquant.db import Database
from kfcquant.interfaces import LiveQuoteProvider
from kfcquant.models import (
    CandidateScore,
    OpportunityOutcome,
    OrderSide,
    PaperFill,
    PaperOrder,
    PaperPosition,
    SignalKind,
    SignalRun,
)


class FeeModel:
    def __init__(self, settings: Settings):
        self.settings = settings

    def commission(self, notional: float) -> float:
        return max(notional * self.settings.commission_rate, self.settings.min_commission)

    def buy_fill(
        self, raw_price: float, shares: int, at: datetime, order: PaperOrder
    ) -> tuple[PaperFill, PaperPosition]:
        fill_price = round(raw_price * (1.0 + self.settings.slippage_rate) + 1e-10, 2)
        notional = fill_price * shares
        commission = self.commission(notional)
        total = notional + commission
        fill = PaperFill(
            order_id=order.order_id,
            ts_code=order.ts_code,
            side=OrderSide.BUY,
            filled_at=at,
            shares=shares,
            raw_price=raw_price,
            fill_price=fill_price,
            commission=commission,
            stamp_duty=0.0,
            slippage=max(fill_price - raw_price, 0.0) * shares,
            total_cash_change=-total,
        )
        position = PaperPosition(
            strategy_id=order.strategy_id,
            strategy_version=order.strategy_version,
            parameter_hash=order.parameter_hash,
            strategy_parameters=order.strategy_parameters,
            ts_code=order.ts_code,
            opened_at=at,
            opened_trade_date=at.date(),
            shares=shares,
            entry_price=fill_price,
            cost_basis=total / shares,
            entry_fees=commission,
        )
        return fill, position

    def sell_fill(
        self,
        raw_price: float,
        shares: int,
        at: datetime,
        order: PaperOrder,
    ) -> PaperFill:
        fill_price = round(raw_price * (1.0 - self.settings.slippage_rate) + 1e-10, 2)
        notional = fill_price * shares
        commission = self.commission(notional)
        stamp = notional * self.settings.stamp_duty_rate
        proceeds = notional - commission - stamp
        return PaperFill(
            order_id=order.order_id,
            ts_code=order.ts_code,
            side=OrderSide.SELL,
            filled_at=at,
            shares=shares,
            raw_price=raw_price,
            fill_price=fill_price,
            commission=commission,
            stamp_duty=stamp,
            slippage=max(raw_price - fill_price, 0.0) * shares,
            total_cash_change=proceeds,
        )

    def trigger_prices(self, position: PaperPosition) -> tuple[float, float]:
        sell_cost_rate = self.settings.commission_rate + self.settings.stamp_duty_rate + self.settings.slippage_rate
        target = position.cost_basis * (1.0 + self.settings.take_profit_net + sell_cost_rate)
        stop = position.cost_basis * (1.0 - self.settings.stop_loss_net + sell_cost_rate)
        return round(target, 2), round(stop, 2)


class PortfolioService:
    def __init__(self, database: Database, settings: Settings, live_provider: LiveQuoteProvider):
        self.database = database
        self.settings = settings
        self.live_provider = live_provider
        self.fees = FeeModel(settings)

    def plan_candidate_orders(self, run: SignalRun, candidates: list[CandidateScore]) -> list[PaperOrder]:
        if not run.tradable:
            return []
        positions = self.database.get_open_positions()
        held = set(positions["ts_code"].astype(str)) if not positions.empty else set()
        available_slots = max(self.settings.max_positions - len(held), 0)
        if available_slots <= 0:
            return []
        target_value = self.settings.initial_cash * self.settings.position_fraction
        planned: list[PaperOrder] = []
        # Reserve orders are bounded by the shared selection policy; fills still stop when slots are full.
        for candidate in self.settings.selection.select_candidates(candidates):
            if candidate.ts_code in held:
                continue
            order = PaperOrder(
                strategy_id=run.strategy_id,
                strategy_version=run.strategy_version,
                parameter_hash=run.parameter_hash,
                strategy_parameters=run.strategy_parameters,
                run_id=run.run_id,
                ts_code=candidate.ts_code,
                side=OrderSide.BUY,
                created_at=run.as_of,
                target_value=target_value,
                reason=(
                    f"{self.settings.schedule.preclose_run_at.strftime('%H:%M')}机会评分 "
                    f"{candidate.opportunity_score:.2f}，排名 {candidate.rank}"
                ),
            )
            planned.append(order)
        return planned

    def create_candidate_orders(self, run: SignalRun, candidates: list[CandidateScore]) -> list[PaperOrder]:
        created: list[PaperOrder] = []
        for order in self.plan_candidate_orders(run, candidates):
            if self.database.save_order(order):
                created.append(order)
        return created

    def capture_buy_fills(self, run_id: str, at: datetime, quotes: pd.DataFrame) -> list[PaperFill]:
        orders = self.database.proposed_orders(run_id)
        if orders.empty:
            return []
        candidates = self.database.get_candidates(run_id, include_blocked=False)[["ts_code", "rank"]]
        orders = orders.merge(candidates, on="ts_code", how="left").sort_values(["rank", "created_at"])
        current_map = quotes.set_index("ts_code").to_dict("index")
        fills: list[PaperFill] = []
        for order_row in orders.to_dict("records"):
            open_positions = self.database.get_open_positions()
            if len(open_positions) >= self.settings.max_positions:
                self.database.reject_order(str(order_row["order_id"]), "组合已满")
                continue
            code = str(order_row["ts_code"])
            if not open_positions.empty and code in set(open_positions["ts_code"].astype(str)):
                self.database.reject_order(str(order_row["order_id"]), "已有持仓，V1禁止加仓")
                continue
            current = current_map.get(code)
            if current is None:
                self.database.reject_order(
                    str(order_row["order_id"]),
                    f"{self.settings.schedule.fill_at.strftime('%H:%M')}无实时行情",
                )
                continue
            signal_quote = self.database.get_quote_near(code, pd.Timestamp(order_row["created_at"]).to_pydatetime())
            if signal_quote is None:
                self.database.reject_order(
                    str(order_row["order_id"]),
                    f"缺少{self.settings.schedule.preclose_run_at.strftime('%H:%M')}基准快照",
                )
                continue
            delta_volume = float(current["volume"]) - float(signal_quote["volume"])
            delta_amount = float(current["amount"]) - float(signal_quote["amount"])
            if delta_volume <= 0 or delta_amount <= 0:
                self.database.reject_order(
                    str(order_row["order_id"]),
                    f"{self.settings.schedule.preclose_run_at.strftime('%H:%M')}至"
                    f"{self.settings.schedule.fill_at.strftime('%H:%M')}无可验证成交",
                )
                continue
            raw_vwap = delta_amount / delta_volume
            pre_close = float(current["pre_close"])
            theoretical_up = round(pre_close * 1.10 + 1e-8, 2)
            if float(current["price"]) >= theoretical_up * (1 - self.settings.limit_distance_fraction):
                self.database.reject_order(str(order_row["order_id"]), "接近涨停，视为无法合理成交")
                continue
            target_value = min(float(order_row["target_value"]), self.database.get_cash())
            estimated_price = raw_vwap * (1 + self.settings.slippage_rate)
            shares = math.floor(target_value / estimated_price / self.settings.lot_size) * self.settings.lot_size
            order = PaperOrder.model_validate(order_row)
            if shares < self.settings.lot_size:
                self.database.reject_order(order.order_id, "资金不足一手")
                continue
            fill, position = self.fees.buy_fill(raw_vwap, shares, at, order)
            if -fill.total_cash_change > self.database.get_cash():
                shares -= self.settings.lot_size
                if shares < self.settings.lot_size:
                    self.database.reject_order(order.order_id, "计入费用后资金不足")
                    continue
                fill, position = self.fees.buy_fill(raw_vwap, shares, at, order)
            self.database.apply_buy_fill(fill, position)
            fills.append(fill)
        return fills

    def _close_position(self, position: PaperPosition, raw_price: float, at: datetime, reason: str) -> PaperFill:
        run_id = f"monitor-{at.strftime('%Y%m%d-%H%M')}"
        order = PaperOrder(
            strategy_id=position.strategy_id,
            strategy_version=position.strategy_version,
            parameter_hash=position.parameter_hash,
            strategy_parameters=position.strategy_parameters,
            run_id=run_id,
            ts_code=position.ts_code,
            side=OrderSide.SELL,
            created_at=at,
            target_value=raw_price * position.shares,
            reason=reason,
            position_id=position.position_id,
        )
        self.database.create_sell_order_if_absent(order)
        # If a repeated monitor already created an order, retrieve the original id.
        pending = self.database.proposed_orders()
        matched = pending[
            (pending["run_id"] == run_id) & (pending["ts_code"] == position.ts_code) & (pending["side"] == "sell")
        ]
        if not matched.empty:
            order = PaperOrder.model_validate(matched.iloc[0].to_dict())
        fill = self.fees.sell_fill(raw_price, position.shares, at, order)
        closed = self.database.apply_sell_fill(fill, position.position_id, reason)
        holding_days = self.database.count_trading_days(position.opened_trade_date, at.date())
        net_return = float(closed.realized_pnl or 0.0) / max(position.cost_basis * position.shares, 1.0)
        hit = reason == "take_profit"
        self.database.save_outcome(
            OpportunityOutcome(
                strategy_id=position.strategy_id,
                strategy_version=position.strategy_version,
                parameter_hash=position.parameter_hash,
                strategy_parameters=position.strategy_parameters,
                position_id=position.position_id,
                ts_code=position.ts_code,
                entry_date=position.opened_trade_date,
                first_day_hit=hit and holding_days == 2,
                five_day_hit=hit and holding_days <= self.settings.max_holding_days,
                holding_days=holding_days,
                net_return=net_return,
                recorded_at=at,
            )
        )
        return fill

    def monitor_positions(self, at: datetime) -> list[PaperFill]:
        frame = self.database.get_open_positions()
        if frame.empty:
            return []
        fills: list[PaperFill] = []
        latest_run = self.database.latest_signal_run(at.date(), SignalKind.PRECLOSE_ENTRY.value)
        score_map: dict[str, dict[str, object]] = {}
        can_reassess = bool(latest_run and latest_run.get("status") in {"success", "degraded"})
        if can_reassess:
            candidates = self.database.get_candidates(str(latest_run["run_id"]), include_blocked=True)
            score_map = candidates.set_index("ts_code").to_dict("index") if not candidates.empty else {}

        for row in frame.to_dict("records"):
            position = PaperPosition.model_validate(row)
            if position.opened_trade_date >= at.date():
                continue  # T+1: no same-day exit.
            start = datetime.combine(at.date(), self.settings.schedule.market_morning_open, tzinfo=at.tzinfo)
            bars = sorted(
                self.live_provider.fetch_intraday_bars(position.ts_code, start, at, 5), key=lambda bar: bar.start_at
            )
            target, stop = self.fees.trigger_prices(position)
            triggered: tuple[float, datetime, str] | None = None
            for bar in bars:
                if bar.low <= stop and bar.high >= target:
                    raw = min(bar.open, stop)
                    triggered = (raw, bar.end_at, "stop_loss")
                    break
                if bar.low <= stop:
                    raw = min(bar.open, stop)
                    triggered = (raw, bar.end_at, "stop_loss")
                    break
                if bar.high >= target:
                    raw = max(bar.open, target)
                    triggered = (raw, bar.end_at, "take_profit")
                    break
            if triggered:
                fills.append(self._close_position(position, *triggered))
                continue

            holding_days = self.database.count_trading_days(position.opened_trade_date, at.date())
            if at.time() >= self.settings.schedule.preclose_run_at:
                candidate = score_map.get(position.ts_code)
                reason: str | None = None
                if can_reassess and candidate and bool(candidate.get("blocked")):
                    reason = "risk_event"
                elif can_reassess and (
                    candidate is None
                    or float(candidate.get("opportunity_score", 0.0)) < self.settings.score_exit_threshold
                ):
                    reason = "score_exit"
                elif holding_days >= self.settings.max_holding_days:
                    reason = "max_holding_days"
                if reason:
                    quotes = self.live_provider.fetch_quotes([position.ts_code])
                    if not quotes.empty:
                        raw_price = float(quotes.iloc[0]["price"])
                        fills.append(self._close_position(position, raw_price, at, reason))
        return fills
