# Optional container image for MCP registries/sandboxes (Glama, Smithery, Docker MCP).
# Local installs require zero Docker or third-party packages: pip install -e . --no-deps
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SHINOBU_HOME=/tmp/shinobu_data \
    SHINOBU_DISABLE_PLUGINS=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY shinobu_core ./shinobu_core

RUN pip install --no-cache-dir --no-deps .

ENTRYPOINT ["shinobu", "mcp-serve"]
