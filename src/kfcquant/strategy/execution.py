from __future__ import annotations

from dataclasses import dataclass

from kfcquant.run_manifest import candidate_result_sha256
from kfcquant.strategy.contracts import StrategyContext, StrategyIdentity, StrategyResult
from kfcquant.strategy.registry import StrategyRegistry


class StrategyExecutionViolation(ValueError):
    """A Strategy cannot be executed under the requested deterministic identity."""


@dataclass(frozen=True, slots=True)
class StrategyExecution:
    """The canonical output of the Strategy kernel shared by live and Replay paths."""

    identity: StrategyIdentity
    result: StrategyResult
    result_sha256: str


class StrategyExecutionRunner:
    """Resolve and execute one registered Strategy through the shared kernel."""

    def __init__(self, registry: StrategyRegistry):
        self.registry = registry

    def execute(
        self,
        context: StrategyContext,
        *,
        expected_identity: StrategyIdentity | None = None,
    ) -> StrategyExecution:
        strategy = self.registry.resolve(context.signal_kind)
        if expected_identity is not None and strategy.identity != expected_identity:
            raise StrategyExecutionViolation(
                "registered Strategy Identity does not match the requested execution identity"
            )
        result = strategy.evaluate(context)
        if any(candidate.run_id != context.run_id for candidate in result.candidates):
            raise StrategyExecutionViolation(
                "Strategy candidates must belong to the execution context run_id"
            )
        return StrategyExecution(
            identity=strategy.identity,
            result=result,
            result_sha256=candidate_result_sha256(result.candidates),
        )
