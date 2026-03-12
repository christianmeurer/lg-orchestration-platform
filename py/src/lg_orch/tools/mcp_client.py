from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lg_orch.logging import get_logger
from lg_orch.tools.runner_client import RunnerClient


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

            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                out.append({**tool, "server_name": server_name})

        log.info("mcp_discover_tools", servers=list(self.server_configs.keys()), count=len(out))
        return out

    def summarize_tools(self, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        discovered = tools if tools is not None else self.discover_tools()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tool in discovered:
            if not isinstance(tool, dict):
                continue
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

