from __future__ import annotations

from kfcquant.config import Settings
from kfcquant.interfaces import LiveQuoteProvider, LLMProvider, MarketDataProvider, NewsProvider
from kfcquant.providers.akshare_live import AkShareLiveQuoteProvider
from kfcquant.providers.akshare_news import AkShareNewsProvider
from kfcquant.providers.baostock_market import BaoStockMarketDataProvider
from kfcquant.providers.qwen import OpenAICompatibleLLMProvider
from kfcquant.providers.tushare import TushareProvider


def build_market_provider(settings: Settings) -> MarketDataProvider:
    name = settings.market_provider.lower()
    if name == "baostock":
        return BaoStockMarketDataProvider()
    if name == "tushare":
        return TushareProvider(settings)
    raise ValueError(f"unsupported market provider: {settings.market_provider}")


def build_live_provider(settings: Settings) -> LiveQuoteProvider:
    if settings.live_provider.lower() == "akshare":
        return AkShareLiveQuoteProvider()
    raise ValueError(f"unsupported live provider: {settings.live_provider}")


def build_news_provider(settings: Settings) -> NewsProvider:
    name = settings.news_provider.lower()
    if name == "akshare":
        return AkShareNewsProvider()
    if name == "tushare":
        return TushareProvider(settings)
    raise ValueError(f"unsupported news provider: {settings.news_provider}")


def build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider.lower() in {"deepseek", "qwen", "openai-compatible"}:
        return OpenAICompatibleLLMProvider(settings)
    raise ValueError(f"unsupported LLM provider: {settings.llm_provider}")
