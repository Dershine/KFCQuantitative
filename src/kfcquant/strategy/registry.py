from __future__ import annotations

from collections.abc import Iterable

from kfcquant.models import SignalKind
from kfcquant.strategy.contracts import Strategy


class StrategyRegistry:
    """Explicit in-process registry keyed by the Signal kind it evaluates."""

    def __init__(self, strategies: Iterable[Strategy] = ()) -> None:
        self._strategies: dict[SignalKind, Strategy] = {}
        for strategy in strategies:
            self.register(strategy)

    def register(self, strategy: Strategy) -> None:
        signal_kind = SignalKind(strategy.signal_kind)
        if signal_kind in self._strategies:
            registered = self._strategies[signal_kind]
            raise ValueError(
                f"strategy already registered for {signal_kind.value}: {registered.identity.strategy_id}"
            )
        self._strategies[signal_kind] = strategy

    def resolve(self, signal_kind: SignalKind) -> Strategy:
        normalized = SignalKind(signal_kind)
        try:
            return self._strategies[normalized]
        except KeyError as exc:
            raise LookupError(f"no strategy registered for {normalized.value}") from exc

    def require(self, signal_kinds: Iterable[SignalKind]) -> None:
        missing = [kind.value for kind in signal_kinds if kind not in self._strategies]
        if missing:
            raise ValueError(f"strategy registry is missing required signal kinds: {', '.join(missing)}")
