# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Approval challenge-response helpers.

Exports the HMAC token generator and summary helpers used by the
approval/rejection endpoints in the main dispatch function.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any

from lg_orch.logging import get_logger


def _non_empty_str(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    return value


def approval_token_for_challenge(challenge_id: str) -> str:
    """Generate a cryptographically signed approval token.

    Format: ``{challenge_id}|{iat}|{nonce}|{signature}``

    Matches the HMAC-SHA256 protocol expected by the Rust runner in
    ``rs/runner/src/approval.rs``.  When ``LG_RUNNER_APPROVAL_SECRET`` is
    unset or empty, falls back to the legacy plain-text format and logs a
    warning.
    """
    secret = os.environ.get("LG_RUNNER_APPROVAL_SECRET", "")
    if not secret:
        _log = get_logger()
        _log.warning(
            "approval_token_insecure",
            detail="LG_RUNNER_APPROVAL_SECRET not set; using deprecated plain-text token",
        )
        return f"approve:{challenge_id}"
    nonce = secrets.token_hex(16)
    iat = int(time.time())
    message = f"{challenge_id}|{iat}|{nonce}"
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{message}|{signature}"


def tool_name_for_approval(*, operation_class: str, challenge_id: str) -> str:
    joined = f"{operation_class}:{challenge_id}".lower()
    if "apply_patch" in joined:
        return "apply_patch"
    if "exec" in joined:
        return "exec"
    return "apply_patch"


def approval_summary_text(details: dict[str, Any]) -> str:
    operation_class = _non_empty_str(details.get("operation_class")) or "mutation"
    challenge_id = _non_empty_str(details.get("challenge_id"))
    reason = _non_empty_str(details.get("reason")) or "approval_required"
    summary = f"{operation_class} requires approval"
    if challenge_id is not None:
        summary = f"{summary} ({challenge_id})"
    if reason not in {"approval_required", "challenge_required", "missing_approval_token"}:
        summary = f"{summary}: {reason}"
    return summary
