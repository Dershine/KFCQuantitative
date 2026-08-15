from __future__ import annotations

import numpy as np
import pandas as pd


def _percentile(series: pd.Series) -> pd.Series:
    if series.nunique(dropna=True) <= 1:
        return pd.Series(50.0, index=series.index)
    return series.rank(method="average", pct=True).fillna(0.0) * 100.0


class ScoreModel:
    """Deterministic technical scoring over versioned market features only."""

    def score_preclose(self, features: pd.DataFrame) -> pd.DataFrame:
        frame = features.copy()
        if frame.empty:
            return frame
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
        frame["technical_score"] = np.clip(
            0.9 * (frame["positive_score"] - frame["risk_penalty"]), 0.0, 90.0
        )
        return frame

    def score_morning(self, features: pd.DataFrame) -> pd.DataFrame:
        frame = features.copy()
        if frame.empty:
            return frame
        positive = (
            0.20 * _percentile(frame["ret_1d"])
            + 0.25 * _percentile(frame["ret_5d"])
            + 0.15 * _percentile(frame["ret_20d"])
            + 0.15 * _percentile(frame["close_location"])
            + 0.15 * _percentile(frame["projected_volume_ratio"])
            + 0.10 * _percentile(np.log1p(frame["median_amount_20d"]))
        )
        volatility_penalty = 10.0 * _percentile(frame["volatility_20d"]) / 100.0
        frame["positive_score"] = positive
        frame["risk_penalty"] = volatility_penalty
        frame["technical_score"] = np.clip(0.9 * (positive - volatility_penalty), 0.0, 90.0)
        return frame
