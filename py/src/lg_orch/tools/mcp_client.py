from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.tools.runner_client import RunnerClient


def _compute_tools_hash(tools: list[dict[str, Any]]) -> str:
    """SHA-256 of the sorted, canonicalized tools list JSON."""
    canonical = json.dumps(tools, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_timeout(value: object, *, default: int = 20) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, float):
        return max(1, int(value))
    if isinstance(value, str):
        try:
            return max(1, int(value.strip()))
        except ValueError:
            return default
    return default


@dataclass(frozen=True)
class MCPClient:
    runner_client: RunnerClient
    server_configs: dict[str, Any]

    def _server_payload(self, server_name: str) -> dict[str, Any]:
        raw = self.server_configs.get(server_name)
        if not isinstance(raw, dict):
            raise ValueError(f"unknown MCP server: {server_name}")

        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"invalid MCP server command: {server_name}")

        args_raw = raw.get("args", [])
        if not isinstance(args_raw, list):
            raise ValueError(f"invalid MCP server args: {server_name}")
        args: list[str] = []
        for arg in args_raw:
            if not isinstance(arg, str):
                raise ValueError(f"invalid MCP server arg entry: {server_name}")
            args.append(arg)

        cwd_raw = raw.get("cwd")
        cwd: str | None
        if cwd_raw is None:
            cwd = None
        elif isinstance(cwd_raw, str):
            cwd = cwd_raw.strip() or None
        else:
            raise ValueError(f"invalid MCP server cwd: {server_name}")

        env_raw = raw.get("env", {})
        if not isinstance(env_raw, dict):
            raise ValueError(f"invalid MCP server env: {server_name}")
        env: dict[str, str] = {}
        for key, val in env_raw.items():
            if not isinstance(key, str) or not isinstance(val, str):
                raise ValueError(f"invalid MCP server env entry: {server_name}")
            env[key] = val

        timeout_s = _to_timeout(raw.get("timeout_s", 20), default=20)

        payload: dict[str, Any] = {
            "command": command.strip(),
            "args": args,
            "env": env,
            "timeout_s": timeout_s,
        }
        if cwd is not None:
            payload["cwd"] = cwd
        return payload

    def discover_tools(self) -> list[dict[str, Any]]:
        log = get_logger()
        out: list[dict[str, Any]] = []

        for server_name in sorted(self.server_configs.keys()):
            try:
                payload = {
                    "server_name": server_name,
                    "server": self._server_payload(server_name),
                }
            except ValueError as exc:
                log.warning(
                    "mcp_discover_server_config_invalid",
                    server=server_name,
                    error=str(exc),
                )
                continue

            env = self.runner_client.execute_tool(tool="mcp_discover", input=payload)
            if bool(env.get("ok", False)) is not True:
                stderr = str(env.get("stderr", ""))
                log.warning("mcp_discover_failed", server=server_name, error=stderr)
                continue

            stdout = env.get("stdout", "")
            if not isinstance(stdout, str) or not stdout.strip():
                continue

            try:
                import json

                tools = json.loads(stdout)
            except Exception:
                log.warning("mcp_discover_invalid_stdout", server=server_name)
                continue

            if not isinstance(tools, list):
                continue

            # Zero-trust: verify schema hash if pinned
            server_cfg_raw = self.server_configs.get(server_name, {})
            expected_hash = (
                str(server_cfg_raw.get("schema_hash", "")).strip().lower()
                if isinstance(server_cfg_raw, dict)
                else ""
            )
            if expected_hash:
                actual_hash = _compute_tools_hash(tools)
                if actual_hash != expected_hash:
                    log.error(
                        "mcp_schema_hash_mismatch",
                        server=server_name,
                        expected=expected_hash,
                        actual=actual_hash,
                    )
                    out.append({
                        "server_name": server_name,
                        "_schema_hash_mismatch": True,
                        "_expected_hash": expected_hash,
                        "_actual_hash": actual_hash,
                    })
                    continue

            actual_hash = _compute_tools_hash(tools)
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                out.append({**tool, "server_name": server_name, "_schema_hash": actual_hash})

        log.info("mcp_discover_tools", servers=list(self.server_configs.keys()), count=len(out))
        return out

    def summarize_tools(self, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        discovered = tools if tools is not None else self.discover_tools()
        valid_tools = [t for t in discovered if not bool(t.get("_schema_hash_mismatch", False))]
        mismatch_servers = [
            str(t.get("server_name", "")) for t in discovered
            if bool(t.get("_schema_hash_mismatch", False))
        ]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tool in valid_tools:
            if not isinstance(tool, dict):
                continue  # type: ignore[unreachable]
            server_name = str(tool.get("server_name", "")).strip() or "unknown"
            grouped.setdefault(server_name, []).append(tool)

        servers: list[dict[str, Any]] = []
        lines: list[str] = []
        for server_name in sorted(grouped.keys()):
            tool_entries = grouped[server_name]
            tool_summaries: list[dict[str, Any]] = []
            names: list[str] = []
            for tool in tool_entries:
                name = str(tool.get("name", "")).strip()
                description = str(tool.get("description", "")).strip()
                if name:
                    names.append(name)
                tool_summaries.append(
                    {
                        "name": name,
                        "description": description,
                        "input_schema": tool.get("inputSchema"),
                    }
                )
            servers.append(
                {
                    "server_name": server_name,
                    "tool_count": len(tool_summaries),
                    "tools": tool_summaries,
                }
            )
            lines.append(f"{server_name}: {', '.join(name for name in names if name) or 'no tools'}")

        return {
            "server_count": len(servers),
            "tool_count": sum(server["tool_count"] for server in servers),
            "servers": servers,
            "summary": "\n".join(lines),
            "mismatch_servers": mismatch_servers,
        }

    def execute_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        log = get_logger()
        payload = {
            "server_name": server_name,
            "tool_name": tool_name,
            "args": args,
            "server": self._server_payload(server_name),
        }
        env = self.runner_client.execute_tool(tool="mcp_execute", input=payload)
        log.info(
            "mcp_execute_tool",
            server=server_name,
            tool=tool_name,
            ok=bool(env.get("ok", False)),
        )
        return env

    def list_resources(self, server_name: str) -> list[dict[str, Any]]:
        """Call mcp_resources_list for the given server. Returns [] on any error."""
        log = get_logger()
        try:
            payload = {
                "server_name": server_name,
                "server": self._server_payload(server_name),
            }
        except ValueError as exc:
            log.warning("mcp_list_resources_config_invalid", server=server_name, error=str(exc))
            return []

        env = self.runner_client.execute_tool(tool="mcp_resources_list", input=payload)
        if not bool(env.get("ok", False)):
            log.warning("mcp_list_resources_failed", server=server_name, error=str(env.get("stderr", "")))
            return []

        stdout = env.get("stdout", "")
        if not isinstance(stdout, str) or not stdout.strip():
            return []
        try:
            resources = json.loads(stdout)
        except Exception:
            return []
        if not isinstance(resources, list):
            return []
        return [r for r in resources if isinstance(r, dict)]

    def read_resource(self, server_name: str, resource_uri: str) -> dict[str, Any]:
        """Call mcp_resource_read for the given server and URI. Returns {} on any error."""
        log = get_logger()
        try:
            payload = {
                "server_name": server_name,
                "resource_uri": resource_uri,
                "server": self._server_payload(server_name),
            }
        except ValueError as exc:
            log.warning("mcp_read_resource_config_invalid", server=server_name, error=str(exc))
            return {}

        env = self.runner_client.execute_tool(tool="mcp_resource_read", input=payload)
        if not bool(env.get("ok", False)):
            log.warning("mcp_read_resource_failed", server=server_name, uri=resource_uri, error=str(env.get("stderr", "")))
            return {}

        stdout = env.get("stdout", "")
        if not isinstance(stdout, str) or not stdout.strip():
            return {}
        try:
            result = json.loads(stdout)
        except Exception:
            return {}
        return result if isinstance(result, dict) else {}

    def list_prompts(self, server_name: str) -> list[dict[str, Any]]:
        """Call mcp_prompts_list for the given server. Returns [] on any error."""
        log = get_logger()
        try:
            payload = {
                "server_name": server_name,
                "server": self._server_payload(server_name),
            }
        except ValueError as exc:
            log.warning("mcp_list_prompts_config_invalid", server=server_name, error=str(exc))
            return []

        env = self.runner_client.execute_tool(tool="mcp_prompts_list", input=payload)
        if not bool(env.get("ok", False)):
            log.warning("mcp_list_prompts_failed", server=server_name, error=str(env.get("stderr", "")))
            return []

        stdout = env.get("stdout", "")
        if not isinstance(stdout, str) or not stdout.strip():
            return []
        try:
            prompts = json.loads(stdout)
        except Exception:
            return []
        if not isinstance(prompts, list):
            return []
        return [p for p in prompts if isinstance(p, dict)]

    def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call mcp_prompt_get for the given server and prompt name. Returns {} on any error."""
        log = get_logger()
        try:
            payload = {
                "server_name": server_name,
                "prompt_name": prompt_name,
                "arguments": arguments or {},
                "server": self._server_payload(server_name),
            }
        except ValueError as exc:
            log.warning("mcp_get_prompt_config_invalid", server=server_name, error=str(exc))
            return {}

        env = self.runner_client.execute_tool(tool="mcp_prompt_get", input=payload)
        if not bool(env.get("ok", False)):
            log.warning("mcp_get_prompt_failed", server=server_name, prompt=prompt_name, error=str(env.get("stderr", "")))
            return {}

        stdout = env.get("stdout", "")
        if not isinstance(stdout, str) or not stdout.strip():
            return {}
        try:
            result = json.loads(stdout)
        except Exception:
            return {}
        return result if isinstance(result, dict) else {}

    def summarize_capabilities(self) -> dict[str, Any]:
        """
        Returns a summary of the MCP surface area available across all servers.
        tools_count: total discovered tools
        resources_count: total discovered resources (requires runner)
        prompts_count: total discovered prompts (requires runner)
        servers: list of {server_name, tools, resources, prompts}
        """
        servers: list[dict[str, Any]] = []
        total_tools = 0
        total_resources = 0
        total_prompts = 0

        for server_name in sorted(self.server_configs.keys()):
            try:
                self._server_payload(server_name)
            except ValueError:
                continue

            tools_data = self.discover_tools()
            server_tools = [t for t in tools_data if t.get("server_name") == server_name and not t.get("_schema_hash_mismatch")]
            resources = self.list_resources(server_name)
            prompts = self.list_prompts(server_name)

            total_tools += len(server_tools)
            total_resources += len(resources)
            total_prompts += len(prompts)

            servers.append({
                "server_name": server_name,
                "tools": len(server_tools),
                "resources": len(resources),
                "prompts": len(prompts),
            })

        return {
            "tools_count": total_tools,
            "resources_count": total_resources,
            "prompts_count": total_prompts,
            "servers": servers,
        }

