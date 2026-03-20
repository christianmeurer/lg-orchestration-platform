#!/usr/bin/env bash
# Generate a SCIP index for a Python or Rust repository.
#
# Usage:
#   REPO_PATH=/path/to/repo LANG=python|rust OUTPUT_DIR=./scip_out ./scripts/index_repo.sh
#
# To make this script executable after cloning:
#   chmod +x scripts/index_repo.sh
set -euo pipefail

# ---------------------------------------------------------------------------
# Validate required environment variables
# ---------------------------------------------------------------------------
if [ -z "${REPO_PATH:-}" ]; then
    echo "ERROR: REPO_PATH is not set." >&2
    echo "Usage: REPO_PATH=/path/to/repo LANG=python|rust OUTPUT_DIR=./scip_out $0" >&2
    exit 1
fi

if [ -z "${LANG:-}" ]; then
    echo "ERROR: LANG is not set." >&2
    echo "Usage: REPO_PATH=/path/to/repo LANG=python|rust OUTPUT_DIR=./scip_out $0" >&2
    exit 1
fi

if [ -z "${OUTPUT_DIR:-}" ]; then
    echo "ERROR: OUTPUT_DIR is not set." >&2
    echo "Usage: REPO_PATH=/path/to/repo LANG=python|rust OUTPUT_DIR=./scip_out $0" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Ensure output directory exists
# ---------------------------------------------------------------------------
mkdir -p "${OUTPUT_DIR}"

# ---------------------------------------------------------------------------
# Dispatch by LANG
# ---------------------------------------------------------------------------
case "${LANG}" in
    python)
        if ! command -v scip-python > /dev/null 2>&1; then
            echo "ERROR: scip-python is not installed or not on PATH." >&2
            echo "Install it with: pip install scip-python" >&2
            exit 1
        fi

        PROJECT_NAME="$(basename "${REPO_PATH}")"
        echo "Indexing Python project '${PROJECT_NAME}' at ${REPO_PATH} ..."

        scip-python index \
            --project-name "${PROJECT_NAME}" \
            --project-root "${REPO_PATH}" \
            --output "${OUTPUT_DIR}/index.scip"

        echo "Index written to ${OUTPUT_DIR}/index.scip"
        ;;

    rust)
        if ! command -v rust-analyzer > /dev/null 2>&1; then
            echo "ERROR: rust-analyzer is not installed or not on PATH." >&2
            echo "Install it with: rustup component add rust-analyzer" >&2
            exit 1
        fi

        # rust-analyzer exposes SCIP generation via its 'scip' subcommand.
        # Verify the subcommand is available before invoking it.
        if ! rust-analyzer scip --help > /dev/null 2>&1; then
            echo "ERROR: 'rust-analyzer scip' subcommand is not available." >&2
            echo "Upgrade rust-analyzer to a version that supports the 'scip' subcommand." >&2
            exit 1
        fi

        echo "Indexing Rust project at ${REPO_PATH} ..."

        (cd "${REPO_PATH}" && rust-analyzer scip --output "${OUTPUT_DIR}/index.scip")

        echo "Index written to ${OUTPUT_DIR}/index.scip"
        ;;

    *)
        echo "ERROR: Unknown LANG '${LANG}'. Supported values: python, rust." >&2
        echo "Usage: REPO_PATH=/path/to/repo LANG=python|rust OUTPUT_DIR=./scip_out $0" >&2
        exit 1
        ;;
esac
