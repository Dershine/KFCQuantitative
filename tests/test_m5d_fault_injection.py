from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import pytest
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from kfcquant.clock import ReplayClock
from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.observability import MemoryObservabilitySink, Observability
from kfcquant.runtime import heartbeat_path, observe_worker_heartbeat, write_heartbeat
from kfcquant.services.workflow import Workflow
from kfcquant.unit_of_work import DuckDBResearchRunUnitOfWork
from tests.conftest import make_daily, make_quotes, make_securities
from tests.test_workflow import FakeLive, FakeLLM, FakeMarket


def _prepare_preclose(
    settings,
    at: datetime,
    codes: list[str],
    *,
    lock_timeout_seconds: int = 30,
    lock_path=None,
) -> Database:
    database = Database(
        settings.database_path,
        settings.initial_cash,
        lock_timeout_seconds=lock_timeout_seconds,
        lock_path=lock_path,
    )
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [
                {
                    "cal_date": at.date(),
                    "is_open": True,
                    "pretrade_date": (at - timedelta(days=3)).date(),
                }
            ]
        )
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    return database


class TimeoutLiveProvider:
    source_name = "timeout-live"

    def fetch_quotes(self, ts_codes=None):
        raise TimeoutError("injected live quote timeout")

    def fetch_intraday_bars(self, ts_code, start, end, frequency_minutes=5):
        return []


def test_provider_timeout_fails_closed_before_publication_and_healthy_retry_succeeds(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    database = _prepare_preclose(settings, at, codes)
    clock = ReplayClock(at)
    failing = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=TimeoutLiveProvider(),
        llm_provider=FakeLLM(),
        clock=clock,
    )

    with pytest.raises(TimeoutError, match="injected live quote timeout"):
        failing.run_preclose(at)

    assert database.latest_job("run-preclose")["status"] == "failed"
    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("run_manifests").empty
    assert database.table("paper_orders").empty
    with database.connect(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM live_quotes").fetchone()[0] == 0

    retry_clock = ReplayClock(at + timedelta(seconds=1))
    run = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
        clock=retry_clock,
    ).run_preclose(at)

    assert run.tradable
    assert len(database.proposed_orders(run.run_id)) == len(codes)
    assert database.latest_job("run-preclose")["status"] == "success"


def test_process_crash_rolls_back_publication_then_lease_recovery_allows_safe_retry(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    database = _prepare_preclose(settings, at, codes)

    class CrashAfterOrders(DuckDBResearchRunUnitOfWork):
        def _checkpoint(self, stage: str) -> None:
            if stage == "orders":
                raise SystemExit("injected worker termination")

    clock = ReplayClock(at)
    crashed = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
        run_uow=CrashAfterOrders(database),
        clock=clock,
    )

    with pytest.raises(SystemExit, match="injected worker termination"):
        crashed.run_preclose(at)

    interrupted_job = database.latest_job("run-preclose")
    assert interrupted_job["status"] == "running"
    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("run_manifests").empty
    assert database.table("candidate_scores").empty
    assert database.table("paper_orders").empty

    recovered_at = at + timedelta(seconds=settings.job_lease_seconds + 1)
    recovered = crashed.recover_expired_jobs(recovered_at)
    assert recovered == [interrupted_job["job_run_id"]]
    assert database.latest_job("run-preclose")["status"] == "failed"

    retry_clock = ReplayClock(recovered_at)
    run = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
        clock=retry_clock,
    ).run_preclose(at)

    assert run.tradable
    assert len(database.get_candidates(run.run_id)) == len(codes)
    assert len(database.proposed_orders(run.run_id)) == len(codes)


def test_database_lock_timeout_leaves_no_job_or_run_and_release_allows_retry(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH"]
    lock_path = settings.runtime_dir / "fault-injection.lock"
    database = _prepare_preclose(
        settings,
        at,
        codes,
        lock_timeout_seconds=0,
        lock_path=lock_path,
    )
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(make_quotes(codes, at)),
        llm_provider=FakeLLM(),
        clock=ReplayClock(at),
    )
    competing = FileLock(lock_path, timeout=0)
    competing.acquire()
    try:
        with pytest.raises(FileLockTimeout):
            workflow.run_preclose(at)
    finally:
        competing.release()

    assert database.table("job_runs").empty
    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("paper_orders").empty

    run = workflow.run_preclose(at)
    assert run.tradable
    assert len(database.proposed_orders(run.run_id)) == 1


def test_torn_heartbeat_fails_closed_and_next_atomic_write_recovers(settings, monkeypatch):
    sink = MemoryObservabilitySink()
    observability = Observability([sink])
    path = heartbeat_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    now = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)

    unreadable = observe_worker_heartbeat(settings, observability, now=now)
    assert unreadable == {"ok": False, "error": "JSONDecodeError"}

    monkeypatch.setattr(
        "kfcquant.runtime.version_info",
        lambda configured: {"version": "fixture", "source_sha": "a" * 40},
    )
    write_heartbeat(settings)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == "fixture"
    assert payload["source_sha"] == "a" * 40
    assert not path.with_suffix(".tmp").exists()
