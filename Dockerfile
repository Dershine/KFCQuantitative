FROM python:3.13-slim AS runtime

ARG SOURCE_SHA=development
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.source="https://github.com/Dershine/KFCQuantitative" \
      org.opencontainers.image.revision="${SOURCE_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    KFCQUANT_SOURCE_SHA=${SOURCE_SHA} \
    KFCQUANT_BUILD_TIME=${BUILD_TIME}

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin kfcquant

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY deploy/docker-entrypoint.sh /usr/local/bin/kfcquant-entrypoint
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir . \
    && chmod 755 /usr/local/bin/kfcquant-entrypoint

RUN mkdir -p /app/data/raw /app/reports /app/runtime /app/backups \
    && chown -R kfcquant:kfcquant /app
USER kfcquant
EXPOSE 8501
ENTRYPOINT ["/usr/local/bin/kfcquant-entrypoint"]
CMD ["kfcquant", "serve"]
