FROM rust:1.85-bookworm

WORKDIR /app

ENV PATH=/root/.local/bin:${PATH} \
    LG_PROFILE=prod \
    LG_REPO_ROOT=/app \
    LG_RUNNER_BIND=127.0.0.1:8088 \
    LG_REMOTE_API_HOST=0.0.0.0 \
    PORT=8001

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

COPY . .

RUN uv python install 3.12 \
    && uv sync --project ./py --python 3.12 --no-dev \
    && cargo build --manifest-path ./rs/Cargo.toml --release -p lg-runner

EXPOSE 8001

CMD ["bash", "./scripts/start_remote_stack.sh"]
