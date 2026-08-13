from __future__ import annotations

import hashlib
from datetime import datetime

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import Database
from kfcquant.models import NewsDocument, SourceTier
from kfcquant.services.news import NewsService
from kfcquant.services.reports import ReportService


class EmptyProvider:
    def fetch_official_documents(self, start, end):
        return []

    def fetch_mainstream_documents(self, start, end):
        return []


class NoDownload:
    def load_text(self, url):
        raise AssertionError("download should not be called")


def document(title: str, code: str, at: datetime) -> NewsDocument:
    return NewsDocument(
        ts_code=code,
        title=title,
        published_at=at,
        source="fixture",
        source_tier=SourceTier.OFFICIAL,
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
        fetched_at=at,
    )


def test_benign_title_is_processed_without_llm(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    database.save_news_documents([document("关于召开年度股东大会的通知", "600000.SH", at)])
    service = NewsService(database, EmptyProvider(), None, NoDownload())
    assert service.process_pending() == (1, 0)
    assert database.unprocessed_official_codes(at.replace(hour=0), at) == set()


def test_risk_title_without_llm_fails_closed_for_affected_stock(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    database.save_news_documents([document("关于收到立案调查通知书的公告", "600000.SH", at)])
    service = NewsService(database, EmptyProvider(), None, NoDownload())
    assert service.process_pending() == (0, 1)
    assert database.unprocessed_official_codes(at.replace(hour=0), at) == {"600000.SH"}


def test_report_fallback_is_persisted(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    at = datetime(2026, 8, 10, 20, 30, tzinfo=SHANGHAI_TZ)
    content = ReportService(database, None, settings.report_dir, "qwen-plus").generate(
        at.date(),
        at,
        {
            "report_date": at.date().isoformat(),
            "candidates": [],
            "after_entry_events": [],
            "positions": [],
            "cash": 100000,
        },
    )
    assert "不构成投资建议" in content
    assert (settings.report_dir / "2026-08-10-postclose.md").exists()
    reports = database.table("reports")
    assert reports.iloc[0]["model_name"] == "deterministic-fallback"
