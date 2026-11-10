# Starts the server for introspection (tool listing) on Linux. Real work needs Windows and the game:
# the campaign files, LSLib's Divine.exe and Script Extender all live there.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE main.py ./
COPY src ./src
COPY bridge ./bridge
RUN uv sync --frozen --no-dev
ENV DOS2GM_HOME=/data
CMD ["uv", "run", "--no-sync", "dos2-gm-mcp"]
