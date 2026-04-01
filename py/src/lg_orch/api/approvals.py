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
from typing import Any, cast

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
    # Rust runner verify_token splits on '.' — use dot-separated format.
    return f"{challenge_id}.{iat}.{nonce}.{signature}"


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


def handle_spa_approve(
    service: Any,
    run_id: str,
    payload: dict[str, Any],
    *,
    auth_subject: str = "",
) -> dict[str, Any]:
    """Handle POST /v1/runs/{run_id}/approve from the SPA.

    Generates an HMAC approval token for the challenge and delegates to
    ``service.approve_run`` which spawns the resume subprocess.

    Parameters
    ----------
    service:
        The ``RemoteAPIService`` instance.
    run_id:
        The run to approve and resume.
    payload:
        JSON body from the SPA. May contain ``challenge_id`` and ``actor``.
    auth_subject:
        Authenticated user identity (from bearer token or JWT).

    Returns
    -------
    dict
        The resumed run payload, or raises ``ValueError`` / ``RuntimeError``.
    """
    challenge_id = _non_empty_str(payload.get("challenge_id"))
    actor = _non_empty_str(payload.get("actor")) or auth_subject or "spa"
    approve_payload: dict[str, Any] = {"actor": actor}
    if challenge_id is not None:
        approve_payload["challenge_id"] = challenge_id
    rationale = _non_empty_str(payload.get("rationale"))
    if rationale is not None:
        approve_payload["rationale"] = rationale
    result = service.approve_run(run_id, approve_payload, auth_subject=auth_subject)
    if result is None:
        raise ValueError("run_not_found")
    return cast(dict[str, Any], result)
