# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Kubernetes sandbox configuration validation and TOML generation utilities.

Used at deploy time to validate Deployment manifests against expected
sandbox hardening requirements (Wave 9: gVisor/Kata Container sandboxing).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class SandboxConfig:
    """Expected sandbox hardening configuration for a runner Deployment."""

    runtime_class: str = "gvisor"
    workspace_path: str = "/workspace"
    enforce_read_only_root: bool = True
    network_policy_enabled: bool = True


def _get_container(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first container spec from a Deployment manifest, or None."""
    try:
        containers: list[dict[str, Any]] = (
            manifest["spec"]["template"]["spec"]["containers"]
        )
        if containers:
            return containers[0]
    except (KeyError, TypeError, IndexError):
        pass
    return None


def validate_deployment_manifest(
    manifest_path: str,
    expected: SandboxConfig,
) -> list[str]:
    """Read a Kubernetes Deployment YAML and return a list of violation strings.

    Checks performed:
    - ``runtimeClassName`` matches ``expected.runtime_class``
    - ``readOnlyRootFilesystem`` is ``True``
    - ``runAsNonRoot`` is ``True``
    - ``allowPrivilegeEscalation`` is ``False``
    - ``capabilities.drop`` includes ``"ALL"``

    Returns an empty list when all checks pass.
    """
    with open(manifest_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        return ["manifest: top-level document is not a mapping"]

    violations: list[str] = []

    # --- runtimeClassName ---
    pod_spec: dict[str, Any] = raw.get("spec", {}).get("template", {}).get("spec", {})
    actual_runtime = pod_spec.get("runtimeClassName")
    if actual_runtime != expected.runtime_class:
        violations.append(
            f"runtimeClassName: expected '{expected.runtime_class}', got {actual_runtime!r}"
        )

    container = _get_container(raw)
    if container is None:
        violations.append("containers: no container found in spec.template.spec.containers")
        return violations

    sc: dict[str, Any] = container.get("securityContext") or {}

    # --- readOnlyRootFilesystem ---
    if sc.get("readOnlyRootFilesystem") is not True:
        violations.append(
            f"readOnlyRootFilesystem: expected True, got {sc.get('readOnlyRootFilesystem')!r}"
        )

    # --- runAsNonRoot ---
    if sc.get("runAsNonRoot") is not True:
        violations.append(
            f"runAsNonRoot: expected True, got {sc.get('runAsNonRoot')!r}"
        )

    # --- allowPrivilegeEscalation ---
    if sc.get("allowPrivilegeEscalation") is not False:
        violations.append(
            "allowPrivilegeEscalation: expected False, "
            f"got {sc.get('allowPrivilegeEscalation')!r}"
        )

    # --- capabilities.drop includes ALL ---
    caps: dict[str, Any] = sc.get("capabilities") or {}
    drop: list[str] = caps.get("drop") or []
    if "ALL" not in drop:
        violations.append(
            f"capabilities.drop: expected 'ALL' to be present, got {drop!r}"
        )

    return violations


def generate_sandbox_config_toml(config: SandboxConfig) -> str:
    """Return a TOML string for the ``[sandbox]`` section.

    The output is a self-contained TOML fragment suitable for inclusion in the
    runner's ``runtime.*.toml`` configuration files.
    """
    enforce_str = "true" if config.enforce_read_only_root else "false"
    network_str = "true" if config.network_policy_enabled else "false"
    lines: list[str] = [
        "[sandbox]",
        f'runtime_class = "{config.runtime_class}"',
        f'workspace_path = "{config.workspace_path}"',
        f"enforce_read_only_root = {enforce_str}",
        f"network_policy_enabled = {network_str}",
    ]
    return "\n".join(lines) + "\n"
