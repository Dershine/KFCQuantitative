from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.observability import AlertCode, MemoryObservabilitySink, Observability
from kfcquant.run_manifest import RunInputKind
from kfcquant.services.workflow import Workflow
from tests.conftest import make_daily, make_quotes, make_securities


class FakeMarket:
    def fetch_official_documents(self, start, end):
        return []

    def fetch_mainstream_documents(self, start, end):
        return []


class FakeLive:
    source_name = "fixture-live"

    def __init__(self, quotes):
        self.quotes = quotes

    def fetch_quotes(self, ts_codes=None):
        return self.quotes.copy()

    def fetch_intraday_bars(self, ts_code, start, end, frequency_minutes=5):
        return []


class MutableClock:
    def __init__(self, current: datetime):
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class DelayedLive(FakeLive):
    def __init__(self, codes: list[str], clock: MutableClock, delay_seconds: int):
        super().__init__(pd.DataFrame())
        self.codes = codes
        self.clock = clock
        self.delay_seconds = delay_seconds

    def fetch_quotes(self, ts_codes=None):
        self.clock.advance(self.delay_seconds)
        return make_quotes(self.codes, self.clock.now())


class FakeLLM:
    def extract_risk_events(self, document):
        return []

    def generate_report(self, context):
        return "# fixture"


class FakeRangeMarket:
    source_name = "fixture-range"

    def __init__(self, securities, bars):
        self.securities = securities
        self.bars = bars

    def fetch_securities(self):
        return self.securities.copy()

    def fetch_trade_calendar(self, start, end):
        dates = pd.date_range(start=start, end=end, freq="D")
        return pd.DataFrame(
            [
                {
                    "cal_date": item.date(),
                    "is_open": item.weekday() < 5,
                    "pretrade_date": None,
                }
                for item in dates
            ]
        )

    def iter_daily_range(self, start, end, ts_codes):
        assert ts_codes
        yield self.bars[(self.bars["trade_date"] >= start) & (self.bars["trade_date"] <= end)].copy()


def test_preclose_end_to_end_creates_auditable_orders(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ", "002001.SZ", "603001.SH", "001001.SZ", "605001.SH"]
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame([{"cal_date": at.date(), "is_open": True, "pretrade_date": (at - timedelta(days=3)).date()}])
    )
    database.upsert_securities(make_securities([(code, code) for code in codes]))
    database.upsert_daily_bars(make_daily(codes, at))
    quotes = make_quotes(codes, at)
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(quotes),
        llm_provider=FakeLLM(),
    )
    run = workflow.run_preclose(as_of=at)
    assert run.tradable
    assert run.candidate_count == len(codes)
    assert len(database.get_candidates(run.run_id)) == len(codes)
    assert len(database.proposed_orders(run.run_id)) == len(codes)  # first five plus one reserve candidate
    stored_manifest = database.get_run_manifest(run.run_id)["manifest"]
    assert stored_manifest.run_id == run.run_id
    assert stored_manifest.source_sha
    assert isinstance(stored_manifest.source_dirty, bool)
    assert len(stored_manifest.dependency_lock_sha256) == 64
    assert len(stored_manifest.result_sha256) == 64
    assert {snapshot.dataset_kind for snapshot in stored_manifest.input_snapshots} >= {
        RunInputKind.SECURITY,
        RunInputKind.DAILY_BAR,
        RunInputKind.LIVE_QUOTE,
    }
    quote_snapshot = next(
        snapshot
        for snapshot in stored_manifest.input_snapshots
        if snapshot.dataset_kind == RunInputKind.LIVE_QUOTE
    )
    assert len(quote_snapshot.ingestion_batch_ids) == 1


def test_preclose_future_quote_fails_before_strategy_and_never_publishes_or_orders(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame([{"cal_date": at.date(), "is_open": True, "pretrade_date": date(2026, 8, 7)}])
    )
    database.upsert_securities(make_securities([(code, code)]))
    database.upsert_daily_bars(make_daily([code], at))
    class NewsMustNotRun(FakeMarket):
        def fetch_official_documents(self, start, end):
            raise AssertionError("news synchronization must not run for a future quote batch")

    sink = MemoryObservabilitySink()
    workflow = Workflow(
        settings,
        database=database,
        market_provider=NewsMustNotRun(),
        live_provider=FakeLive(make_quotes([code], at + timedelta(seconds=1))),
        llm_provider=FakeLLM(),
        observability=Observability((sink,)),
    )

    with pytest.raises(ValueError, match="live_quote.*information_cutoff"):
        workflow.run_preclose(as_of=at)

    assert database.latest_signal_run(include_non_terminal=True) is None
    assert database.table("run_manifests").empty
    assert database.table("paper_orders").empty
    assert database.get_latest_quotes().empty
    assert database.latest_job("run-preclose")["status"] == "failed"
    assert any(
        record.get("alert_code") == AlertCode.QUOTE_DATA_FUTURE.value
        for record in sink.records
    )


def test_live_preclose_freezes_cutoff_after_delayed_quote_observation(settings):
    triggered_at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    clock = MutableClock(triggered_at)
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": triggered_at.date(), "is_open": True, "pretrade_date": date(2026, 8, 7)}]
        )
    )
    database.upsert_securities(make_securities([(code, code)]))
    database.upsert_daily_bars(make_daily([code], triggered_at))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=DelayedLive([code], clock, delay_seconds=30),
        llm_provider=FakeLLM(),
        clock=clock,
    )

    run = workflow.run_preclose()

    observed_at = triggered_at + timedelta(seconds=30)
    assert run.information_cutoff == observed_at
    assert run.as_of == observed_at
    assert run.data_as_of == observed_at
    assert run.tradable
    job = database.latest_job("run-preclose")
    assert job["scheduled_for"] == triggered_at
    assert job["status"] == "success"


def test_live_preclose_marks_run_missed_when_quote_collection_exceeds_window(settings):
    triggered_at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    clock = MutableClock(triggered_at)
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame(
            [{"cal_date": triggered_at.date(), "is_open": True, "pretrade_date": date(2026, 8, 7)}]
        )
    )
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=DelayedLive([code], clock, delay_seconds=181),
        llm_provider=FakeLLM(),
        clock=clock,
    )

    run = workflow.run_preclose()

    assert run.status.value == "missed"
    assert "实时行情采集完成时已超过" in run.message
    assert database.table("paper_orders").empty


def test_preclose_outside_window_is_recorded_missed(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame([{"cal_date": date(2026, 8, 10), "is_open": True, "pretrade_date": date(2026, 8, 7)}])
    )
    workflow = Workflow(
        settings,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(pd.DataFrame()),
        llm_provider=FakeLLM(),
    )
    run = workflow.run_preclose(datetime(2026, 8, 10, 16, 0, tzinfo=SHANGHAI_TZ))
    assert run.status.value == "missed"
    assert not run.tradable


def test_preclose_runtime_gate_uses_configured_schedule_policy(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    custom_schedule = settings.schedule.model_copy(
        update={
            "morning_evaluation_at": time(14, 25),
            "preclose_run_at": time(14, 30),
            "preclose_window_start": time(14, 25),
            "preclose_window_end": time(14, 33),
            "fill_at": time(14, 35),
            "fill_window_start": time(14, 33),
            "fill_window_end": time(14, 40),
        }
    )
    configured = settings.model_copy(update={"schedule": custom_schedule})
    database = Database(configured.database_path, configured.initial_cash)
    database.initialize()
    database.upsert_trade_calendar(
        pd.DataFrame([{"cal_date": at.date(), "is_open": True, "pretrade_date": date(2026, 8, 7)}])
    )
    workflow = Workflow(
        configured,
        database=database,
        market_provider=FakeMarket(),
        live_provider=FakeLive(pd.DataFrame()),
        llm_provider=FakeLLM(),
    )

    run = workflow.run_preclose(at)

    assert run.status.value == "missed"
    assert "14:25至14:33" in run.message


def test_preclose_fails_closed_when_calendar_does_not_confirm_open(settings):
    workflow = Workflow(
        settings,
        database=Database(settings.database_path, settings.initial_cash),
        market_provider=FakeMarket(),
        live_provider=FakeLive(pd.DataFrame()),
        llm_provider=FakeLLM(),
    )
    run = workflow.run_preclose(datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ))
    assert run.status.value == "missed"
    assert "交易日历" in run.message


def test_sync_eod_uses_provider_range_loader(settings):
    end = datetime(2026, 8, 10, 16, 30, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    database = Database(settings.database_path, settings.initial_cash)
    market = FakeRangeMarket(make_securities([(code, "公司")]), make_daily([code], end, days=3))
    workflow = Workflow(
        settings,
        database=database,
        market_provider=market,
        live_provider=FakeLive(pd.DataFrame()),
        news_provider=FakeMarket(),
        llm_provider=FakeLLM(),
    )
    result = workflow.sync_eod(date(2026, 8, 5), date(2026, 8, 10))
    assert result["bars"] == 3
    assert len(database.get_recent_daily_bars()) == 3
