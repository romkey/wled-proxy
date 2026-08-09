FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WLED_PROXY_CONFIG=/config/config.json \
    HEALTH_URL=http://127.0.0.1:8080/healthz

WORKDIR /app

# Before COPY so code changes do not invalidate this layer.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin wledproxy \
    && mkdir -p /config \
    && chown wledproxy /config

COPY wled_proxy/ ./wled_proxy/

USER wledproxy

EXPOSE 4048/udp 6454/udp 5568/udp 8080/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(os.environ['HEALTH_URL'], timeout=3)"

ENTRYPOINT ["python", "-m", "wled_proxy"]
