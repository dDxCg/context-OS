FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY data/schema.sql ./data/schema.sql

RUN uv sync --frozen --no-dev

ENV MODE=prod \
    CONFIG_PATH=config.yaml \
    SCHEMA_PATH=data/schema.sql

CMD ["uv", "run", "python", "-m", "vcs.runtime"]
