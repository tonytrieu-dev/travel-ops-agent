FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://pkg.boundaryml.com/install.sh -o /tmp/install-baml.sh && sh /tmp/install-baml.sh --yes
ENV PATH="/root/.baml/bin:/application/.venv/bin:$PATH"
RUN baml toolchain use 0.23.0
WORKDIR /application
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
RUN baml generate
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
