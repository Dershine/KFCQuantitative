from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from time import perf_counter

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.interfaces import LLMCallError
from kfcquant.models import (
    EventDirection,
    LLMCallStatus,
    LLMCallTrace,
    NewsDocument,
    RiskEvent,
    RiskEventEntity,
    RiskExtractionResult,
    RiskSeverity,
)

RISK_EXTRACTION_PROMPT_VERSION = "risk-extraction-v1"
RISK_EXTRACTION_SYSTEM_PROMPT = (
    "你是A股公开资讯事件抽取器。只根据输入原文抽取正面、中性或风险事件，不得补充外部事实。"
    '返回JSON对象，格式为{"events":[{"event_type":字符串,'
    '"direction":positive|neutral|negative,"severity":low|medium|high|critical,'
    '"confidence":0到1,"evidence":原文中的短证据}]}。没有事件则返回空数组。'
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
ALLOWED_EVENT_TYPES = HARD_BLOCK_TYPES | {"positive_update", "neutral_update", "other_risk"}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _safe_failure_message(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "LLM provider timed out"
    if isinstance(error, json.JSONDecodeError):
        return "LLM response was not valid JSON"
    if isinstance(error, ValueError):
        return "LLM response violated the risk extraction contract"
    return "LLM risk extraction call failed"


class OpenAICompatibleLLMProvider:
    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is not configured")
        from openai import OpenAI

        self.settings = settings
        self.client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

    def extract_risk_events(self, document: NewsDocument) -> RiskExtractionResult:
        text = f"{document.title}\n{document.content or ''}".strip()
        prompt = {
            "ts_code": document.ts_code,
            "entities": [
                {
                    "ts_code": entity.ts_code,
                    "relevance": entity.relevance,
                    "association_source": entity.association_source.value,
                }
                for entity in document.entities
            ],
            "title": document.title,
            "content": text[:24_000],
            "allowed_event_types": sorted(ALLOWED_EVENT_TYPES),
        }
        prompt_descriptor = {
            "version": RISK_EXTRACTION_PROMPT_VERSION,
            "system": RISK_EXTRACTION_SYSTEM_PROMPT,
            "allowed_event_types": sorted(ALLOWED_EVENT_TYPES),
            "temperature": 0,
            "max_tokens": 2048,
            "response_format": "json_object",
        }
        prompt_sha = _content_sha256(_canonical_json(prompt_descriptor))
        input_json = _canonical_json(prompt)
        input_sha = _content_sha256(input_json)
        started_at = datetime.now(SHANGHAI_TZ)
        timer = perf_counter()
        response_model: str | None = None
        response_content: str | None = None
        try:
            response = self.client.chat.completions.create(
                model=self.settings.llm_extract_model,
                temperature=0,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": RISK_EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": input_json},
                ],
                response_format={"type": "json_object"},
            )
            response_model = str(getattr(response, "model", self.settings.llm_extract_model))
            response_content = response.choices[0].message.content or "{}"
            payload = json.loads(response_content)
            raw_events = payload.get("events", [])
            if not isinstance(raw_events, list):
                raise ValueError("LLM JSON events must be an array")
            trace = LLMCallTrace(
                document_id=document.document_id,
                provider=self.settings.llm_provider,
                prompt_version=RISK_EXTRACTION_PROMPT_VERSION,
                prompt_sha256=prompt_sha,
                input_sha256=input_sha,
                requested_model=self.settings.llm_extract_model,
                response_model=response_model,
                response_sha256=_content_sha256(response_content),
                started_at=started_at,
                duration_ms=max(0, int((perf_counter() - timer) * 1000)),
                status=LLMCallStatus.SUCCESS,
            )
            events: list[RiskEvent] = []
            for raw in raw_events:
                if not isinstance(raw, dict):
                    raise ValueError("LLM JSON event must be an object")
                event_type = str(raw.get("event_type", "other_risk"))
                if event_type not in ALLOWED_EVENT_TYPES:
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
                event = RiskEvent(
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
                    model_name=response_model,
                    llm_call_id=trace.call_id,
                )
                event.entities = [
                    RiskEventEntity(
                        event_id=event.event_id,
                        ts_code=entity.ts_code,
                        relevance=entity.relevance,
                        association_source=entity.association_source,
                    )
                    for entity in document.entities
                ]
                events.append(event)
            return RiskExtractionResult(events=events, trace=trace)
        except Exception as exc:
            trace = LLMCallTrace(
                document_id=document.document_id,
                provider=self.settings.llm_provider,
                prompt_version=RISK_EXTRACTION_PROMPT_VERSION,
                prompt_sha256=prompt_sha,
                input_sha256=input_sha,
                requested_model=self.settings.llm_extract_model,
                response_model=response_model,
                response_sha256=_content_sha256(response_content) if response_content is not None else None,
                started_at=started_at,
                duration_ms=max(0, int((perf_counter() - timer) * 1000)),
                status=LLMCallStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=_safe_failure_message(exc),
            )
            raise LLMCallError(trace) from exc

    def generate_report(self, context: dict[str, object]) -> str:
        response = self.client.chat.completions.create(
            model=self.settings.llm_report_model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是个人量化研究报告撰写器。只能概括输入JSON中的事实，"
                        "必须按输入中的信号截止时间区分截止前信息和入场后信息。"
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
