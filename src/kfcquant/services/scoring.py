from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

import numpy as np
import pandas as pd

from kfcquant.config import Settings
from kfcquant.models import CandidateScore, FactorBreakdown

SH_MAIN_PREFIXES = ("600", "601", "603", "605")
SZ_MAIN_PREFIXES = ("000", "001", "002", "003")


def is_shenzhen_shanghai_main_board(ts_code: str) -> bool:
    symbol, exchange = ts_code.split(".", 1)
    if exchange == "SH":
        return symbol.startswith(SH_MAIN_PREFIXES)
    if exchange == "SZ":
        return symbol.startswith(SZ_MAIN_PREFIXES)
    return False


def trading_minutes_elapsed(at: datetime) -> int:
    local = at.timetz().replace(tzinfo=None)
    morning_start = time(9, 30)
    morning_end = time(11, 30)
    afternoon_start = time(13)
    afternoon_end = time(15)
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


def _percentile(series: pd.Series) -> pd.Series:
    if series.nunique(dropna=True) <= 1:
        return pd.Series(50.0, index=series.index)
    return series.rank(method="average", pct=True).fillna(0.0) * 100.0


@dataclass
class ScoringResult:
    candidates: list[CandidateScore]
    eligible_count: int
    exclusion_counts: dict[str, int]


class ScoringService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _raw_features(
        self,
        securities: pd.DataFrame,
        bars: pd.DataFrame,
        quotes: pd.DataFrame,
        as_of: datetime,
    ) -> tuple[pd.DataFrame, dict[str, int]]:
        exclusions: dict[str, int] = {}
        if securities.empty or bars.empty or quotes.empty:
            return pd.DataFrame(), {"missing_core_data": 1}

        securities = securities.copy()
        securities["main_board"] = securities["ts_code"].map(is_shenzhen_shanghai_main_board)
        exclusions["non_main_board"] = int((~securities["main_board"]).sum())
        securities = securities[securities["main_board"]]
        risky_name = securities["name"].fillna("").str.upper().str.contains(r"ST|退", regex=True)
        exclusions["risk_name"] = int(risky_name.sum())
        securities = securities[~risky_name]
        securities = securities[securities["list_status"] == "L"]

        bars = bars.copy()
        bars["trade_date"] = pd.to_datetime(bars["trade_date"])
        bars["adj_close"] = pd.to_numeric(bars["close"], errors="coerce") * pd.to_numeric(
            bars["adj_factor"], errors="coerce"
        ).fillna(1.0)
        bars = bars.sort_values(["ts_code", "trade_date"])
        quote_latest = quotes.sort_values("captured_at").drop_duplicates("ts_code", keep="last").copy()
        quote_latest["captured_at"] = pd.to_datetime(quote_latest["captured_at"], utc=True)
        as_of_utc = pd.Timestamp(as_of).tz_convert("UTC")
        quote_latest["quote_age_seconds"] = (as_of_utc - quote_latest["captured_at"]).dt.total_seconds().abs()

        rows: list[dict[str, float | str | datetime]] = []
        minutes_elapsed = max(trading_minutes_elapsed(as_of), 1)
        session_fraction = min(minutes_elapsed / 240.0, 1.0)
        security_names = securities.set_index("ts_code")["name"].to_dict()
        quote_map = quote_latest.set_index("ts_code").to_dict("index")
        history_counts = bars.groupby("ts_code")["trade_date"].nunique().to_dict()

        for ts_code, history in bars.groupby("ts_code", sort=False):
            if ts_code not in security_names or ts_code not in quote_map:
                continue
            history = history.tail(30)
            latest_state = history.iloc[-1]
            if bool(latest_state.get("suspended", False)) or bool(latest_state.get("is_st", False)):
                continue
            valid = history.dropna(subset=["adj_close", "amount"])
            if int(history_counts.get(ts_code, 0)) < self.settings.min_listing_trading_days:
                continue
            if len(valid) < 21:
                continue
            quote = quote_map[ts_code]
            if float(quote["quote_age_seconds"]) > self.settings.quote_freshness_seconds:
                continue
            recent = valid.tail(21)
            median_amount = float(recent.tail(20)["amount"].median())
            if not np.isfinite(median_amount) or median_amount < self.settings.min_median_amount_20d:
                continue
            price = float(quote["price"])
            open_price = float(quote["open"])
            high = float(quote["high"])
            low = float(quote["low"])
            pre_close = float(quote["pre_close"])
            volume = float(quote["volume"])
            amount = float(quote["amount"])
            if min(price, open_price, high, low, pre_close) <= 0 or volume <= 0 or amount <= 0:
                continue
            theoretical_up = round(pre_close * 1.10 + 1e-8, 2)
            theoretical_down = round(pre_close * 0.90 + 1e-8, 2)
            if price >= theoretical_up * (1 - self.settings.limit_distance_fraction):
                continue
            if price <= theoretical_down * (1 + self.settings.limit_distance_fraction):
                continue

            closes = recent["adj_close"].astype(float).to_numpy()
            daily_returns = pd.Series(closes).pct_change().dropna()
            ret_5d = float(closes[-1] / closes[-6] - 1.0)
            ret_20d = float(closes[-1] / closes[-21] - 1.0)
            intraday = price / open_price - 1.0
            close_location = 0.5 if high <= low else float(np.clip((price - low) / (high - low), 0.0, 1.0))
            projected_amount = amount / session_fraction
            projected_ratio = projected_amount / max(float(recent.tail(20)["amount"].mean()), 1.0)
            volatility = float(daily_returns.tail(20).std(ddof=0))
            gap_abs = abs(open_price / pre_close - 1.0)
            distance = min(max(theoretical_up - price, 0.0), max(price - theoretical_down, 0.0)) / price
            limit_proximity = float(np.clip(1.0 - distance / 0.03, 0.0, 1.0))
            rows.append(
                {
                    "ts_code": ts_code,
                    "name": security_names[ts_code],
                    "quote_at": pd.Timestamp(quote["captured_at"]).to_pydatetime(),
                    "ret_5d": ret_5d,
                    "ret_20d": ret_20d,
                    "intraday_strength": intraday,
                    "close_location": close_location,
                    "projected_volume_ratio": projected_ratio,
                    "median_amount_20d": median_amount,
                    "volatility_20d": volatility,
                    "gap_abs": gap_abs,
                    "limit_proximity": limit_proximity,
                }
            )

        features = pd.DataFrame(rows)
        exclusions["eligible"] = len(features)
        return features, exclusions

    def score(
        self,
        run_id: str,
        securities: pd.DataFrame,
        bars: pd.DataFrame,
        quotes: pd.DataFrame,
        as_of: datetime,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: set[str] | None = None,
        morning_codes: set[str] | None = None,
    ) -> ScoringResult:
        frame, exclusions = self._raw_features(securities, bars, quotes, as_of)
        if frame.empty:
            return ScoringResult([], 0, exclusions)

        positive = (
            0.25 * _percentile(frame["ret_5d"])
            + 0.15 * _percentile(frame["ret_20d"])
            + 0.20 * _percentile(frame["intraday_strength"])
            + 0.15 * _percentile(frame["close_location"])
            + 0.15 * _percentile(frame["projected_volume_ratio"])
            + 0.10 * _percentile(np.log1p(frame["median_amount_20d"]))
        )
        abnormal_volume = np.clip((frame["projected_volume_ratio"] - 3.0) / 2.0, 0.0, 1.0)
        penalty = (
            8.0 * _percentile(frame["volatility_20d"]) / 100.0
            + 4.0 * _percentile(frame["gap_abs"]) / 100.0
            + 4.0 * frame["limit_proximity"]
            + 4.0 * abnormal_volume
        )
        frame["positive_score"] = positive
        frame["risk_penalty"] = np.clip(penalty, 0.0, 20.0)
        frame["technical_score"] = np.clip(0.9 * (frame["positive_score"] - frame["risk_penalty"]), 0.0, 90.0)

        events_by_code: dict[str, list[dict[str, object]]] = {}
        if risk_events is not None and not risk_events.empty:
            for event in risk_events.to_dict("records"):
                code = event.get("ts_code")
                if code:
                    events_by_code.setdefault(str(code), []).append(event)
        unprocessed = unprocessed_official_codes or set()
        morning = morning_codes or set()

        staged: list[dict[str, object]] = []
        for row in frame.to_dict("records"):
            code = str(row["ts_code"])
            related = events_by_code.get(code, [])
            positive_events = [event for event in related if str(event.get("direction")) == "positive"]
            negative_events = [event for event in related if str(event.get("direction")) == "negative"]
            news_score = min(sum(float(event.get("confidence", 0.0)) * 2.5 for event in positive_events), 7.0)
            news_penalty = min(
                sum(
                    {"low": 1.0, "medium": 3.0, "high": 7.0, "critical": 15.0}.get(str(event.get("severity")), 0.0)
                    for event in negative_events
                ),
                20.0,
            )
            confirmed = code in morning and float(row["intraday_strength"]) > 0 and float(row["close_location"]) >= 0.5
            row["news_score"] = news_score
            row["continuity_score"] = 3.0 if confirmed else 0.0
            row["morning_status"] = "confirmed" if confirmed else ("invalidated" if code in morning else "new")
            row["opportunity_score"] = float(
                np.clip(row["technical_score"] + news_score + row["continuity_score"] - news_penalty, 0.0, 100.0)
            )
            reasons: list[str] = []
            if code in unprocessed:
                reasons.append("存在尚未完成抽取的官方公告")
            for event in related:
                if bool(event.get("hard_block")):
                    reasons.append(f"{event.get('event_type')}: {event.get('evidence')}")
            row["blocked"] = bool(reasons)
            row["block_reasons"] = reasons
            row["risk_event_ids"] = [str(event["event_id"]) for event in related]
            staged.append(row)

        unblocked = [row for row in staged if not row["blocked"]]
        blocked = [row for row in staged if row["blocked"]]
        ordered = unblocked + blocked
        candidates: list[CandidateScore] = []
        ordered.sort(key=lambda item: (bool(item["blocked"]), -float(item["opportunity_score"]), str(item["ts_code"])))
        for index, row in enumerate(ordered, start=1):
            breakdown = FactorBreakdown.model_validate(
                {field: row[field] for field in FactorBreakdown.model_fields if field in row}
            )
            candidates.append(
                CandidateScore(
                    run_id=run_id,
                    ts_code=str(row["ts_code"]),
                    name=str(row["name"]),
                    rank=index,
                    opportunity_score=round(float(row["opportunity_score"]), 4),
                    factor_breakdown=breakdown,
                    risk_event_ids=list(row["risk_event_ids"]),
                    blocked=bool(row["blocked"]),
                    block_reasons=list(row["block_reasons"]),
                    quote_at=row["quote_at"],
                )
            )
        return ScoringResult(candidates=candidates[:100], eligible_count=len(frame), exclusion_counts=exclusions)

    def score_morning(
        self,
        run_id: str,
        securities: pd.DataFrame,
        bars: pd.DataFrame,
        as_of: datetime,
        risk_events: pd.DataFrame | None = None,
        unprocessed_official_codes: set[str] | None = None,
    ) -> ScoringResult:
        if securities.empty or bars.empty:
            return ScoringResult([], 0, {"missing_core_data": 1})
        securities = securities.copy()
        securities = securities[securities["ts_code"].map(is_shenzhen_shanghai_main_board)]
        securities = securities[~securities["name"].fillna("").str.upper().str.contains(r"ST|退", regex=True)]
        securities = securities[securities["list_status"] == "L"]
        names = securities.set_index("ts_code")["name"].to_dict()
        bars = bars.copy().sort_values(["ts_code", "trade_date"])
        rows: list[dict[str, object]] = []
        for code, history in bars.groupby("ts_code", sort=False):
            if code not in names or history["trade_date"].nunique() < self.settings.min_listing_trading_days:
                continue
            history = history.tail(30).copy()
            valid = history.dropna(subset=["close", "amount"])
            if len(valid) < 21:
                continue
            latest = valid.iloc[-1]
            if bool(latest.get("suspended", False)) or bool(latest.get("is_st", False)):
                continue
            median_amount = float(valid.tail(20)["amount"].median())
            if not np.isfinite(median_amount) or median_amount < self.settings.min_median_amount_20d:
                continue
            closes = valid.tail(21)["close"].astype(float).to_numpy()
            returns = pd.Series(closes).pct_change().dropna()
            high, low, close = float(latest["high"]), float(latest["low"]), float(latest["close"])
            close_location = 0.5 if high <= low else float(np.clip((close - low) / (high - low), 0.0, 1.0))
            rows.append(
                {
                    "ts_code": str(code),
                    "name": str(names[code]),
                    "quote_at": datetime.combine(
                        pd.Timestamp(latest["trade_date"]).date(), time(15), tzinfo=as_of.tzinfo
                    ),
                    "ret_1d": float(closes[-1] / closes[-2] - 1.0),
                    "ret_5d": float(closes[-1] / closes[-6] - 1.0),
                    "ret_20d": float(closes[-1] / closes[-21] - 1.0),
                    "close_location": close_location,
                    "projected_volume_ratio": float(latest["amount"])
                    / max(float(valid.tail(20)["amount"].mean()), 1.0),
                    "median_amount_20d": median_amount,
                    "volatility_20d": float(returns.tail(20).std(ddof=0)),
                }
            )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return ScoringResult([], 0, {"eligible": 0})
        base = (
            0.20 * _percentile(frame["ret_1d"])
            + 0.25 * _percentile(frame["ret_5d"])
            + 0.15 * _percentile(frame["ret_20d"])
            + 0.15 * _percentile(frame["close_location"])
            + 0.15 * _percentile(frame["projected_volume_ratio"])
            + 0.10 * _percentile(np.log1p(frame["median_amount_20d"]))
        )
        volatility_penalty = 10.0 * _percentile(frame["volatility_20d"]) / 100.0
        frame["positive_score"] = base
        frame["risk_penalty"] = volatility_penalty
        frame["technical_score"] = np.clip(0.9 * (base - volatility_penalty), 0.0, 90.0)

        events_by_code: dict[str, list[dict[str, object]]] = {}
        if risk_events is not None and not risk_events.empty:
            for event in risk_events.to_dict("records"):
                if event.get("ts_code"):
                    events_by_code.setdefault(str(event["ts_code"]), []).append(event)
        unprocessed = unprocessed_official_codes or set()
        staged: list[dict[str, object]] = []
        for row in frame.to_dict("records"):
            code = str(row["ts_code"])
            related = events_by_code.get(code, [])
            positives = [event for event in related if str(event.get("direction")) == "positive"]
            negatives = [event for event in related if str(event.get("direction")) == "negative"]
            row["news_score"] = min(sum(float(event.get("confidence", 0.0)) * 3.0 for event in positives), 10.0)
            negative_penalty = min(
                sum(
                    {"low": 1.0, "medium": 3.0, "high": 7.0, "critical": 15.0}.get(str(event.get("severity")), 0.0)
                    for event in negatives
                ),
                20.0,
            )
            reasons = [
                f"{event.get('event_type')}: {event.get('evidence')}" for event in related if event.get("hard_block")
            ]
            if code in unprocessed:
                reasons.append("存在尚未完成抽取的官方公告")
            row["opportunity_score"] = float(
                np.clip(row["technical_score"] + row["news_score"] - negative_penalty, 0.0, 100.0)
            )
            row["blocked"] = bool(reasons)
            row["block_reasons"] = reasons
            row["risk_event_ids"] = [str(event["event_id"]) for event in related]
            staged.append(row)
        staged.sort(key=lambda item: (bool(item["blocked"]), -float(item["opportunity_score"]), str(item["ts_code"])))
        candidates: list[CandidateScore] = []
        for rank, row in enumerate(staged, start=1):
            candidates.append(
                CandidateScore(
                    run_id=run_id,
                    ts_code=str(row["ts_code"]),
                    name=str(row["name"]),
                    rank=rank,
                    opportunity_score=round(float(row["opportunity_score"]), 4),
                    factor_breakdown=FactorBreakdown.model_validate(
                        {field: row[field] for field in FactorBreakdown.model_fields if field in row}
                    ),
                    risk_event_ids=list(row["risk_event_ids"]),
                    blocked=bool(row["blocked"]),
                    block_reasons=list(row["block_reasons"]),
                    quote_at=row["quote_at"],
                )
            )
        return ScoringResult(candidates[:100], len(frame), {"eligible": len(frame)})
