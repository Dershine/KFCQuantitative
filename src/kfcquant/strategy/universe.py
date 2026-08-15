from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from kfcquant.config import Settings

SH_MAIN_PREFIXES = ("600", "601", "603", "605")
SZ_MAIN_PREFIXES = ("000", "001", "002", "003")


def is_shenzhen_shanghai_main_board(ts_code: str) -> bool:
    try:
        symbol, exchange = ts_code.split(".", 1)
    except ValueError:
        return False
    if exchange == "SH":
        return symbol.startswith(SH_MAIN_PREFIXES)
    if exchange == "SZ":
        return symbol.startswith(SZ_MAIN_PREFIXES)
    return False


@dataclass(frozen=True, slots=True)
class UniverseSelection:
    """The auditable output of stock-pool eligibility rules."""

    securities: pd.DataFrame
    bars: pd.DataFrame
    eligible_codes: tuple[str, ...]
    exclusion_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class UniversePolicy:
    """Selects the stock pool without calculating features, scores, or risk."""

    min_listing_trading_days: int
    min_median_amount_20d: float

    @classmethod
    def from_settings(cls, settings: Settings) -> UniversePolicy:
        return cls(
            min_listing_trading_days=settings.min_listing_trading_days,
            min_median_amount_20d=settings.min_median_amount_20d,
        )

    def select(self, securities: pd.DataFrame, bars: pd.DataFrame) -> UniverseSelection:
        if securities.empty or bars.empty:
            return UniverseSelection(
                pd.DataFrame(columns=securities.columns),
                pd.DataFrame(columns=bars.columns),
                (),
                {"missing_core_data": 1, "eligible": 0},
            )

        security_rows = securities.drop_duplicates("ts_code", keep="last").copy()
        history = bars.copy()
        history["trade_date"] = pd.to_datetime(history["trade_date"], errors="coerce")
        history = history.sort_values(["ts_code", "trade_date"])
        histories = {str(code): frame for code, frame in history.groupby("ts_code", sort=False)}
        exclusions: Counter[str] = Counter()
        eligible: list[str] = []

        for security in security_rows.to_dict("records"):
            code = str(security["ts_code"])
            name = str(security.get("name") or "")
            if not is_shenzhen_shanghai_main_board(code):
                exclusions["non_main_board"] += 1
                continue
            if "ST" in name.upper() or "退" in name:
                exclusions["risk_name"] += 1
                continue
            if str(security.get("list_status")) != "L":
                exclusions["inactive_listing"] += 1
                continue

            security_history = histories.get(code)
            if security_history is None or security_history.empty:
                exclusions["missing_history"] += 1
                continue
            latest = security_history.iloc[-1]
            if bool(latest.get("suspended", False)):
                exclusions["suspended"] += 1
                continue
            if bool(latest.get("is_st", False)):
                exclusions["historical_st"] += 1
                continue
            if security_history["trade_date"].nunique() < self.min_listing_trading_days:
                exclusions["insufficient_listing_history"] += 1
                continue

            amounts = pd.to_numeric(security_history.tail(20)["amount"], errors="coerce")
            median_amount = float(amounts.median())
            if len(amounts.dropna()) < 20 or not np.isfinite(median_amount):
                exclusions["insufficient_liquidity_history"] += 1
                continue
            if median_amount < self.min_median_amount_20d:
                exclusions["insufficient_liquidity"] += 1
                continue
            eligible.append(code)

        exclusions["eligible"] = len(eligible)
        eligible_set = set(eligible)
        selected_securities = security_rows[security_rows["ts_code"].astype(str).isin(eligible_set)].copy()
        selected_bars = history[history["ts_code"].astype(str).isin(eligible_set)].copy()
        return UniverseSelection(
            securities=selected_securities,
            bars=selected_bars,
            eligible_codes=tuple(eligible),
            exclusion_counts=dict(exclusions),
        )
