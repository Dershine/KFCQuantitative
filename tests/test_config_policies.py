from __future__ import annotations

from datetime import datetime, time

import pytest
from pydantic import ValidationError

from kfcops.config import OpsSettings
from kfcquant.config import SHANGHAI_TZ, Settings


def test_schedule_policy_owns_runtime_windows_and_registration_plan():
    settings = Settings(
        schedule={
            "calendar_sync_at": "07:55",
            "morning_run_at": "08:20",
            "morning_window_start": "08:15",
            "morning_window_end": "08:25",
            "morning_evaluation_at": "14:25",
            "preclose_run_at": "14:30",
            "preclose_window_start": "14:25",
            "preclose_window_end": "14:33",
            "fill_at": "14:35",
            "fill_window_start": "14:33",
            "fill_window_end": "14:40",
            "eod_sync_at": "18:20",
            "postclose_at": "20:40",
        }
    )

    schedule = settings.schedule
    assert schedule.preclose_window.contains(datetime(2026, 8, 10, 14, 30, tzinfo=SHANGHAI_TZ))
    assert not schedule.preclose_window.contains(datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ))
    assert schedule.fill_window.contains(datetime(2026, 8, 10, 14, 35, tzinfo=SHANGHAI_TZ))

    plan = schedule.registration_plan()
    assert plan["tasks"][0] == {"name": "SyncCalendar", "command": "sync-calendar", "at": "07:55"}
    assert {item["command"]: item["at"] for item in plan["tasks"]}["run-preclose"] == "14:30"
    assert plan["monitor"]["start"] == "09:30"
    assert plan["monitor"]["interval_minutes"] == 5


def test_selection_policy_validates_the_shared_top_n_limit():
    settings = Settings(selection={"top_n": 3, "candidate_limit": 7}, max_positions=3, position_fraction=0.3)

    assert settings.selection.top_n == 3
    assert settings.selection.candidate_limit == 7

    with pytest.raises(ValidationError, match="candidate_limit"):
        Settings(selection={"top_n": 10, "candidate_limit": 5})


@pytest.mark.parametrize(
    "overrides",
    [
        {"position_fraction": 0},
        {"commission_rate": -0.001},
        {"max_positions": 6, "position_fraction": 0.2},
        {"market_provider": "unknown"},
        {"market_provider": "tushare", "tushare_token": None},
        {"strategy_version_morning": "contains whitespace"},
        {
            "schedule": {
                "preclose_run_at": "14:40",
                "preclose_window_start": "14:41",
                "preclose_window_end": "14:43",
            }
        },
    ],
)
def test_research_settings_reject_unsafe_startup_configuration(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_ops_settings_reject_default_secret_and_invalid_protected_window(tmp_path):
    with pytest.raises(ValidationError, match="session_secret"):
        OpsSettings(database_path=tmp_path / "ops.sqlite3", session_secret="change-me")

    with pytest.raises(ValidationError, match="protected"):
        OpsSettings(
            database_path=tmp_path / "ops.sqlite3",
            session_secret="x" * 32,
            protected_window_start=time(15, 10),
            protected_window_end=time(8, 15),
        )
