# BMS Monitor: production image (dashboard + checker + Telegram bot).
#   docker build -t bms-monitor .
#   docker run -p 127.0.0.1:30030:8642 -v <data dir>:/data --env-file .env bms-monitor
# Plain-HTTP checks only (no browser): small image. BookMyShow must accept the server's IP.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY static ./static
RUN uv sync --frozen --no-dev && useradd -r -u 10001 bms && mkdir -p /data && chown bms /data
USER bms
ENV HOST=0.0.0.0 PORT=8642 BMS_DATA_DIR=/data BMS_BROWSER_FALLBACK=0 PYTHONUNBUFFERED=1
EXPOSE 8642
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8642/healthz',timeout=4)"
CMD ["/app/.venv/bin/bms-monitor"]
