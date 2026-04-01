# Security

## Reporting Vulnerabilities

Please report security vulnerabilities through [GitHub Security Advisories](https://github.com/christianmeurer/Lula/security/advisories/new) or by emailing `security@lula.dev`.

Do **not** open public issues for security vulnerabilities. We follow responsible disclosure: once a fix is available, we will coordinate a public disclosure timeline with the reporter.

We aim to acknowledge reports within 48 hours and provide an initial assessment within 5 business days.

## Scope

The Rust runner is a security-critical execution boundary. It enforces:

- **HMAC-SHA256 approval gates** — Time-bounded, nonce-bearing tokens with constant-time validation (`subtle::ConstantTimeEq`) for all mutation operations
- **Firecracker MicroVM sandbox** — Full VM isolation with vsock guest agent communication
- **Linux namespace isolation** — User/PID/net/mount namespace isolation via `unshare` (default on Linux)
- **Path confinement** — cap-std capability-based filesystem access (TOCTOU-safe)
- **Command allowlist** — Single canonical allowlist in `config.rs`; only `uv`, `python`, `pytest`, `ruff`, `mypy`, `cargo`, `git` permitted
- **Prompt injection detection** — Bidirectional Unicode overrides, RCE shell vectors, and cryptomining patterns blocked

## Supply-Chain Security

- `cargo deny` scans the Rust dependency tree on every pull request (license allowlist + advisory database)
- `pip-audit` scans Python dependencies in the `security-audit` CI job
- `trivy-action` (pinned to SHA) scans container images
- Cosign image signing in the release workflow

## Runtime Hardening

- Container runs as UID 10001 (non-root)
- `readOnlyRootFilesystem: true`
- `capabilities.drop: [ALL]`
- `seccompProfile: RuntimeDefault`
- `automountServiceAccountToken: false` on runner pods
- gVisor `runtimeClassName` in Kubernetes
