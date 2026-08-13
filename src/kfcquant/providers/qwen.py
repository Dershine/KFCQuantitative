from __future__ import annotations

import json
from datetime import datetime

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.models import (
    EventDirection,
    NewsDocument,
    RiskEvent,
    RiskSeverity,
)

HARD_BLOCK_TYPES = {
    "regulatory_investigation",
    "regulatory_penalty",
    "delisting_risk",
    "risk_warning",
    "earnings_downgrade",
    "major_loss",
    "qualified_audit_opinion",
    "debt_default",
    "major_litigation",
    "illegal_guarantee",
    "major_shareholder_reduction",
    "production_accident",
    "production_halt",
    "trading_suspension",
}


class OpenAICompatibleLLMProvider:
    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is not configured")
        from openai import OpenAI

        self.settings = settings
        self.client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

    def extract_risk_events(self, document: NewsDocument) -> list[RiskEvent]:
        text = f"{document.title}\n{document.content or ''}".strip()
        prompt = {
            "ts_code": document.ts_code,
            "title": document.title,
            "content": text[:24_000],
            "allowed_event_types": sorted(HARD_BLOCK_TYPES | {"positive_update", "neutral_update", "other_risk"}),
        }
        response = self.client.chat.completions.create(
            model=self.settings.llm_extract_model,
            temperature=0,
            max_tokens=2048,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是A股公开资讯事件抽取器。只根据输入原文抽取正面、中性或风险事件，不得补充外部事实。"
                        '返回JSON对象，格式为{"events":[{"event_type":字符串,'
                        '"direction":positive|neutral|negative,"severity":low|medium|high|critical,'
                        '"confidence":0到1,"evidence":原文中的短证据}]}。没有事件则返回空数组。'
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        events: list[RiskEvent] = []
        for raw in payload.get("events", []):
            event_type = str(raw.get("event_type", "other_risk"))
            if event_type not in HARD_BLOCK_TYPES | {"positive_update", "neutral_update", "other_risk"}:
                event_type = "other_risk"
            direction = EventDirection(str(raw.get("direction", "neutral")))
            severity = RiskSeverity(str(raw.get("severity", "low")))
            confidence = min(max(float(raw.get("confidence", 0.0)), 0.0), 1.0)
            evidence = str(raw.get("evidence") or "").strip()
            evidence_is_grounded = bool(evidence and evidence in text)
            if not evidence_is_grounded:
                evidence = document.title[:500]
                confidence = min(confidence, 0.5)
            hard_block = (
                event_type in HARD_BLOCK_TYPES
                and direction == EventDirection.NEGATIVE
                and severity in {RiskSeverity.HIGH, RiskSeverity.CRITICAL}
                and confidence >= 0.7
                and evidence_is_grounded
            )
            events.append(
                RiskEvent(
                    document_id=document.document_id,
                    ts_code=document.ts_code,
                    event_type=event_type,
                    direction=direction,
                    severity=severity,
                    confidence=confidence,
                    hard_block=hard_block,
                    evidence=evidence,
                    source_url=document.url,
                    published_at=document.published_at,
                    extracted_at=datetime.now(SHANGHAI_TZ),
                    model_name=self.settings.llm_extract_model,
                )
            )
        return events

    def generate_report(self, context: dict[str, object]) -> str:
        response = self.client.chat.completions.create(
            model=self.settings.llm_report_model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是个人量化研究报告撰写器。只能概括输入JSON中的事实，必须区分14:40前信息和入场后信息。"
                        "不得承诺收益，不得把影子组合描述成真实交易。输出简洁中文Markdown。"
                    ),
                },
                {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    def healthcheck(self) -> str:
        response = self.client.chat.completions.create(
            model=self.settings.llm_extract_model,
            temperature=0,
            max_tokens=32,
            messages=[
                {"role": "system", "content": "只返回JSON。"},
                {"role": "user", "content": '返回 {"ok": true}'},
            ],
            response_format={"type": "json_object"},
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        if payload.get("ok") is not True:
            raise RuntimeError("LLM JSON healthcheck returned unexpected content")
        return str(getattr(response, "model", self.settings.llm_extract_model))


# Preserve the public import used by existing integrations.
QwenLLMProvider = OpenAICompatibleLLMProvider
