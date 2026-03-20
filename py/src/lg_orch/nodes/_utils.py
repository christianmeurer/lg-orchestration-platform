# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Shared utilities for node implementations.

Provides:
- ``validate_base_url`` — URL scheme validation used by executor, verifier, context_builder.
- ``extract_json_block`` — unified JSON extraction from LLM output used by router and planner.
- ``resolve_inference_client`` — model provider resolution block used by router and planner.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lg_orch.tools import InferenceClient

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL | re.IGNORECASE)


def validate_base_url(url: str, label: str = "url") -> None:
    """Raise ``ValueError`` if *url* does not start with http:// or https://.

    Args:
        url: The URL string to validate.
        label: Human-readable label used in the error message.

    Raises:
        ValueError: If the URL has an unsupported scheme.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"{label} must start with http:// or https://; got {url!r}")


def extract_json_block(text: str) -> str | None:
    """Extract a JSON string from LLM output.

    Tries, in order:
    1. A ````json ... ```` fenced block.
    2. A ````` ``` ... ``` ````` fenced block.
    3. Raw ``{...}`` or ``[...]`` starting at the first brace/bracket.

    Returns the extracted JSON string, or ``None`` when nothing is found.
    """
    fenced = _JSON_FENCE_RE.search(text)
    if fenced is not None:
        return fenced.group(1).strip()

    # Try raw object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end >= start:
        return text[start: end + 1].strip()

    # Try raw array
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end >= start:
        return text[start: end + 1].strip()

    return None


def resolve_inference_client(
    state: dict[str, Any],
    slot_name: str,
    cfg_key: str,
) -> tuple["InferenceClient", str]:
    """Resolve the configured model provider and return ``(client, model_name)``.

    Reads ``state["_models"][slot_name]`` and
    ``state["_model_provider_runtime"][cfg_key]`` to construct an
    :class:`~lg_orch.tools.InferenceClient`.

    Args:
        state: The current agent state dict.
        slot_name: The model slot to read (``"router"`` or ``"planner"``).
        cfg_key: The runtime config key (``"digitalocean"`` or ``"openai_compatible"``).

    Returns:
        A ``(InferenceClient, model_name)`` tuple.

    Raises:
        ValueError: When the provider is ``"local"`` or required keys are missing.
    """
    from lg_orch.tools import InferenceClient

    models_raw = state.get("_models", {})
    models = models_raw if isinstance(models_raw, dict) else {}
    slot_raw = models.get(slot_name, {})
    slot = slot_raw if isinstance(slot_raw, dict) else {}

    provider = str(slot.get("provider", "local")).strip().lower()
    if provider in {"", "local"}:
        raise ValueError(f"provider for slot '{slot_name}' is 'local' — no remote client needed")

    model = str(slot.get("model", "deterministic")).strip()
    if not model:
        raise ValueError(f"model name for slot '{slot_name}' is empty")

    runtime_raw = state.get("_model_provider_runtime", {})
    runtime = runtime_raw if isinstance(runtime_raw, dict) else {}

    if provider == "openai_compatible":
        oc_raw = runtime.get("openai_compatible", {})
        oc_cfg = oc_raw if isinstance(oc_raw, dict) else {}
        api_key = str(oc_cfg.get("api_key", "")).strip()
        if not api_key:
            raise ValueError("openai_compatible.api_key is not set")
        base_url = str(oc_cfg.get("base_url", "https://api.openai.com/v1")).strip().rstrip("/")
        if not base_url:
            raise ValueError("openai_compatible.base_url is empty")
        validate_base_url(base_url, "openai_compatible.base_url")
        timeout_raw = oc_cfg.get("timeout_s", 60)
        timeout_s = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 60
    else:
        do_raw = runtime.get("digitalocean", {})
        do_cfg = do_raw if isinstance(do_raw, dict) else {}
        api_key = str(do_cfg.get("api_key", "")).strip()
        if not api_key:
            raise ValueError("digitalocean.api_key is not set")
        base_url = str(do_cfg.get("base_url", "https://inference.do-ai.run/v1")).strip().rstrip("/")
        if not base_url:
            raise ValueError("digitalocean.base_url is empty")
        validate_base_url(base_url, "digitalocean.base_url")
        timeout_raw = do_cfg.get("timeout_s", 60)
        timeout_s = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 60

    client = InferenceClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    return client, model
