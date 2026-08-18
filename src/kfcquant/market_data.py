from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from numbers import Real

import numpy as np
import pandas as pd


class MarketDataValidationError(ValueError):
    """A normalized market-data batch violated its declared boundary contract."""


class LogicalType(StrEnum):
    STRING = "string"
    DATE = "date"
    DATETIME = "datetime"
    NUMBER = "number"
    BOOLEAN = "boolean"


def _is_null(value: object) -> bool:
    result = pd.isna(value)
    return bool(result) if isinstance(result, (bool, np.bool_)) else False


@dataclass(frozen=True, slots=True)
class MarketColumn:
    name: str
    logical_type: LogicalType
    nullable: bool = False
    unit: str | None = None
    pattern: str | None = None
    allowed_values: frozenset[str] | None = None
    minimum: float | None = None
    minimum_inclusive: bool = True

    def validate(self, series: pd.Series, schema_version: str) -> None:
        values = series.tolist()
        null_count = sum(_is_null(value) for value in values)
        if null_count and not self.nullable:
            raise MarketDataValidationError(
                f"{schema_version}.{self.name} is not nullable; found {null_count} null values"
            )
        present = [value for value in values if not _is_null(value)]
        if self.logical_type == LogicalType.STRING:
            invalid = [value for value in present if not isinstance(value, str) or not value.strip()]
            type_message = "non-empty strings"
        elif self.logical_type == LogicalType.DATE:
            invalid = [value for value in present if not isinstance(value, date) or isinstance(value, datetime)]
            type_message = "date values without a time component"
        elif self.logical_type == LogicalType.DATETIME:
            invalid = [
                value
                for value in present
                if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
            ]
            type_message = "timezone-aware datetimes"
        elif self.logical_type == LogicalType.NUMBER:
            invalid = [
                value
                for value in present
                if isinstance(value, (bool, np.bool_))
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
            ]
            type_message = "finite numeric values"
        else:
            invalid = [value for value in present if not isinstance(value, (bool, np.bool_))]
            type_message = "boolean values"
        if invalid:
            raise MarketDataValidationError(
                f"{schema_version}.{self.name} must contain {type_message}; sample={invalid[:3]!r}"
            )

        if self.pattern is not None:
            invalid = [value for value in present if re.fullmatch(self.pattern, str(value)) is None]
            if invalid:
                raise MarketDataValidationError(
                    f"{schema_version}.{self.name} does not match {self.pattern}; sample={invalid[:3]!r}"
                )
        if self.allowed_values is not None:
            invalid = [value for value in present if str(value) not in self.allowed_values]
            if invalid:
                raise MarketDataValidationError(
                    f"{schema_version}.{self.name} contains values outside "
                    f"{sorted(self.allowed_values)}; sample={invalid[:3]!r}"
                )
        if self.minimum is not None:
            invalid = [
                value
                for value in present
                if (
                    float(value) < self.minimum
                    if self.minimum_inclusive
                    else float(value) <= self.minimum
                )
            ]
            if invalid:
                comparator = "greater than or equal to" if self.minimum_inclusive else "greater than"
                raise MarketDataValidationError(
                    f"{schema_version}.{self.name} must be {comparator} {self.minimum}; "
                    f"sample={invalid[:3]!r}"
                )


@dataclass(frozen=True, slots=True)
class MarketRowRule:
    name: str
    predicate: Callable[[pd.DataFrame], pd.Series]


@dataclass(frozen=True, slots=True)
class ValidatedMarketFrame:
    schema: MarketTableSchema
    frame: pd.DataFrame

    @property
    def row_count(self) -> int:
        return len(self.frame)


@dataclass(frozen=True, slots=True)
class MarketTableSchema:
    name: str
    version: str
    fields: tuple[MarketColumn, ...]
    unique_key: tuple[str, ...]
    row_rules: tuple[MarketRowRule, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    @property
    def units(self) -> dict[str, str]:
        return {field.name: field.unit for field in self.fields if field.unit is not None}

    def empty_frame(self) -> pd.DataFrame:
        return pd.DataFrame(columns=self.columns)

    def validate(self, frame: pd.DataFrame) -> ValidatedMarketFrame:
        if not isinstance(frame, pd.DataFrame):
            raise MarketDataValidationError(f"{self.version} requires a pandas DataFrame")
        if frame.empty and len(frame.columns) == 0:
            return ValidatedMarketFrame(self, self.empty_frame())

        duplicate_columns = frame.columns[frame.columns.duplicated()].astype(str).tolist()
        if duplicate_columns:
            raise MarketDataValidationError(
                f"{self.version} contains duplicate columns: {', '.join(duplicate_columns)}"
            )

        actual = set(frame.columns)
        expected = set(self.columns)
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if missing:
            raise MarketDataValidationError(f"{self.version} is missing columns: {', '.join(missing)}")
        if unexpected:
            raise MarketDataValidationError(
                f"{self.version} contains unexpected columns: {', '.join(unexpected)}"
            )

        normalized = frame.loc[:, self.columns].copy()
        for field in self.fields:
            field.validate(normalized[field.name], self.version)

        duplicates = normalized.duplicated(list(self.unique_key), keep=False)
        if duplicates.any():
            sample = normalized.loc[duplicates, list(self.unique_key)].head(3).to_dict("records")
            raise MarketDataValidationError(
                f"{self.version} contains a duplicate unique key {self.unique_key}; sample={sample!r}"
            )

        for rule in self.row_rules:
            valid = rule.predicate(normalized)
            if not isinstance(valid, pd.Series) or len(valid) != len(normalized):
                raise TypeError(f"market row rule {rule.name} did not return a row-aligned Series")
            invalid = ~valid.fillna(False).astype(bool)
            if invalid.any():
                sample = normalized.loc[invalid, list(self.unique_key)].head(3).to_dict("records")
                raise MarketDataValidationError(
                    f"{self.version} violates row rule {rule.name}; sample={sample!r}"
                )
        return ValidatedMarketFrame(self, normalized.reset_index(drop=True))


_TS_CODE = r"\d{6}\.(SH|SZ|BJ)"
_SYMBOL = r"\d{6}"


def _security_identity(frame: pd.DataFrame) -> pd.Series:
    return (
        frame["ts_code"].str.slice(0, 6).eq(frame["symbol"])
        & frame["ts_code"].str.rsplit(".", n=1).str[-1].eq(frame["exchange"])
    )


def _delist_on_or_after_list(frame: pd.DataFrame) -> pd.Series:
    return frame["delist_date"].isna() | frame.apply(
        lambda row: row["delist_date"] >= row["list_date"] if not _is_null(row["delist_date"]) else True,
        axis=1,
    )


def _previous_trade_before_date(frame: pd.DataFrame) -> pd.Series:
    return frame["pretrade_date"].isna() | frame.apply(
        lambda row: row["pretrade_date"] < row["cal_date"]
        if not _is_null(row["pretrade_date"])
        else True,
        axis=1,
    )


def _ohlc_bounds(frame: pd.DataFrame, current: str) -> pd.Series:
    upper = frame[["open", "high", "low", current]].max(axis=1)
    lower = frame[["open", "high", "low", current]].min(axis=1)
    return frame["high"].ge(upper) & frame["low"].le(lower)


def _limits_ordered(frame: pd.DataFrame) -> pd.Series:
    incomplete = frame["up_limit"].isna() | frame["down_limit"].isna()
    return incomplete | frame["up_limit"].gt(frame["down_limit"])


def _quote_price_state(frame: pd.DataFrame) -> pd.Series:
    regular = (
        frame[["price", "open", "high", "low"]].gt(0).all(axis=1)
        & _ohlc_bounds(frame, "price")
    )
    inactive = (
        frame[["open", "high", "low", "volume", "amount"]].eq(0).all(axis=1)
        & frame["price"].ge(0)
    )
    return regular | inactive


SECURITY_SCHEMA = MarketTableSchema(
    name="security",
    version="security-v1",
    fields=(
        MarketColumn("ts_code", LogicalType.STRING, pattern=_TS_CODE),
        MarketColumn("symbol", LogicalType.STRING, pattern=_SYMBOL),
        MarketColumn("name", LogicalType.STRING),
        MarketColumn("exchange", LogicalType.STRING, allowed_values=frozenset({"SH", "SZ", "BJ"})),
        MarketColumn("market", LogicalType.STRING, nullable=True),
        MarketColumn("list_date", LogicalType.DATE),
        MarketColumn("delist_date", LogicalType.DATE, nullable=True),
        MarketColumn("list_status", LogicalType.STRING, allowed_values=frozenset({"L", "D", "P"})),
    ),
    unique_key=("ts_code",),
    row_rules=(
        MarketRowRule("symbol_and_exchange_match_code", _security_identity),
        MarketRowRule("delist_on_or_after_list", _delist_on_or_after_list),
    ),
)

TRADE_CALENDAR_SCHEMA = MarketTableSchema(
    name="trade_calendar",
    version="trade-calendar-v1",
    fields=(
        MarketColumn("cal_date", LogicalType.DATE),
        MarketColumn("is_open", LogicalType.BOOLEAN),
        MarketColumn("pretrade_date", LogicalType.DATE, nullable=True),
    ),
    unique_key=("cal_date",),
    row_rules=(MarketRowRule("previous_trade_before_date", _previous_trade_before_date),),
)

DAILY_BAR_SCHEMA = MarketTableSchema(
    name="daily_bar",
    version="daily-bar-v1",
    fields=(
        MarketColumn("ts_code", LogicalType.STRING, pattern=_TS_CODE),
        MarketColumn("trade_date", LogicalType.DATE),
        MarketColumn("open", LogicalType.NUMBER, unit="CNY/share", minimum=0.0, minimum_inclusive=False),
        MarketColumn("high", LogicalType.NUMBER, unit="CNY/share", minimum=0.0, minimum_inclusive=False),
        MarketColumn("low", LogicalType.NUMBER, unit="CNY/share", minimum=0.0, minimum_inclusive=False),
        MarketColumn("close", LogicalType.NUMBER, unit="CNY/share", minimum=0.0, minimum_inclusive=False),
        MarketColumn(
            "pre_close",
            LogicalType.NUMBER,
            nullable=True,
            unit="CNY/share",
            minimum=0.0,
            minimum_inclusive=False,
        ),
        MarketColumn("volume", LogicalType.NUMBER, unit="share", minimum=0.0),
        MarketColumn("amount", LogicalType.NUMBER, unit="CNY", minimum=0.0),
        MarketColumn("adj_factor", LogicalType.NUMBER, unit="ratio", minimum=0.0, minimum_inclusive=False),
        MarketColumn(
            "up_limit",
            LogicalType.NUMBER,
            nullable=True,
            unit="CNY/share",
            minimum=0.0,
            minimum_inclusive=False,
        ),
        MarketColumn(
            "down_limit",
            LogicalType.NUMBER,
            nullable=True,
            unit="CNY/share",
            minimum=0.0,
            minimum_inclusive=False,
        ),
        MarketColumn("suspended", LogicalType.BOOLEAN),
        MarketColumn("is_st", LogicalType.BOOLEAN),
    ),
    unique_key=("ts_code", "trade_date"),
    row_rules=(
        MarketRowRule("ohlc_bounds", lambda frame: _ohlc_bounds(frame, "close")),
        MarketRowRule("price_limits_ordered", _limits_ordered),
    ),
)

LIVE_QUOTE_SCHEMA = MarketTableSchema(
    name="live_quote",
    version="live-quote-v1",
    fields=(
        MarketColumn("ts_code", LogicalType.STRING, pattern=_TS_CODE),
        MarketColumn("captured_at", LogicalType.DATETIME),
        MarketColumn("price", LogicalType.NUMBER, unit="CNY/share", minimum=0.0),
        MarketColumn("open", LogicalType.NUMBER, unit="CNY/share", minimum=0.0),
        MarketColumn("high", LogicalType.NUMBER, unit="CNY/share", minimum=0.0),
        MarketColumn("low", LogicalType.NUMBER, unit="CNY/share", minimum=0.0),
        MarketColumn(
            "pre_close", LogicalType.NUMBER, unit="CNY/share", minimum=0.0, minimum_inclusive=False
        ),
        MarketColumn("volume", LogicalType.NUMBER, unit="share", minimum=0.0),
        MarketColumn("amount", LogicalType.NUMBER, unit="CNY", minimum=0.0),
        MarketColumn("source", LogicalType.STRING),
    ),
    unique_key=("ts_code", "captured_at"),
    row_rules=(MarketRowRule("ohlc_bounds_or_inactive_zero_state", _quote_price_state),),
)
