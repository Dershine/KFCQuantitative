from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from kfcquant.application.ports import NewsRepository
from kfcquant.interfaces import LLMCallError, LLMProvider, NewsProvider
from kfcquant.models import (
    DocumentEntity,
    EntityAssociationSource,
    NewsDocument,
    RiskExtractionResult,
)
from kfcquant.observability import AlertCode, MetricName, Observability, get_observability
from kfcquant.providers.document_loader import DocumentLoader

RISK_KEYWORDS = (
    "立案",
    "调查",
    "处罚",
    "退市",
    "风险警示",
    "ST",
    "下修",
    "亏损",
    "非标",
    "保留意见",
    "违约",
    "诉讼",
    "仲裁",
    "违规担保",
    "减持",
    "事故",
    "停产",
    "暂停上市",
    "停牌",
    "冻结",
    "失信",
    "破产",
    "终止上市",
    "无法表示意见",
    "重大风险",
)

POSITIVE_KEYWORDS = (
    "中标",
    "增持",
    "回购",
    "预增",
    "扭亏",
    "突破",
    "获批",
    "签订合同",
    "战略合作",
    "订单增长",
)

LOGGER = logging.getLogger(__name__)


@dataclass
class NewsSyncResult:
    official_healthy: bool
    mainstream_healthy: bool
    fetched_documents: int
    inserted_documents: int
    processed_documents: int
    failed_documents: int
    messages: list[str]


class NewsService:
    def __init__(
        self,
        repository: NewsRepository,
        provider: NewsProvider,
        llm: LLMProvider | None,
        loader: DocumentLoader,
        observability: Observability | None = None,
        official_news_backlog_threshold: int = 100,
    ):
        self.repository = repository
        self.provider = provider
        self.llm = llm
        self.loader = loader
        self.observability = observability or get_observability()
        self.official_news_backlog_threshold = official_news_backlog_threshold

    def _map_entities(self, documents: list[NewsDocument]) -> None:
        securities = self.repository.get_securities()
        names = sorted(
            (
                (str(row["name"]), str(row["ts_code"]))
                for row in securities.to_dict("records")
                if row.get("name") and len(str(row["name"])) >= 3
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for document in documents:
            associations = {entity.ts_code: entity for entity in document.entities}
            if document.ts_code:
                associations[document.ts_code] = DocumentEntity(
                    document_id=document.document_id,
                    ts_code=document.ts_code,
                    relevance=1.0,
                    association_source=EntityAssociationSource.PROVIDER,
                )
            title = document.title
            content = document.content or ""
            for name, code in names:
                if code in associations:
                    continue
                if name in title:
                    associations[code] = DocumentEntity(
                        document_id=document.document_id,
                        ts_code=code,
                        relevance=1.0,
                        association_source=EntityAssociationSource.EXACT_TITLE,
                    )
                elif name in content:
                    associations[code] = DocumentEntity(
                        document_id=document.document_id,
                        ts_code=code,
                        relevance=0.8,
                        association_source=EntityAssociationSource.EXACT_CONTENT,
                    )
            document.entities = sorted(associations.values(), key=lambda item: item.ts_code)
            if document.ts_code is None and len(document.entities) == 1:
                document.ts_code = document.entities[0].ts_code

    def sync(self, start: datetime, end: datetime) -> NewsSyncResult:
        official_healthy = True
        mainstream_healthy = True
        messages: list[str] = []
        documents: list[NewsDocument] = []
        try:
            documents.extend(self.provider.fetch_official_documents(start, end))
        except Exception as exc:
            official_healthy = False
            messages.append(f"官方公告接口失败: {exc}")
            self.observability.alert(
                AlertCode.OFFICIAL_NEWS_UNHEALTHY,
                "official news provider request failed",
                dedup_key=end.date().isoformat(),
                provider=str(getattr(self.provider, "source_name", type(self.provider).__name__)),
                stage="news.sync",
            )
        try:
            documents.extend(self.provider.fetch_mainstream_documents(start, end))
        except Exception as exc:
            mainstream_healthy = False
            messages.append(f"主流新闻接口失败: {exc}")
        self._map_entities(documents)
        inserted = self.repository.save_news_documents(documents)
        processed, failed = self.process_pending()
        return NewsSyncResult(
            official_healthy=official_healthy,
            mainstream_healthy=mainstream_healthy,
            fetched_documents=len(documents),
            inserted_documents=inserted,
            processed_documents=processed,
            failed_documents=failed,
            messages=messages,
        )

    def process_pending(self, limit: int = 5_000) -> tuple[int, int]:
        processed = 0
        failed = 0
        for document in self.repository.pending_news_documents(limit=limit):
            text = f"{document.title}\n{document.content or ''}"
            extraction_keywords = RISK_KEYWORDS + POSITIVE_KEYWORDS
            if not any(keyword.upper() in text.upper() for keyword in extraction_keywords):
                self.repository.mark_document(document.document_id, "processed")
                processed += 1
                continue
            try:
                content = document.content
                if not content and document.url:
                    content = self.loader.load_text(document.url)
                    if not content:
                        raise ValueError("文档没有可抽取文本")
                    document.content = content
                if self.llm is None:
                    raise RuntimeError("LLM未配置，风险公告不能安全抽取")
                result = self.llm.extract_risk_events(document)
                if isinstance(result, RiskExtractionResult) and result.trace is not None:
                    self.repository.complete_risk_extraction(document.document_id, result, content=content)
                    LOGGER.info(
                        "risk extraction completed document_id=%s llm_call_id=%s events=%s",
                        document.document_id,
                        result.trace.call_id,
                        len(result.events),
                    )
                else:
                    events = result.events if isinstance(result, RiskExtractionResult) else result
                    self.repository.save_risk_events(events)
                    self.repository.mark_document(document.document_id, "processed", content=content)
                processed += 1
            except LLMCallError as exc:
                self.repository.fail_risk_extraction(document.document_id, exc.trace, content=document.content)
                LOGGER.warning(
                    "risk extraction failed document_id=%s llm_call_id=%s error_type=%s",
                    document.document_id,
                    exc.trace.call_id,
                    exc.trace.error_type,
                )
                self.observability.metric(
                    MetricName.LLM_EXTRACTION_FAILURE_TOTAL,
                    1,
                    labels={"error_type": exc.trace.error_type or "unknown"},
                    provider=str(getattr(self.llm, "source_name", type(self.llm).__name__)),
                    stage="news.extract-risk",
                )
                failed += 1
            except Exception as exc:
                self.repository.mark_document(document.document_id, "failed", error=str(exc)[:1000])
                self.observability.metric(
                    MetricName.LLM_EXTRACTION_FAILURE_TOTAL,
                    1,
                    labels={"error_type": type(exc).__name__},
                    provider=str(getattr(self.llm, "source_name", type(self.llm).__name__)) if self.llm else "none",
                    stage="news.extract-risk",
                )
                failed += 1
        pending = self.repository.pending_news_documents(limit=self.official_news_backlog_threshold + 1)
        official_pending = sum(str(item.source_tier.value) == "official" for item in pending)
        self.observability.metric(
            MetricName.OFFICIAL_NEWS_PENDING,
            official_pending,
            stage="news.backlog",
        )
        if official_pending >= self.official_news_backlog_threshold:
            self.observability.alert(
                AlertCode.OFFICIAL_NEWS_BACKLOG,
                "official news processing backlog exceeded the configured threshold",
                dedup_key="official-news",
                stage="news.backlog",
            )
        return processed, failed
