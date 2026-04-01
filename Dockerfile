# Base image digests — update with: docker inspect <image> --format '{{index .RepoDigests 0}}'
# rust:1.88-bookworm  — pin with: FROM rust:1.88-bookworm@sha256:<digest>
# python:3.12-slim-bookworm — pin with: FROM python:3.12-slim-bookworm@sha256:<digest>
# debian:bookworm-slim — pin with: FROM debian:bookworm-slim@sha256:<digest>
#
# To obtain digests in CI: docker pull <image> && docker inspect <image> --format '{{index .RepoDigests 0}}'
# The release workflow records the built image digest in the release notes via docker/metadata-action.

# Stage 1: Rust build
FROM rust:1.88-bookworm AS rust-builder

WORKDIR /app

COPY rs/ ./rs/

RUN cargo build --manifest-path ./rs/Cargo.toml --release --locked -p lg-runner

# ── Stage 2: SPA (Leptos/WASM) ──────────────────────────────────────
FROM rust:1.88-bookworm AS spa-builder
RUN rustup target add wasm32-unknown-unknown \
    && curl -sSL https://github.com/trunk-rs/trunk/releases/download/v0.21.14/trunk-x86_64-unknown-linux-gnu.tar.gz | tar xz \
    && mv trunk /usr/local/bin/
WORKDIR /app
COPY rs/spa-leptos/ rs/spa-leptos/
COPY rs/Cargo.toml rs/Cargo.toml
RUN mkdir -p rs/runner/src && echo "fn main(){}" > rs/runner/src/main.rs \
    && mkdir -p rs/guest-agent/src && echo "fn main(){}" > rs/guest-agent/src/main.rs
COPY rs/runner/Cargo.toml rs/runner/Cargo.toml
COPY rs/guest-agent/Cargo.toml rs/guest-agent/Cargo.toml
WORKDIR /app/rs/spa-leptos
RUN trunk build --release

# Stage 3: Python + uv setup
FROM python:3.12-slim-bookworm AS python-builder

WORKDIR /app

ARG UV_VERSION=0.7.2

ADD https://astral.sh/uv/${UV_VERSION}/install.sh /tmp/uv-installer.sh

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && sh /tmp/uv-installer.sh \
    && rm /tmp/uv-installer.sh

ENV PATH=/root/.local/bin:${PATH}

COPY py/ ./py/
COPY configs/ ./configs/
COPY prompts/ ./prompts/
COPY schemas/ ./schemas/

RUN uv sync --project ./py --python /usr/local/bin/python --no-dev --all-extras

# Stage 4: Runtime image
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

ENV PATH=/app/py/.venv/bin:${PATH} \
    HOME=/home/lula \
    LG_PROFILE=prod \
    LG_REPO_ROOT=/app \
    LG_RUNNER_BIND=127.0.0.1:8088 \
    LG_REMOTE_API_HOST=0.0.0.0 \
    PORT=8001

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Rust binary
COPY --from=rust-builder /app/rs/target/release/lg-runner ./rs/target/release/lg-runner

# Copy Python environment and app files
COPY --from=python-builder /app/py /app/py
COPY --from=python-builder /app/configs /app/configs
COPY --from=python-builder /app/prompts /app/prompts
COPY --from=python-builder /app/schemas /app/schemas

COPY --from=spa-builder /app/rs/spa-leptos/dist/ /app/spa-dist/
ENV LG_SPA_DIST_DIR=/app/spa-dist

# Copy startup script
COPY scripts/start_remote_stack.sh ./scripts/start_remote_stack.sh

EXPOSE 8001

RUN groupadd --gid 10001 lula && \
    useradd --uid 10001 --gid lula --shell /bin/bash --create-home lula && \
    chown -R lula:lula /app

USER lula

CMD ["bash", "./scripts/start_remote_stack.sh"]
