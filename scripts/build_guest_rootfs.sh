#!/usr/bin/env bash
# Build the Firecracker guest rootfs ext4 image containing lula-guest-agent.
# Output: artifacts/rootfs.ext4
# Requires: Docker with BuildKit enabled (Docker 20.10+).
#
# This script is idempotent: re-running overwrites the previous image.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/artifacts"
OUTPUT_IMAGE="${OUTPUT_DIR}/rootfs.ext4"

# Verify Docker is available.
if ! command -v docker &>/dev/null; then
    echo "ERROR: docker not found in PATH. Install Docker and ensure it is running." >&2
    exit 1
fi

# Verify BuildKit support (Docker 20.10+ has it built in; older versions need
# DOCKER_BUILDKIT=1 env var).  If the daemon is too old the build will fail
# with a clear error from Docker itself.
if ! docker buildx version &>/dev/null; then
    echo "WARNING: 'docker buildx' not available. Falling back to DOCKER_BUILDKIT=1." >&2
fi

mkdir -p "${OUTPUT_DIR}"

echo "Building guest rootfs (this may take several minutes on first run)..."
DOCKER_BUILDKIT=1 docker build \
    --file "${REPO_ROOT}/rs/guest-agent/Dockerfile.rootfs" \
    --target export \
    --output "type=local,dest=${OUTPUT_DIR}" \
    "${REPO_ROOT}/rs"

if [[ ! -f "${OUTPUT_IMAGE}" ]]; then
    echo "ERROR: Expected output file '${OUTPUT_IMAGE}' not found after build." >&2
    echo "  Ensure Docker BuildKit is enabled (DOCKER_BUILDKIT=1) and the" >&2
    echo "  build completed without errors." >&2
    exit 1
fi

echo "Guest rootfs built: ${OUTPUT_IMAGE}"
echo "Size: $(du -h "${OUTPUT_IMAGE}" | cut -f1)"
