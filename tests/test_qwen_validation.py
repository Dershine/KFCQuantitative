from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from kfcquant.config import SHANGHAI_TZ
from kfcquant.models import NewsDocument, SourceTier
from kfcquant.providers.qwen import QwenLLMProvider


class FakeCompletions:
    def create(self, **kwargs):
        content = (
            '{"events":[{"event_type":"regulatory_investigation","direction":"negative",'
            '"severity":"critical","confidence":0.99,"evidence":"原文不存在的证据"}]}'
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_ungrounded_model_evidence_cannot_hard_block(settings):
    provider = QwenLLMProvider.__new__(QwenLLMProvider)
    provider.settings = settings
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    at = datetime(2026, 8, 10, 13, 0, tzinfo=SHANGHAI_TZ)
    doc = NewsDocument(
        document_id="doc",
        ts_code="600000.SH",
        title="普通公告",
        content="公司发布普通公告。",
        published_at=at,
        source="fixture",
        source_tier=SourceTier.OFFICIAL,
        content_hash="hash",
        fetched_at=at,
    )
    event = provider.extract_risk_events(doc)[0]
    assert not event.hard_block
    assert event.confidence == 0.5
    assert event.evidence == "普通公告"
