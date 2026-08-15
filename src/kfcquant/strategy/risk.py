from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from kfcquant.models import EventDirection, RiskSeverity, SignalKind

_SEVERITY_PENALTIES = {
    RiskSeverity.LOW.value: 1.0,
    RiskSeverity.MEDIUM.value: 3.0,
    RiskSeverity.HIGH.value: 7.0,
    RiskSeverity.CRITICAL.value: 15.0,
}


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    news_score: float
    news_penalty: float
    risk_event_ids: tuple[str, ...]
    blocked: bool
    block_reasons: tuple[str, ...]


class RiskPolicy:
    """Applies evidence-bound intelligence adjustments without changing technical scores."""

    def __init__(
        self,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._events_by_code: dict[str, tuple[dict[str, object], ...]] = {}
        if risk_events is not None and not risk_events.empty:
            grouped: dict[str, list[dict[str, object]]] = {}
            for event in risk_events.to_dict("records"):
                code = event.get("ts_code")
                if code is not None and not pd.isna(code) and str(code).strip():
                    grouped.setdefault(str(code), []).append(event)
            self._events_by_code = {code: tuple(events) for code, events in grouped.items()}
        self._unprocessed_official_codes = frozenset(unprocessed_official_codes or ())

    def assess(self, ts_code: str, signal_kind: SignalKind) -> RiskAssessment:
        events = self._events_by_code.get(ts_code, ())
        positive_multiplier, positive_cap = (
            (3.0, 10.0) if signal_kind == SignalKind.MORNING_WATCHLIST else (2.5, 7.0)
        )
        positives = [event for event in events if str(event.get("direction")) == EventDirection.POSITIVE.value]
        negatives = [event for event in events if str(event.get("direction")) == EventDirection.NEGATIVE.value]
        news_score = min(
            sum(float(event.get("confidence") or 0.0) * positive_multiplier for event in positives),
            positive_cap,
        )
        news_penalty = min(
            sum(_SEVERITY_PENALTIES.get(str(event.get("severity")), 0.0) for event in negatives),
            20.0,
        )

        reasons: list[str] = []
        if ts_code in self._unprocessed_official_codes:
            reasons.append("存在尚未完成抽取的官方公告")
        for event in events:
            evidence_value = event.get("evidence")
            evidence = evidence_value.strip() if isinstance(evidence_value, str) else ""
            if bool(event.get("hard_block")) and evidence:
                reasons.append(f"{event.get('event_type')}: {evidence}")
        event_ids = tuple(str(event["event_id"]) for event in events if event.get("event_id") is not None)
        return RiskAssessment(
            news_score=news_score,
            news_penalty=news_penalty,
            risk_event_ids=event_ids,
            blocked=bool(reasons),
            block_reasons=tuple(reasons),
        )
