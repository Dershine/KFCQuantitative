from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd

from kfcquant.policies import SchedulePolicy
from kfcquant.strategy.universe import UniverseSelection

if TYPE_CHECKING:
    from kfcquant.config import Settings


class FeatureName(StrEnum):
    TS_CODE = "ts_code"
    NAME = "name"
    QUOTE_AT = "quote_at"
    RET_1D = "ret_1d"
    RET_5D = "ret_5d"
    RET_20D = "ret_20d"
    INTRADAY_STRENGTH = "intraday_strength"
    CLOSE_LOCATION = "close_location"
    PROJECTED_VOLUME_RATIO = "projected_volume_ratio"
    MEDIAN_AMOUNT_20D = "median_amount_20d"
    VOLATILITY_20D = "volatility_20d"
    GAP_ABS = "gap_abs"
    LIMIT_PROXIMITY = "limit_proximity"


FeatureDType = Literal["string", "float", "datetime"]


@dataclass(frozen=True, slots=True)
class FeatureField:
    name: FeatureName
    dtype: FeatureDType


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    """An explicit, versioned contract for a strategy feature frame."""

    name: str
    version: str
    fields: tuple[FeatureField, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(field.name.value for field in self.fields)

    def validate(self, frame: pd.DataFrame) -> None:
        columns = set(frame.columns)
        expected = set(self.columns)
        missing = sorted(expected - columns)
        unexpected = sorted(columns - expected)
        if missing:
            raise ValueError(f"feature frame is missing columns: {', '.join(missing)}")
        if unexpected:
            raise ValueError(f"feature frame contains unexpected columns: {', '.join(unexpected)}")
        if frame.empty:
            return
        for field in self.fields:
            series = frame[field.name.value]
            if field.dtype == "string" and not series.map(lambda value: isinstance(value, str)).all():
                raise ValueError(f"feature column {field.name.value} must contain strings")
            if field.dtype == "float" and not pd.api.types.is_numeric_dtype(series):
                raise ValueError(f"feature column {field.name.value} must be numeric")
            if field.dtype == "datetime" and not pd.api.types.is_datetime64_any_dtype(series):
                raise ValueError(f"feature column {field.name.value} must contain datetimes")


_IDENTITY_FIELDS = (
    FeatureField(FeatureName.TS_CODE, "string"),
    FeatureField(FeatureName.NAME, "string"),
    FeatureField(FeatureName.QUOTE_AT, "datetime"),
)

MORNING_FEATURE_SCHEMA = FeatureSchema(
    name="morning-watchlist-features",
    version="morning-features-v1",
    fields=(
        *_IDENTITY_FIELDS,
        FeatureField(FeatureName.RET_1D, "float"),
        FeatureField(FeatureName.RET_5D, "float"),
        FeatureField(FeatureName.RET_20D, "float"),
        FeatureField(FeatureName.CLOSE_LOCATION, "float"),
        FeatureField(FeatureName.PROJECTED_VOLUME_RATIO, "float"),
        FeatureField(FeatureName.MEDIAN_AMOUNT_20D, "float"),
        FeatureField(FeatureName.VOLATILITY_20D, "float"),
    ),
)

PRECLOSE_FEATURE_SCHEMA = FeatureSchema(
    name="preclose-entry-features",
    version="preclose-features-v1",
    fields=(
        *_IDENTITY_FIELDS,
        FeatureField(FeatureName.RET_5D, "float"),
        FeatureField(FeatureName.RET_20D, "float"),
        FeatureField(FeatureName.INTRADAY_STRENGTH, "float"),
        FeatureField(FeatureName.CLOSE_LOCATION, "float"),
        FeatureField(FeatureName.PROJECTED_VOLUME_RATIO, "float"),
        FeatureField(FeatureName.MEDIAN_AMOUNT_20D, "float"),
        FeatureField(FeatureName.VOLATILITY_20D, "float"),
        FeatureField(FeatureName.GAP_ABS, "float"),
        FeatureField(FeatureName.LIMIT_PROXIMITY, "float"),
    ),
)


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    frame: pd.DataFrame
    schema: FeatureSchema
    exclusion_counts: dict[str, int]

    def __post_init__(self) -> None:
        self.schema.validate(self.frame)


class FeaturePipeline:
    """Calculates market features without news, risk, scoring, or ranking."""

    def __init__(
        self,
        schedule: SchedulePolicy,
        quote_freshness_seconds: int,
        limit_distance_fraction: float,
    ) -> None:
        self.schedule = schedule
        self.quote_freshness_seconds = quote_freshness_seconds
        self.limit_distance_fraction = limit_distance_fraction

    @classmethod
    def from_settings(cls, settings: Settings) -> FeaturePipeline:
        return cls(
            schedule=settings.schedule,
            quote_freshness_seconds=settings.quote_freshness_seconds,
            limit_distance_fraction=settings.limit_distance_fraction,
        )

    @staticmethod
    def _empty(schema: FeatureSchema, exclusions: Counter[str] | None = None) -> FeatureFrame:
        counts = exclusions or Counter()
        counts["eligible_features"] = 0
        return FeatureFrame(pd.DataFrame(columns=schema.columns), schema, dict(counts))

    def build_morning(self, universe: UniverseSelection, as_of: datetime) -> FeatureFrame:
        if not universe.eligible_codes:
            return self._empty(MORNING_FEATURE_SCHEMA)
        names = universe.securities.set_index("ts_code")["name"].to_dict()
        histories = {
            str(code): frame.sort_values("trade_date")
            for code, frame in universe.bars.groupby("ts_code", sort=False)
        }
        rows: list[dict[str, object]] = []
        exclusions: Counter[str] = Counter()
        for code in universe.eligible_codes:
            history = histories[code].tail(30).copy()
            history["close"] = pd.to_numeric(history["close"], errors="coerce")
            history["amount"] = pd.to_numeric(history["amount"], errors="coerce")
            valid = history.dropna(subset=["close", "amount"])
            if len(valid) < 21:
                exclusions["insufficient_feature_history"] += 1
                continue
            latest = valid.iloc[-1]
            closes = valid.tail(21)["close"].astype(float).to_numpy()
            returns = pd.Series(closes).pct_change().dropna()
            high, low, close = float(latest["high"]), float(latest["low"]), float(latest["close"])
            close_location = 0.5 if high <= low else float(np.clip((close - low) / (high - low), 0.0, 1.0))
            rows.append(
                {
                    "ts_code": code,
                    "name": str(names[code]),
                    "quote_at": datetime.combine(
                        pd.Timestamp(latest["trade_date"]).date(),
                        self.schedule.market_close,
                        tzinfo=as_of.tzinfo,
                    ),
                    "ret_1d": float(closes[-1] / closes[-2] - 1.0),
                    "ret_5d": float(closes[-1] / closes[-6] - 1.0),
                    "ret_20d": float(closes[-1] / closes[-21] - 1.0),
                    "close_location": close_location,
                    "projected_volume_ratio": float(latest["amount"])
                    / max(float(valid.tail(20)["amount"].mean()), 1.0),
                    "median_amount_20d": float(valid.tail(20)["amount"].median()),
                    "volatility_20d": float(returns.tail(20).std(ddof=0)),
                }
            )
        if not rows:
            return self._empty(MORNING_FEATURE_SCHEMA, exclusions)
        exclusions["eligible_features"] = len(rows)
        frame = pd.DataFrame(rows, columns=MORNING_FEATURE_SCHEMA.columns)
        return FeatureFrame(frame, MORNING_FEATURE_SCHEMA, dict(exclusions))

    def build_preclose(
        self,
        universe: UniverseSelection,
        quotes: pd.DataFrame,
        as_of: datetime,
    ) -> FeatureFrame:
        if not universe.eligible_codes:
            return self._empty(PRECLOSE_FEATURE_SCHEMA)
        if quotes.empty:
            return self._empty(PRECLOSE_FEATURE_SCHEMA, Counter({"missing_quote": len(universe.eligible_codes)}))

        bars = universe.bars.copy()
        bars["adj_close"] = pd.to_numeric(bars["close"], errors="coerce") * pd.to_numeric(
            bars["adj_factor"], errors="coerce"
        ).fillna(1.0)
        histories = {
            str(code): frame.sort_values("trade_date") for code, frame in bars.groupby("ts_code", sort=False)
        }
        names = universe.securities.set_index("ts_code")["name"].to_dict()
        quote_latest = quotes.sort_values("captured_at").drop_duplicates("ts_code", keep="last").copy()
        quote_latest["captured_at"] = pd.to_datetime(quote_latest["captured_at"], utc=True, errors="coerce")
        as_of_utc = pd.Timestamp(as_of).tz_convert("UTC")
        quote_latest["quote_age_seconds"] = (as_of_utc - quote_latest["captured_at"]).dt.total_seconds().abs()
        quote_map = quote_latest.set_index("ts_code").to_dict("index")
        minutes_elapsed = max(trading_minutes_elapsed(as_of, self.schedule), 1)
        session_fraction = min(minutes_elapsed / 240.0, 1.0)
        rows: list[dict[str, object]] = []
        exclusions: Counter[str] = Counter()

        for code in universe.eligible_codes:
            quote = quote_map.get(code)
            if quote is None:
                exclusions["missing_quote"] += 1
                continue
            quote_age = float(quote["quote_age_seconds"])
            if not np.isfinite(quote_age) or quote_age > self.quote_freshness_seconds:
                exclusions["stale_quote"] += 1
                continue
            history = histories[code].tail(30)
            valid = history.dropna(subset=["adj_close", "amount"])
            if len(valid) < 21:
                exclusions["insufficient_feature_history"] += 1
                continue
            recent = valid.tail(21)
            price = float(quote["price"])
            open_price = float(quote["open"])
            high = float(quote["high"])
            low = float(quote["low"])
            pre_close = float(quote["pre_close"])
            volume = float(quote["volume"])
            amount = float(quote["amount"])
            if min(price, open_price, high, low, pre_close) <= 0 or volume <= 0 or amount <= 0:
                exclusions["invalid_quote"] += 1
                continue
            theoretical_up = round(pre_close * 1.10 + 1e-8, 2)
            theoretical_down = round(pre_close * 0.90 + 1e-8, 2)
            if price >= theoretical_up * (1 - self.limit_distance_fraction):
                exclusions["near_up_limit"] += 1
                continue
            if price <= theoretical_down * (1 + self.limit_distance_fraction):
                exclusions["near_down_limit"] += 1
                continue

            closes = recent["adj_close"].astype(float).to_numpy()
            daily_returns = pd.Series(closes).pct_change().dropna()
            intraday = price / open_price - 1.0
            close_location = 0.5 if high <= low else float(np.clip((price - low) / (high - low), 0.0, 1.0))
            projected_amount = amount / session_fraction
            distance = min(max(theoretical_up - price, 0.0), max(price - theoretical_down, 0.0)) / price
            rows.append(
                {
                    "ts_code": code,
                    "name": str(names[code]),
                    "quote_at": pd.Timestamp(quote["captured_at"]).to_pydatetime(),
                    "ret_5d": float(closes[-1] / closes[-6] - 1.0),
                    "ret_20d": float(closes[-1] / closes[-21] - 1.0),
                    "intraday_strength": intraday,
                    "close_location": close_location,
                    "projected_volume_ratio": projected_amount / max(float(recent.tail(20)["amount"].mean()), 1.0),
                    "median_amount_20d": float(recent.tail(20)["amount"].median()),
                    "volatility_20d": float(daily_returns.tail(20).std(ddof=0)),
                    "gap_abs": abs(open_price / pre_close - 1.0),
                    "limit_proximity": float(np.clip(1.0 - distance / 0.03, 0.0, 1.0)),
                }
            )
        if not rows:
            return self._empty(PRECLOSE_FEATURE_SCHEMA, exclusions)
        exclusions["eligible_features"] = len(rows)
        frame = pd.DataFrame(rows, columns=PRECLOSE_FEATURE_SCHEMA.columns)
        return FeatureFrame(frame, PRECLOSE_FEATURE_SCHEMA, dict(exclusions))


def trading_minutes_elapsed(at: datetime, schedule: SchedulePolicy | None = None) -> int:
    schedule = schedule or SchedulePolicy()
    local = at.timetz().replace(tzinfo=None)
    morning_start = schedule.market_morning_open
    morning_end = schedule.market_morning_close
    afternoon_start = schedule.market_afternoon_open
    afternoon_end = schedule.market_close
    if local <= morning_start:
        return 0
    if local <= morning_end:
        return int((datetime.combine(at.date(), local) - datetime.combine(at.date(), morning_start)).seconds / 60)
    if local <= afternoon_start:
        return 120
    if local <= afternoon_end:
        return 120 + int(
            (datetime.combine(at.date(), local) - datetime.combine(at.date(), afternoon_start)).seconds / 60
        )
    return 240
