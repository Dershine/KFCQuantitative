from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from kfcquant.config import SHANGHAI_TZ
from kfcquant.models import NewsDocument, SourceTier


def _to_ts_code(symbol: str) -> str:
    symbol = str(symbol).zfill(6)
    return f"{symbol}.SH" if symbol.startswith("6") else f"{symbol}.SZ"


def _is_main_board(ts_code: str) -> bool:
    symbol, exchange = ts_code.split(".", 1)
    return (exchange == "SH" and symbol.startswith(("600", "601", "603", "605"))) or (
        exchange == "SZ" and symbol.startswith(("000", "001", "002", "003"))
    )


class AkShareNewsProvider:
    """Free announcement mirror and public-news adapter for the learning profile."""

    source_name = "akshare"

    def __init__(self, client: Any | None = None):
        if client is None:
            import akshare as client

        self.client = client

    @staticmethod
    def _hash(source: str, title: str, url: str | None, published_at: datetime) -> str:
        payload = "|".join((source, title, url or "", published_at.isoformat()))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _announcement_time(day: date, requested_end: datetime) -> datetime:
        # The free mirror exposes only a date. Same-day records remain invisible
        # to 14:40 and become after-entry events in the post-close run.
        if day == requested_end.date() and requested_end.time() < time(15, 1):
            return datetime.combine(day, time(23, 59, 59), tzinfo=SHANGHAI_TZ)
        return datetime.combine(day, time(15, 1), tzinfo=SHANGHAI_TZ)

    def fetch_official_documents(self, start: datetime, end: datetime) -> list[NewsDocument]:
        fetched_at = datetime.now(SHANGHAI_TZ)
        documents: list[NewsDocument] = []
        day = start.date()
        while day <= end.date():
            frame = self.client.stock_notice_report(symbol="全部", date=day.strftime("%Y%m%d"))
            required = {"代码", "公告标题", "公告日期", "网址"}
            missing = required.difference(frame.columns)
            if missing:
                raise RuntimeError(f"AKShare announcement schema changed; missing columns: {sorted(missing)}")
            for row in frame.to_dict("records"):
                ts_code = _to_ts_code(str(row["代码"]))
                if not _is_main_board(ts_code):
                    continue
                published_day = pd.to_datetime(row["公告日期"], errors="coerce")
                if pd.isna(published_day):
                    continue
                published_at = self._announcement_time(published_day.date(), end)
                if not (start <= published_at <= end):
                    continue
                title = str(row.get("公告标题") or "").strip()
                url = str(row.get("网址") or "").strip() or None
                source = "akshare-eastmoney-announcement-mirror"
                documents.append(
                    NewsDocument(
                        ts_code=ts_code,
                        title=title,
                        published_at=published_at,
                        source=source,
                        source_tier=SourceTier.OFFICIAL,
                        url=url,
                        content_hash=self._hash(source, title, url, published_at),
                        fetched_at=fetched_at,
                    )
                )
            day += timedelta(days=1)
        return documents

    def fetch_mainstream_documents(self, start: datetime, end: datetime) -> list[NewsDocument]:
        fetched_at = datetime.now(SHANGHAI_TZ)
        documents: list[NewsDocument] = []
        successful_sources = 0
        errors: list[str] = []
        try:
            frame = self.client.stock_info_global_cls(symbol="全部")
            required = {"标题", "内容", "发布日期", "发布时间"}
            if not required.issubset(frame.columns):
                raise RuntimeError(f"missing columns: {sorted(required.difference(frame.columns))}")
            successful_sources += 1
            for row in frame.to_dict("records"):
                published_at = datetime.combine(row["发布日期"], row["发布时间"], tzinfo=SHANGHAI_TZ)
                if not (start <= published_at <= end):
                    continue
                content = str(row.get("内容") or "").strip()
                title = str(row.get("标题") or "").strip() or content[:80]
                source = "akshare-cls"
                documents.append(
                    NewsDocument(
                        title=title,
                        content=content,
                        published_at=published_at,
                        source=source,
                        source_tier=SourceTier.MAINSTREAM,
                        content_hash=self._hash(source, title + content, None, published_at),
                        fetched_at=fetched_at,
                    )
                )
        except Exception as exc:
            errors.append(f"CLS: {exc}")
        try:
            frame = self.client.stock_info_global_sina()
            required = {"时间", "内容"}
            if not required.issubset(frame.columns):
                raise RuntimeError(f"missing columns: {sorted(required.difference(frame.columns))}")
            successful_sources += 1
            for row in frame.to_dict("records"):
                timestamp = pd.to_datetime(row["时间"], errors="coerce")
                if pd.isna(timestamp):
                    continue
                published_at = timestamp.to_pydatetime()
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=SHANGHAI_TZ)
                if not (start <= published_at <= end):
                    continue
                content = str(row.get("内容") or "").strip()
                source = "akshare-sina"
                documents.append(
                    NewsDocument(
                        title=content[:80],
                        content=content,
                        published_at=published_at,
                        source=source,
                        source_tier=SourceTier.MAINSTREAM,
                        content_hash=self._hash(source, content, None, published_at),
                        fetched_at=fetched_at,
                    )
                )
        except Exception as exc:
            errors.append(f"Sina: {exc}")
        if successful_sources == 0:
            raise RuntimeError("; ".join(errors) or "all AKShare news sources failed")
        return documents
