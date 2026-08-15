from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import ResearchRunState
from kfcquant.services.workflow import Workflow
from kfcquant.unit_of_work import DuckDBResearchRunUnitOfWork
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


def prepare_stage(settings, at, codes):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame([{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}])
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    return database


def test_preclose_publishes_complete_run_and_job_in_one_uow(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ", "002001.SZ"]
    database = prepare_stage(settings, at, codes)
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
    )

    run = workflow.run_preclose(at)

    assert run.lifecycle_state == ResearchRunState.PUBLISHED
    assert len(database.get_candidates(run.run_id)) == 3
    assert len(database.proposed_orders(run.run_id)) == 3
    job = database.latest_job("run-preclose")
    assert job["status"] == run.status.value
    assert job["finished_at"] is not None


def test_workflow_publication_failure_exposes_no_partial_run_and_can_retry(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    database = prepare_stage(settings, at, codes)

    class FailingUnitOfWork(DuckDBResearchRunUnitOfWork):
        def _checkpoint(self, stage):
            if stage == "orders":
                raise RuntimeError("publication interrupted")

    failed_workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
        run_uow=FailingUnitOfWork(database),
    )

    with pytest.raises(RuntimeError, match="publication interrupted"):
        failed_workflow.run_preclose(at)

    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("candidate_scores").empty
    assert database.table("paper_orders").empty
    assert database.latest_job("run-preclose")["status"] == "failed"

    retry = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
    ).run_preclose(at)

    assert retry.lifecycle_state == ResearchRunState.PUBLISHED
    assert len(database.get_candidates(retry.run_id)) == 2
    assert len(database.proposed_orders(retry.run_id)) == 2
