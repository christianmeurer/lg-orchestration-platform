# Stage 1: Rust build
FROM rust:1.88-bookworm AS rust-builder

WORKDIR /app

COPY rs/ ./rs/
COPY Cargo.lock* ./

RUN cargo build --manifest-path ./rs/Cargo.toml --release -p lg-runner

# Stage 2: Python + uv setup
FROM python:3.12-slim-bookworm AS python-builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH=/root/.local/bin:${PATH}

COPY py/ ./py/
COPY configs/ ./configs/
COPY prompts/ ./prompts/
COPY schemas/ ./schemas/

RUN uv python install 3.12 \
    && uv sync --project ./py --python 3.12 --no-dev

# Stage 3: Runtime image
FROM debian:bookworm-slim AS runtime

WORKDIR /app

ENV PATH=/root/.local/bin:/app/py/.venv/bin:${PATH} \
    LG_PROFILE=prod \
    LG_REPO_ROOT=/app \
    LG_RUNNER_BIND=127.0.0.1:8088 \
    LG_REMOTE_API_HOST=0.0.0.0 \
    PORT=8001

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl python3 \
    && rm -rf /var/lib/apt/lists/*

# Copy Rust binary
COPY --from=rust-builder /app/rs/target/release/lg-runner ./rs/target/release/lg-runner

# Copy Python environment and app files
COPY --from=python-builder /root/.local /root/.local
COPY --from=python-builder /app/py /app/py
COPY --from=python-builder /app/configs /app/configs
COPY --from=python-builder /app/prompts /app/prompts
COPY --from=python-builder /app/schemas /app/schemas

# Copy startup script
COPY scripts/start_remote_stack.sh ./scripts/start_remote_stack.sh

EXPOSE 8001

RUN groupadd --gid 10001 lula && \
    useradd --uid 10001 --gid lula --shell /bin/bash --create-home lula

USER lula

CMD ["bash", "./scripts/start_remote_stack.sh"]
