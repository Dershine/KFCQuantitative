from __future__ import annotations

from datetime import datetime

import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import CandidateScore, FactorBreakdown, ResearchRunState, RunStatus, SignalRun
from tests.conftest import strategy_attribution


def make_run(state: ResearchRunState = ResearchRunState.CREATED) -> SignalRun:
    return SignalRun(
        **strategy_attribution(),
        run_id="state-machine",
        as_of=datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ),
        status=RunStatus.RUNNING,
        lifecycle_state=state,
        data_fresh=False,
        official_news_healthy=False,
        mainstream_news_healthy=False,
        tradable=False,
    )


def test_research_run_state_machine_accepts_only_declared_transitions():
    run = make_run()

    run = run.transition_to(ResearchRunState.COLLECTING_DATA)
    run = run.transition_to(ResearchRunState.EVALUATING)
    run = run.transition_to(ResearchRunState.STAGED)
    run = run.transition_to(ResearchRunState.PUBLISHED)
    run = run.transition_to(ResearchRunState.EVALUATED)

    assert run.lifecycle_state == ResearchRunState.EVALUATED
    with pytest.raises(ValueError, match="illegal research run transition"):
        run.transition_to(ResearchRunState.COLLECTING_DATA)


def test_research_run_can_fail_from_active_state_but_not_after_failure():
    failed = make_run().transition_to(ResearchRunState.COLLECTING_DATA).transition_to(ResearchRunState.FAILED)

    assert failed.lifecycle_state == ResearchRunState.FAILED
    with pytest.raises(ValueError, match="illegal research run transition"):
        failed.transition_to(ResearchRunState.PUBLISHED)


def test_terminal_transitions_are_idempotent_and_degradation_fails_closed():
    published = (
        make_run()
        .transition_to(ResearchRunState.COLLECTING_DATA)
        .transition_to(ResearchRunState.EVALUATING)
        .transition_to(ResearchRunState.STAGED)
        .transition_to(ResearchRunState.PUBLISHED)
        .model_copy(update={"status": RunStatus.SUCCESS, "tradable": True})
    )

    assert published.transition_to(ResearchRunState.PUBLISHED) is published
    degraded = published.transition_to(ResearchRunState.DEGRADED)
    assert degraded.status == RunStatus.DEGRADED
    assert not degraded.tradable


def test_created_run_can_be_recorded_as_missed():
    missed = make_run().transition_to(ResearchRunState.MISSED)
    assert missed.status == RunStatus.MISSED
    assert missed.lifecycle_state == ResearchRunState.MISSED


def test_business_queries_hide_non_published_runs(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    staged = make_run(ResearchRunState.STAGED)
    database.save_signal_run(staged)
    database.save_candidates(
        [
            CandidateScore(
                run_id=staged.run_id,
                ts_code="600000.SH",
                name="fixture",
                rank=1,
                opportunity_score=80,
                factor_breakdown=FactorBreakdown(),
                quote_at=staged.as_of,
            )
        ]
    )

    assert database.latest_signal_run() is None
    assert database.get_candidates(staged.run_id).empty
    assert database.recent_signal_runs().empty
    assert database.latest_signal_run(include_non_terminal=True)["run_id"] == staged.run_id
    assert database.recent_signal_runs(include_non_terminal=True)["run_id"].tolist() == [staged.run_id]


@pytest.mark.parametrize(
    ("status", "expected_state"),
    [
        (RunStatus.SUCCESS, ResearchRunState.PUBLISHED),
        (RunStatus.DEGRADED, ResearchRunState.PUBLISHED),
        (RunStatus.MISSED, ResearchRunState.MISSED),
        (RunStatus.FAILED, ResearchRunState.FAILED),
        (RunStatus.RUNNING, ResearchRunState.EVALUATING),
    ],
)
def test_legacy_result_status_infers_compatible_lifecycle(status, expected_state):
    run = SignalRun(
        **strategy_attribution(),
        as_of=datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ),
        status=status,
        data_fresh=False,
        official_news_healthy=False,
        mainstream_news_healthy=False,
        tradable=False,
    )

    assert run.lifecycle_state == expected_state
