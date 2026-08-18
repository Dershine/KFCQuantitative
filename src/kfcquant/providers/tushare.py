from __future__ import annotations

import hashlib
from datetime import date, datetime, time

import pandas as pd

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.market_data import DAILY_BAR_SCHEMA, SECURITY_SCHEMA, TRADE_CALENDAR_SCHEMA
from kfcquant.models import NewsDocument, SourceTier


class TushareProvider:
    """Tushare adapter for EOD data, disclosures and licensed news endpoints."""

    def __init__(self, settings: Settings):
        if not settings.tushare_token:
            raise ValueError("TUSHARE_TOKEN is not configured")
        self.settings = settings
        import tushare as ts

        ts.set_token(settings.tushare_token)
        self.pro = ts.pro_api(settings.tushare_token)

    def fetch_securities(self) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for status in ("L", "D", "P"):
            frame = self.pro.stock_basic(
                exchange="",
                list_status=status,
                fields="ts_code,symbol,name,market,exchange,list_status,list_date,delist_date",
            )
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return SECURITY_SCHEMA.empty_frame()
        result = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code", keep="last")
        result["list_date"] = pd.to_datetime(result["list_date"], format="%Y%m%d", errors="coerce").dt.date
        result["delist_date"] = pd.to_datetime(result["delist_date"], format="%Y%m%d", errors="coerce").dt.date
        result["exchange"] = result["ts_code"].astype(str).str.rsplit(".", n=1).str[-1]
        normalized = result[list(SECURITY_SCHEMA.columns)]
        return SECURITY_SCHEMA.validate(normalized).frame

    def fetch_trade_calendar(self, start: date, end: date) -> pd.DataFrame:
        frame = self.pro.trade_cal(exchange="SSE", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        if frame.empty:
            return TRADE_CALENDAR_SCHEMA.empty_frame()
        frame = frame.rename(columns={"cal_date": "cal_date", "pretrade_date": "pretrade_date"})
        frame["cal_date"] = pd.to_datetime(frame["cal_date"], format="%Y%m%d").dt.date
        frame["pretrade_date"] = pd.to_datetime(frame["pretrade_date"], format="%Y%m%d", errors="coerce").dt.date
        frame["is_open"] = frame["is_open"].astype(str).eq("1")
        normalized = frame[["cal_date", "is_open", "pretrade_date"]]
        return TRADE_CALENDAR_SCHEMA.validate(normalized).frame

    def fetch_daily(self, trade_date: date) -> pd.DataFrame:
        day = trade_date.strftime("%Y%m%d")
        daily = self.pro.daily(trade_date=day)
        if daily.empty:
            return DAILY_BAR_SCHEMA.empty_frame()
        factor = self.pro.adj_factor(trade_date=day)
        limits = self.pro.stk_limit(trade_date=day)
        result = daily.merge(factor[["ts_code", "adj_factor"]], on="ts_code", how="left")
        if not limits.empty:
            result = result.merge(limits[["ts_code", "up_limit", "down_limit"]], on="ts_code", how="left")
        else:
            result["up_limit"] = pd.NA
            result["down_limit"] = pd.NA
        suspended = self.pro.suspend_d(suspend_type="S", trade_date=day)
        suspended_codes = (
            set(suspended["ts_code"].dropna().astype(str)) if not suspended.empty else set()
        )
        st_frame = self.pro.stock_st(trade_date=day)
        st_codes = set(st_frame["ts_code"].dropna().astype(str)) if not st_frame.empty else set()
        result["trade_date"] = trade_date
        for column in ("open", "high", "low", "close", "pre_close", "up_limit", "down_limit"):
            result[column] = pd.to_numeric(result[column], errors="coerce")
        result["volume"] = pd.to_numeric(result["vol"], errors="coerce") * 100.0
        # Tushare daily amount is documented in thousands of CNY.
        result["amount"] = pd.to_numeric(result["amount"], errors="coerce") * 1000.0
        result["adj_factor"] = pd.to_numeric(result["adj_factor"], errors="coerce")
        result["suspended"] = result["ts_code"].isin(suspended_codes)
        result["is_st"] = result["ts_code"].isin(st_codes)
        normalized = result[list(DAILY_BAR_SCHEMA.columns)]
        return DAILY_BAR_SCHEMA.validate(normalized).frame

    @staticmethod
    def _hash_document(source: str, url: str | None, title: str, published_at: datetime) -> str:
        payload = "|".join([source, url or "", title, published_at.isoformat()])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _official_publish_time(row: dict[str, object]) -> datetime:
        recorded = pd.to_datetime(row.get("rec_time"), errors="coerce")
        if not pd.isna(recorded):
            naive = recorded.to_pydatetime()
            return naive.replace(tzinfo=SHANGHAI_TZ) if naive.tzinfo is None else naive.astimezone(SHANGHAI_TZ)
        announced = pd.to_datetime(row["ann_date"], format="%Y%m%d").date()
        # Unknown publication time is conservatively placed at day end so it cannot leak into 14:40.
        return datetime.combine(announced, time(23, 59, 59), tzinfo=SHANGHAI_TZ)

    def fetch_official_documents(self, start: datetime, end: datetime) -> list[NewsDocument]:
        frame = self.pro.anns_d(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        fetched_at = datetime.now(SHANGHAI_TZ)
        documents: list[NewsDocument] = []
        for row in frame.to_dict("records"):
            published_at = self._official_publish_time(row)
            if not (start <= published_at <= end):
                continue
            title = str(row.get("title") or "").strip()
            url = str(row.get("url") or "").strip() or None
            source = "tushare-anns"
            documents.append(
                NewsDocument(
                    ts_code=str(row.get("ts_code") or "").strip() or None,
                    title=title,
                    published_at=published_at,
                    source=source,
                    source_tier=SourceTier.OFFICIAL,
                    url=url,
                    content_hash=self._hash_document(source, url, title, published_at),
                    fetched_at=fetched_at,
                )
            )
        return documents

    def fetch_mainstream_documents(self, start: datetime, end: datetime) -> list[NewsDocument]:
        fetched_at = datetime.now(SHANGHAI_TZ)
        documents: list[NewsDocument] = []
        for source_name in self.settings.news_sources:
            frame = self.pro.news(
                src=source_name,
                start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                fields="datetime,title,content,channels",
            )
            for row in frame.to_dict("records"):
                timestamp = pd.to_datetime(row.get("datetime"), errors="coerce")
                if pd.isna(timestamp):
                    continue
                published_at = timestamp.to_pydatetime()
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=SHANGHAI_TZ)
                title = str(row.get("title") or "").strip()
                content = str(row.get("content") or "").strip() or None
                source = f"tushare-news:{source_name}"
                documents.append(
                    NewsDocument(
                        title=title or (content or "")[:80],
                        content=content,
                        published_at=published_at,
                        source=source,
                        source_tier=SourceTier.MAINSTREAM,
                        content_hash=self._hash_document(source, None, title + (content or ""), published_at),
                        fetched_at=fetched_at,
                    )
                )
        return documents

    def fetch_documents(self, start: datetime, end: datetime) -> list[NewsDocument]:
        return self.fetch_official_documents(start, end) + self.fetch_mainstream_documents(start, end)
