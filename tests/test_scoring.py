from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.services.scoring import ScoringService, is_shenzhen_shanghai_main_board
from tests.conftest import make_daily, make_quotes, make_securities


def test_main_board_filter():
    assert is_shenzhen_shanghai_main_board("600000.SH")
    assert is_shenzhen_shanghai_main_board("000001.SZ")
    assert not is_shenzhen_shanghai_main_board("688001.SH")
    assert not is_shenzhen_shanghai_main_board("300001.SZ")
    assert not is_shenzhen_shanghai_main_board("830001.BJ")


def test_scoring_is_deterministic_and_excludes_other_boards(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ", "002001.SZ", "300001.SZ"]
    securities = make_securities([(code, f"公司{index}") for index, code in enumerate(codes)])
    bars = make_daily(codes, at)
    quotes = make_quotes(codes, at)
    service = ScoringService(settings)

    first = service.score("run-1", securities, bars, quotes, at)
    second = service.score("run-1", securities, bars, quotes, at)

    assert first.eligible_count == 3
    assert [item.ts_code for item in first.candidates] == [item.ts_code for item in second.candidates]
    assert [item.opportunity_score for item in first.candidates] == [
        item.opportunity_score for item in second.candidates
    ]
    assert "300001.SZ" not in {item.ts_code for item in first.candidates}


def test_hard_risk_event_blocks_candidate(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "000001.SZ"]
    events = pd.DataFrame(
        [
            {
                "event_id": "event-1",
                "ts_code": "600000.SH",
                "hard_block": True,
                "event_type": "regulatory_investigation",
                "evidence": "立案调查",
            }
        ]
    )
    result = ScoringService(settings).score(
        "run-risk",
        make_securities([(code, code) for code in codes]),
        make_daily(codes, at),
        make_quotes(codes, at),
        at,
        events,
    )
    blocked = {item.ts_code: item for item in result.candidates}
    assert blocked["600000.SH"].blocked
    assert not blocked["000001.SZ"].blocked


def test_stale_quotes_produce_no_candidates(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH"]
    quotes = make_quotes(codes, at - timedelta(minutes=2))
    result = ScoringService(settings).score(
        "stale", make_securities([(codes[0], "公司")]), make_daily(codes, at), quotes, at
    )
    assert result.candidates == []


def test_latest_historical_st_state_is_excluded(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    code = "600000.SH"
    bars = make_daily([code], at)
    bars["is_st"] = False
    bars.loc[bars["trade_date"] == bars["trade_date"].max(), "is_st"] = True
    result = ScoringService(settings).score(
        "historical-st",
        make_securities([(code, "普通名称")]),
        bars,
        make_quotes([code], at),
        at,
    )
    assert result.candidates == []


def test_scoring_uses_shared_candidate_limit(settings):
    at = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    codes = ["600000.SH", "601001.SH", "603001.SH", "605001.SH"]
    configured = settings.model_copy(
        update={"selection": settings.selection.model_copy(update={"top_n": 2, "candidate_limit": 3})}
    )

    result = ScoringService(configured).score(
        "limited",
        make_securities([(code, code) for code in codes]),
        make_daily(codes, at),
        make_quotes(codes, at),
        at,
    )

    assert len(result.candidates) == 3
