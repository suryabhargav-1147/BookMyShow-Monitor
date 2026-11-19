FROM mcr.microsoft.com/playwright/python:v1.65.0-noble
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY static ./static
RUN uv sync --frozen --no-dev && uv run playwright install chromium
ENV HOST=0.0.0.0 PORT=8642 BMS_DATA_DIR=/data
VOLUME /data
EXPOSE 8642
CMD ["uv", "run", "--no-dev", "bms-monitor"]
