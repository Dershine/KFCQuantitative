from __future__ import annotations

from datetime import datetime

from kfcquant.application.ports import CandidateEvaluationRepository
from kfcquant.config import Settings
from kfcquant.interfaces import LiveQuoteProvider
from kfcquant.models import CandidateOutcome, EvaluationStatus, SignalKind


class CandidateEvaluationService:
    def __init__(self, repository: CandidateEvaluationRepository, settings: Settings, live_provider: LiveQuoteProvider):
        self.repository = repository
        self.settings = settings
        self.live_provider = live_provider

    def evaluate(self, run: dict[str, object], evaluation_date: datetime) -> list[CandidateOutcome]:
        kind = SignalKind(str(run["signal_kind"]))
        candidates = self.settings.selection.select_frame(
            self.repository.get_candidates(str(run["run_id"]), include_blocked=True)
        )
        outcomes: list[CandidateOutcome] = []
        signal_at = run["as_of"]
        if not isinstance(signal_at, datetime):
            signal_at = datetime.fromisoformat(str(signal_at))
        for candidate in candidates.to_dict("records"):
            code = str(candidate["ts_code"])
            if kind == SignalKind.MORNING_WATCHLIST:
                baseline_start = datetime.combine(
                    signal_at.date(), self.settings.schedule.market_morning_open, tzinfo=signal_at.tzinfo
                )
                window_end = datetime.combine(
                    signal_at.date(), self.settings.schedule.preclose_run_at, tzinfo=signal_at.tzinfo
                )
                morning_bars = sorted(
                    self.live_provider.fetch_intraday_bars(code, baseline_start, window_end, 5),
                    key=lambda item: item.start_at,
                )
                baseline_bars = morning_bars[:1]
                evaluation_bars = morning_bars[1:]
            else:
                baseline_start = datetime.combine(
                    signal_at.date(), self.settings.schedule.preclose_run_at, tzinfo=signal_at.tzinfo
                )
                baseline_end = datetime.combine(
                    signal_at.date(), self.settings.schedule.fill_at, tzinfo=signal_at.tzinfo
                )
                evaluation_start = datetime.combine(
                    evaluation_date.date(),
                    self.settings.schedule.market_morning_open,
                    tzinfo=evaluation_date.tzinfo,
                )
                window_end = datetime.combine(
                    evaluation_date.date(), self.settings.schedule.market_close, tzinfo=evaluation_date.tzinfo
                )
                baseline_bars = sorted(
                    self.live_provider.fetch_intraday_bars(code, baseline_start, baseline_end, 5),
                    key=lambda item: item.start_at,
                )
                evaluation_bars = sorted(
                    self.live_provider.fetch_intraday_bars(code, evaluation_start, window_end, 5),
                    key=lambda item: item.start_at,
                )
            outcome = self._from_bars(
                run,
                code,
                kind,
                baseline_bars,
                evaluation_bars,
                evaluation_date,
            )
            self.repository.save_candidate_outcome(outcome)
            outcomes.append(outcome)
        return outcomes

    def _from_bars(self, run, code, kind, baseline_bars, evaluation_bars, evaluated_at) -> CandidateOutcome:
        attribution = {
            "strategy_id": run["strategy_id"],
            "strategy_version": run["strategy_version"],
            "parameter_hash": run["parameter_hash"],
            "strategy_parameters": run["strategy_parameters"],
        }
        if not baseline_bars or not evaluation_bars:
            return CandidateOutcome(
                **attribution,
                run_id=str(run["run_id"]),
                ts_code=code,
                signal_kind=kind,
                status=EvaluationStatus.NOT_EVALUABLE,
                reason="缺少有效5分钟行情",
                evaluated_at=evaluated_at,
            )
        baseline_bar = baseline_bars[0]
        if baseline_bar.volume <= 0 or baseline_bar.amount <= 0:
            return CandidateOutcome(
                **attribution,
                run_id=str(run["run_id"]),
                ts_code=code,
                signal_kind=kind,
                status=EvaluationStatus.NOT_EVALUABLE,
                reason="基准窗口无可验证成交",
                evaluated_at=evaluated_at,
            )
        raw_baseline = baseline_bar.amount / baseline_bar.volume
        baseline = raw_baseline * (1 + self.settings.slippage_rate)
        exit_cost = self.settings.commission_rate + self.settings.stamp_duty_rate + self.settings.slippage_rate
        target = baseline * (1 + self.settings.take_profit_net + exit_cost)
        highs = [bar.high for bar in evaluation_bars]
        lows = [bar.low for bar in evaluation_bars]
        hit_bar = next((bar for bar in evaluation_bars if bar.high >= target), None)
        return CandidateOutcome(
            **attribution,
            run_id=str(run["run_id"]),
            ts_code=code,
            signal_kind=kind,
            status=EvaluationStatus.HIT if hit_bar else EvaluationStatus.MISS,
            baseline_at=baseline_bar.end_at,
            baseline_price=baseline,
            target_price=target,
            hit_at=hit_bar.end_at if hit_bar else None,
            max_favorable_excursion=max(highs) / baseline - 1,
            max_adverse_excursion=min(lows) / baseline - 1,
            evaluated_at=evaluated_at,
        )
