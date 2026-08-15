from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    import pandas as pd

    from kfcquant.models import CandidateScore


def _time_text(value: time) -> str:
    return value.strftime("%H:%M")


class TimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: time
    end: time

    @model_validator(mode="after")
    def validate_order(self) -> TimeWindow:
        if self.start > self.end:
            raise ValueError("window start must not be after window end")
        return self

    def contains(self, value: datetime | time) -> bool:
        candidate = value.time() if isinstance(value, datetime) else value
        candidate = candidate.replace(tzinfo=None)
        return self.start <= candidate <= self.end

    def describe(self) -> str:
        return f"{_time_text(self.start)}至{_time_text(self.end)}"


class SchedulePolicy(BaseModel):
    """Single source for research triggers, safety windows, and market sessions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calendar_sync_at: time = time(8, 0)
    morning_run_at: time = time(8, 30)
    morning_window_start: time = time(8, 25)
    morning_window_end: time = time(8, 35)
    morning_evaluation_at: time = time(14, 35)
    preclose_run_at: time = time(14, 40)
    preclose_window_start: time = time(14, 35)
    preclose_window_end: time = time(14, 43)
    fill_at: time = time(14, 45)
    fill_window_start: time = time(14, 43)
    fill_window_end: time = time(14, 50)
    eod_sync_at: time = time(18, 10)
    postclose_at: time = time(20, 30)
    market_morning_open: time = time(9, 30)
    market_morning_close: time = time(11, 30)
    market_afternoon_open: time = time(13, 0)
    market_close: time = time(15, 0)
    monitor_start: time = time(9, 30)
    monitor_end: time = time(15, 0)
    monitor_interval_minutes: int = Field(default=5, ge=1, le=60)
    heartbeat_interval_minutes: int = Field(default=1, ge=1, le=60)

    @property
    def morning_window(self) -> TimeWindow:
        return TimeWindow(start=self.morning_window_start, end=self.morning_window_end)

    @property
    def preclose_window(self) -> TimeWindow:
        return TimeWindow(start=self.preclose_window_start, end=self.preclose_window_end)

    @property
    def fill_window(self) -> TimeWindow:
        return TimeWindow(start=self.fill_window_start, end=self.fill_window_end)

    @model_validator(mode="after")
    def validate_timeline(self) -> SchedulePolicy:
        for name, trigger, window in (
            ("morning", self.morning_run_at, self.morning_window),
            ("preclose", self.preclose_run_at, self.preclose_window),
            ("fill", self.fill_at, self.fill_window),
        ):
            if not window.contains(trigger):
                raise ValueError(f"{name} trigger must be inside its configured window")
        if not (
            self.calendar_sync_at
            < self.morning_run_at
            < self.morning_evaluation_at
            <= self.preclose_run_at
            < self.fill_at
            < self.eod_sync_at
            < self.postclose_at
        ):
            raise ValueError(
                "scheduled jobs must follow calendar, morning, evaluation, preclose, fill, EOD, postclose order"
            )
        if not (
            self.market_morning_open
            < self.market_morning_close
            < self.market_afternoon_open
            < self.market_close
        ):
            raise ValueError("market sessions must be ordered and non-overlapping")
        if not (self.market_morning_open <= self.monitor_start < self.monitor_end <= self.market_close):
            raise ValueError("monitor window must stay inside the market session span")
        if self.preclose_window_end > self.fill_window_end or self.fill_window_start < self.preclose_run_at:
            raise ValueError("fill window must follow the preclose signal cutoff")
        if self.fill_window_end > self.market_close:
            raise ValueError("fill window must close no later than the market session")
        return self

    def is_trading_session(self, value: datetime | time) -> bool:
        candidate = value.time() if isinstance(value, datetime) else value
        candidate = candidate.replace(tzinfo=None)
        return (
            self.market_morning_open <= candidate <= self.market_morning_close
            or self.market_afternoon_open <= candidate <= self.market_close
        )

    def monitor_times(self) -> tuple[time, ...]:
        anchor = date(2000, 1, 1)
        current = datetime.combine(anchor, self.monitor_start)
        end = datetime.combine(anchor, self.monitor_end)
        interval = timedelta(minutes=self.monitor_interval_minutes)
        values: list[time] = []
        while current <= end:
            values.append(current.time())
            current += interval
        return tuple(values)

    def scheduled_tasks(self) -> tuple[tuple[str, str, time], ...]:
        return (
            ("SyncCalendar", "sync-calendar", self.calendar_sync_at),
            ("Morning", "run-morning", self.morning_run_at),
            ("EvaluateMorning", "evaluate-morning", self.morning_evaluation_at),
            ("Preclose", "run-preclose", self.preclose_run_at),
            ("CaptureFill", "capture-fill", self.fill_at),
            ("SyncEod", "sync-eod", self.eod_sync_at),
            ("Postclose", "run-postclose", self.postclose_at),
        )

    def registration_plan(self) -> dict[str, object]:
        anchor = date(2000, 1, 1)
        monitor_duration = datetime.combine(anchor, self.monitor_end) - datetime.combine(anchor, self.monitor_start)
        return {
            "tasks": [
                {"name": name, "command": command, "at": _time_text(at)}
                for name, command, at in self.scheduled_tasks()
            ],
            "monitor": {
                "name": "Monitor",
                "command": "monitor-paper",
                "start": _time_text(self.monitor_start),
                "interval_minutes": self.monitor_interval_minutes,
                "duration_minutes": int(monitor_duration.total_seconds() // 60),
            },
        }


class SelectionPolicy(BaseModel):
    """Single source for ranking, thresholds, and consumer candidate selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    top_n: int = Field(default=10, ge=1, le=100)
    candidate_limit: int = Field(default=100, ge=1, le=10_000)
    minimum_opportunity_score: float = Field(default=0.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def validate_limits(self) -> SelectionPolicy:
        if self.candidate_limit < self.top_n:
            raise ValueError("candidate_limit must be greater than or equal to top_n")
        return self

    def rank_candidates(self, candidates: list[CandidateScore]) -> list[CandidateScore]:
        eligible = [
            candidate
            for candidate in candidates
            if candidate.opportunity_score >= self.minimum_opportunity_score
        ]
        ordered = sorted(
            eligible,
            key=lambda candidate: (
                candidate.blocked,
                -candidate.opportunity_score,
                candidate.ts_code,
            ),
        )
        return [
            candidate.model_copy(update={"rank": rank})
            for rank, candidate in enumerate(ordered[: self.candidate_limit], start=1)
        ]

    def includes(self, rank: int, opportunity_score: float, blocked: bool) -> bool:
        return (
            not blocked
            and rank <= self.top_n
            and opportunity_score >= self.minimum_opportunity_score
        )

    def select_candidates(self, candidates: list[CandidateScore]) -> list[CandidateScore]:
        return sorted(
            (
                candidate
                for candidate in candidates
                if self.includes(candidate.rank, candidate.opportunity_score, candidate.blocked)
            ),
            key=lambda candidate: candidate.rank,
        )[: self.top_n]

    def select_frame(self, candidates: pd.DataFrame) -> pd.DataFrame:
        if candidates.empty:
            return candidates.copy()
        required = {"rank", "opportunity_score", "blocked"}
        missing = sorted(required - set(candidates.columns))
        if missing:
            raise ValueError(f"candidate frame is missing selection columns: {', '.join(missing)}")
        selected = candidates[
            (~candidates["blocked"].astype(bool))
            & (candidates["rank"] <= self.top_n)
            & (candidates["opportunity_score"] >= self.minimum_opportunity_score)
        ]
        return selected.sort_values("rank").head(self.top_n).copy()
