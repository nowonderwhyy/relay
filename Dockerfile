FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_HOST=0.0.0.0 \
    RELAY_PORT=8080 \
    RELAY_ROOT=/staging \
    RELAY_DATA=/data \
    RELAY_CONFIG=/config/config.json

RUN apt-get update && \
    apt-get install -y --no-install-recommends aria2 iproute2 && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --system --user-group --create-home --home-dir /home/relay --shell /usr/sbin/nologin relay && \
    mkdir -p /app /config /data /staging && \
    chown -R relay:relay /config /data /staging

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY relay ./relay

RUN pip install --no-cache-dir .

USER relay
EXPOSE 8080

VOLUME ["/config", "/data", "/staging"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

ENTRYPOINT ["relay"]
CMD ["serve"]
