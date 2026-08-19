from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol, TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from filelock import FileLock


class MetricName(StrEnum):
    JOB_DURATION_SECONDS = "job_duration_seconds"
    JOB_SUCCESS_TOTAL = "job_success_total"
    JOB_FAILED_TOTAL = "job_failed_total"
    JOB_MISSED_TOTAL = "job_missed_total"
    PROVIDER_REQUEST_DURATION_SECONDS = "provider_request_duration_seconds"
    PROVIDER_FAILURE_TOTAL = "provider_failure_total"
    QUOTE_AGE_SECONDS = "quote_age_seconds"
    LATEST_EOD_LAG_DAYS = "latest_eod_lag_days"
    OFFICIAL_NEWS_PENDING = "official_news_pending"
    LLM_EXTRACTION_FAILURE_TOTAL = "llm_extraction_failure_total"
    CANDIDATE_COUNT = "candidate_count"
    ORDER_REJECTION_TOTAL = "order_rejection_total"
    DATABASE_LOCK_WAIT_SECONDS = "database_lock_wait_seconds"
    WORKER_HEARTBEAT_AGE_SECONDS = "worker_heartbeat_age_seconds"


class AlertCode(StrEnum):
    PRECLOSE_RUN_FAILED = "preclose_run_failed"
    OFFICIAL_NEWS_UNHEALTHY = "official_news_unhealthy"
    OFFICIAL_NEWS_BACKLOG = "official_news_backlog"
    QUOTE_DATA_STALE = "quote_data_stale"
    EOD_DATA_STALE = "eod_data_stale"
    DATABASE_LOCK_TIMEOUT = "database_lock_timeout"
    WORKER_HEARTBEAT_MISSING = "worker_heartbeat_missing"
    WORKER_HEARTBEAT_STALE = "worker_heartbeat_stale"
    ALERT_DELIVERY_FAILED = "alert_delivery_failed"


@dataclass(frozen=True)
class ObservabilityContext:
    job_run_id: str | None = None
    signal_run_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    source_sha: str | None = None
    provider: str | None = None
    stage: str | None = None
    information_cutoff: datetime | None = None


_CURRENT_CONTEXT: ContextVar[ObservabilityContext | None] = ContextVar(
    "kfcquant_observability_context",
    default=None,
)
_SENSITIVE_KEY_PARTS = ("authorization", "api_key", "apikey", "password", "secret", "token")
_SENSITIVE_QUERY_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "authorization", "key", "password", "secret", "signature", "token"}
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_PATTERN = re.compile(r"https?://[^\s]+")


def _iso(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.query:
            return value
        query = [
            (key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    except ValueError:
        return value


def _redact_string(value: str, secret_values: tuple[str, ...]) -> str:
    sanitized = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    sanitized = _URL_PATTERN.sub(lambda match: _redact_url(match.group(0)), sanitized)
    for secret in secret_values:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def redact(value: object, secret_values: tuple[str, ...] = (), key: str | None = None) -> object:
    if key and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]" if value not in (None, "") else value
    if isinstance(value, str):
        return _redact_string(value, secret_values)
    if isinstance(value, Mapping):
        return {str(item_key): redact(item, secret_values, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact(item, secret_values) for item in value]
    return _iso(value)


class ObservabilitySink(Protocol):
    def emit(self, record: dict[str, object]) -> None: ...


class MemoryObservabilitySink:
    def __init__(self):
        self.records: list[dict[str, object]] = []

    def emit(self, record: dict[str, object]) -> None:
        self.records.append(dict(record))


class JsonLineStreamSink:
    def __init__(self, stream: TextIO | None = None):
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()

    def emit(self, record: dict[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            self.stream.write(f"{line}\n")
            self.stream.flush()


class JsonlAuditSink:
    """Persist metric and alert records outside the research database."""

    def __init__(self, metrics_path: Path, alerts_path: Path):
        self.paths = {"metric": metrics_path, "alert": alerts_path}

    def emit(self, record: dict[str, object]) -> None:
        path = self.paths.get(str(record.get("record_type")))
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(f"{path}.lock", timeout=5)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with lock, path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{line}\n")


class WebhookAlertSink:
    """Deliver alerts through a generic JSON webhook; inactive when not configured."""

    def __init__(
        self,
        url: str,
        bearer_token: str | None = None,
        transport: Callable[[str, dict[str, object], dict[str, str]], None] | None = None,
    ):
        self.url = url
        self.bearer_token = bearer_token
        self.transport = transport or self._post

    @staticmethod
    def _post(url: str, payload: dict[str, object], headers: dict[str, str]) -> None:
        response = httpx.post(url, json=payload, headers=headers, timeout=5)
        response.raise_for_status()

    def emit(self, record: dict[str, object]) -> None:
        if record.get("record_type") != "alert":
            return
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        self.transport(self.url, record, headers)


class Observability:
    def __init__(
        self,
        sinks: tuple[ObservabilitySink, ...] = (),
        *,
        secret_values: tuple[str, ...] = (),
        alert_cooldown_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ):
        self.sinks = sinks
        self.secret_values = tuple(item for item in secret_values if item)
        self.alert_cooldown_seconds = alert_cooldown_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._last_alert: dict[tuple[str, str], datetime] = {}

    @contextmanager
    def context(self, **fields: object) -> Iterator[None]:
        current = _CURRENT_CONTEXT.get() or ObservabilityContext()
        updates = {key: value for key, value in fields.items() if key in asdict(current)}
        token = _CURRENT_CONTEXT.set(replace(current, **updates))
        try:
            yield
        finally:
            _CURRENT_CONTEXT.reset(token)

    def begin_context(self, **fields: object) -> Token[ObservabilityContext | None]:
        allowed = {key: value for key, value in fields.items() if key in asdict(ObservabilityContext())}
        return _CURRENT_CONTEXT.set(ObservabilityContext(**allowed))

    @staticmethod
    def end_context(token: Token[ObservabilityContext | None]) -> None:
        _CURRENT_CONTEXT.reset(token)

    def _base_record(self, record_type: str) -> dict[str, object]:
        context = {
            key: _iso(value)
            for key, value in asdict(_CURRENT_CONTEXT.get() or ObservabilityContext()).items()
            if value is not None
        }
        return {
            "record_type": record_type,
            "timestamp": self.clock().isoformat(),
            **context,
        }

    def _emit(self, record: dict[str, object], *, skip_sink: ObservabilitySink | None = None) -> None:
        safe = redact(record, self.secret_values)
        if not isinstance(safe, dict):
            raise TypeError("observability record must remain a mapping after redaction")
        for sink in self.sinks:
            if sink is skip_sink:
                continue
            try:
                sink.emit(safe)
            except Exception as exc:
                failure = {
                    **self._base_record("log"),
                    "event": AlertCode.ALERT_DELIVERY_FAILED.value,
                    "severity": "error",
                    "sink": type(sink).__name__,
                    "error_type": type(exc).__name__,
                }
                for fallback in self.sinks:
                    if fallback is sink or fallback is skip_sink:
                        continue
                    try:
                        fallback.emit(redact(failure, self.secret_values))
                    except Exception:
                        continue

    def event(self, event: str, *, severity: str = "info", **fields: object) -> None:
        self._emit({**self._base_record("log"), "event": event, "severity": severity, **fields})

    def metric(
        self,
        metric: MetricName,
        value: int | float,
        *,
        unit: str = "count",
        labels: Mapping[str, object] | None = None,
        **context: object,
    ) -> None:
        with self.context(**context):
            self._emit(
                {
                    **self._base_record("metric"),
                    "metric": metric.value,
                    "value": value,
                    "unit": unit,
                    "labels": dict(labels or {}),
                }
            )

    def alert(
        self,
        code: AlertCode,
        message: str,
        *,
        severity: str = "critical",
        dedup_key: str = "default",
        **context: object,
    ) -> bool:
        now = self.clock()
        key = (code.value, dedup_key)
        previous = self._last_alert.get(key)
        if previous is not None and (now - previous).total_seconds() < self.alert_cooldown_seconds:
            return False
        self._last_alert[key] = now
        with self.context(**context):
            self._emit(
                {
                    **self._base_record("alert"),
                    "alert_code": code.value,
                    "severity": severity,
                    "message": message,
                    "dedup_key": dedup_key,
                }
            )
        return True


class ObservabilityLogHandler(logging.Handler):
    """Route existing application logging calls through the structured sink."""

    def __init__(self, observability: Observability):
        super().__init__()
        self.observability = observability
        self._kfcquant_structured = True

    def emit(self, record: logging.LogRecord) -> None:
        fields: dict[str, object] = {
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            fields["error_type"] = record.exc_info[0].__name__
        self.observability.event(
            "application_log",
            severity=record.levelname.lower(),
            **fields,
        )


def configure_standard_logging(observability: Observability) -> None:
    for logger_name in ("kfcquant", "kfcops"):
        logger = logging.getLogger(logger_name)
        logger.handlers = [
            handler
            for handler in logger.handlers
            if not getattr(handler, "_kfcquant_structured", False)
        ]
        logger.addHandler(ObservabilityLogHandler(observability))
        logger.setLevel(logging.INFO)
        logger.propagate = False


class _ObservedProvider:
    def __init__(self, provider: object, observability: Observability, identity: str):
        self._provider = provider
        self._observability = observability
        self._identity = identity

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self._provider, name)
        if not callable(attribute) or not name.startswith(("fetch_", "iter_", "extract_", "generate_", "healthcheck")):
            return attribute

        def observed(*args: object, **kwargs: object) -> object:
            timer = perf_counter()
            with self._observability.context(provider=self._identity, stage=f"provider.{name}"):
                try:
                    result = attribute(*args, **kwargs)
                except Exception:
                    self._record_failure(name, timer)
                    raise
                if isinstance(result, Iterator):
                    return self._observe_iterator(result, name, timer)
                self._record_duration(name, timer)
                return result

        return observed

    def _record_duration(self, operation: str, timer: float) -> None:
        self._observability.metric(
            MetricName.PROVIDER_REQUEST_DURATION_SECONDS,
            max(0.0, perf_counter() - timer),
            unit="seconds",
            labels={"operation": operation},
            provider=self._identity,
        )

    def _record_failure(self, operation: str, timer: float) -> None:
        self._record_duration(operation, timer)
        self._observability.metric(
            MetricName.PROVIDER_FAILURE_TOTAL,
            1,
            labels={"operation": operation},
            provider=self._identity,
        )

    def _observe_iterator(self, iterator: Iterator[object], operation: str, timer: float) -> Iterator[object]:
        try:
            yield from iterator
        except Exception:
            self._record_failure(operation, timer)
            raise
        else:
            self._record_duration(operation, timer)


def observe_provider(provider: object, observability: Observability, identity: str | None = None) -> object:
    if isinstance(provider, _ObservedProvider):
        return provider
    declared_identity = getattr(provider, "source_name", None)
    resolved = identity or (declared_identity if isinstance(declared_identity, str) else type(provider).__name__)
    return _ObservedProvider(provider, observability, resolved)


_DEFAULT_OBSERVABILITY = Observability()


def get_observability() -> Observability:
    return _DEFAULT_OBSERVABILITY


def configure_observability(
    settings: Any,
    *,
    stream: TextIO | None = None,
    webhook_transport: Callable[[str, dict[str, object], dict[str, str]], None] | None = None,
) -> Observability:
    global _DEFAULT_OBSERVABILITY
    secrets = tuple(
        str(item)
        for item in (settings.llm_api_key, settings.tushare_token, settings.alert_webhook_bearer_token)
        if item
    )
    sinks: list[ObservabilitySink] = [
        JsonLineStreamSink(stream),
        JsonlAuditSink(settings.metrics_path, settings.alerts_path),
    ]
    if settings.alert_webhook_url:
        sinks.append(
            WebhookAlertSink(
                settings.alert_webhook_url,
                settings.alert_webhook_bearer_token,
                webhook_transport,
            )
        )
    _DEFAULT_OBSERVABILITY = Observability(
        tuple(sinks),
        secret_values=secrets,
        alert_cooldown_seconds=settings.alert_cooldown_seconds,
    )
    configure_standard_logging(_DEFAULT_OBSERVABILITY)
    return _DEFAULT_OBSERVABILITY
