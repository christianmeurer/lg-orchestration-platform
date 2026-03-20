use std::collections::BTreeMap;
use std::sync::OnceLock;
use std::time::Duration;

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::time::timeout;

use crate::config::RunnerConfig;
use crate::envelope::{McpMetadata, RedactionMetadata, ToolEnvelope};
use crate::errors::ApiError;
use crate::tools::fs::resolve_under_root;

const JSONRPC_VERSION: &str = "2.0";
const DEFAULT_MCP_TIMEOUT_S: u64 = 20;

#[derive(Debug, Deserialize, Clone)]
struct McpServerConfigIn {
    command: String,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default)]
    env: BTreeMap<String, String>,
    #[serde(default)]
    timeout_s: Option<u64>,
}

#[derive(Debug, Deserialize)]
struct McpDiscoverIn {
    server_name: String,
    server: McpServerConfigIn,
    #[serde(default)]
    list_params: Option<Value>,
}

#[derive(Debug, Deserialize)]
struct McpExecuteIn {
    server_name: String,
    tool_name: String,
    #[serde(default = "default_args")]
    args: Value,
    server: McpServerConfigIn,
}

#[derive(Debug, Deserialize)]
struct McpResourcesListIn {
    server_name: String,
    server: McpServerConfigIn,
    #[serde(default)]
    list_params: Option<Value>,
}

#[derive(Debug, Deserialize)]
struct McpResourceReadIn {
    server_name: String,
    resource_uri: String,
    server: McpServerConfigIn,
}

#[derive(Debug, Deserialize)]
struct McpPromptsListIn {
    server_name: String,
    server: McpServerConfigIn,
    #[serde(default)]
    list_params: Option<Value>,
}

#[derive(Debug, Deserialize)]
struct McpPromptGetIn {
    server_name: String,
    prompt_name: String,
    #[serde(default = "default_args")]
    arguments: Value,
    server: McpServerConfigIn,
}

fn default_args() -> Value {
    json!({})
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct JsonRpcErrorObject {
    code: i64,
    message: String,
    #[serde(default)]
    data: Option<Value>,
}

#[derive(Debug, Default, Clone, Copy)]
struct RedactionStats {
    total: u32,
    paths: u32,
    usernames: u32,
    ip_addresses: u32,
}

impl RedactionStats {
    fn into_metadata(self) -> RedactionMetadata {
        RedactionMetadata {
            total: self.total,
            paths: self.paths,
            usernames: self.usernames,
            ip_addresses: self.ip_addresses,
        }
    }
}

struct McpStdioClient {
    _child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    timeout: Duration,
    next_id: u64,
}

impl McpStdioClient {
    async fn connect(cfg: &RunnerConfig, server: &McpServerConfigIn) -> Result<Self, ApiError> {
        let command = server.command.trim();
        if command.is_empty() {
            return Err(ApiError::BadRequest(
                "mcp server command is required".to_string(),
            ));
        }

        // Security: MCP server commands must pass the same allowlist as exec tool calls.
        let bin_name = std::path::Path::new(command)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(command);
        if !crate::config::ALLOWED_EXEC_COMMANDS.contains(&bin_name) {
            return Err(ApiError::Forbidden(
                "mcp server command not in allowlist".to_string(),
            ));
        }

        let mut cmd = Command::new(command);
        cmd.args(&server.args)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);

        if let Some(cwd) = &server.cwd {
            let cwd_path = resolve_under_root(cfg, cwd)?;
            cmd.current_dir(cwd_path);
        } else {
            cmd.current_dir(&cfg.root_dir);
        }

        for (key, value) in &server.env {
            cmd.env(key, value);
        }

        let mut child = cmd.spawn().map_err(|e| {
            ApiError::Other(anyhow::anyhow!(
                "failed to spawn MCP server '{}': {e}",
                command
            ))
        })?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| ApiError::Other(anyhow::anyhow!("failed to capture MCP stdin")))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| ApiError::Other(anyhow::anyhow!("failed to capture MCP stdout")))?;

        Ok(Self {
            _child: child,
            stdin,
            stdout: BufReader::new(stdout),
            timeout: Duration::from_secs(server.timeout_s.unwrap_or(DEFAULT_MCP_TIMEOUT_S)),
            next_id: 1,
        })
    }

    async fn send_request(&mut self, method: &str, params: Value) -> Result<Value, ApiError> {
        let id = self.next_id;
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| ApiError::Other(anyhow::anyhow!("json-rpc request id overflow")))?;

        let payload = json!({
            "jsonrpc": JSONRPC_VERSION,
            "id": id,
            "method": method,
            "params": params,
        });
        self.write_message(&payload).await?;

        loop {
            let msg = self.read_message().await?;
            let response_id = msg.get("id").and_then(jsonrpc_id_to_u64);
            if response_id != Some(id) {
                continue;
            }

            if let Some(error_val) = msg.get("error") {
                let parsed_error: JsonRpcErrorObject = serde_json::from_value(error_val.clone())
                    .map_err(|e| {
                        ApiError::BadRequest(format!("invalid json-rpc error payload: {e}"))
                    })?;
                let data_suffix = parsed_error
                    .data
                    .as_ref()
                    .map(|d| format!(":{}", serde_json::to_string(d).unwrap_or_default()))
                    .unwrap_or_default();
                return Err(ApiError::BadRequest(format!(
                    "jsonrpc_error:{}:{}{}",
                    parsed_error.code, parsed_error.message, data_suffix
                )));
            }

            if let Some(result) = msg.get("result") {
                return Ok(result.clone());
            }

            return Err(ApiError::BadRequest(
                "json-rpc response missing result/error".to_string(),
            ));
        }
    }

    async fn send_notification(&mut self, method: &str, params: Value) -> Result<(), ApiError> {
        let payload = json!({
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": params,
        });
        self.write_message(&payload).await
    }

    async fn write_message(&mut self, payload: &Value) -> Result<(), ApiError> {
        let body = serde_json::to_vec(payload).map_err(|e| {
            ApiError::BadRequest(format!("failed to serialize json-rpc payload: {e}"))
        })?;
        let header = format!("Content-Length: {}\r\n\r\n", body.len());
        timeout(self.timeout, self.stdin.write_all(header.as_bytes()))
            .await
            .map_err(|_| ApiError::BadRequest("timeout writing json-rpc header".to_string()))
            .and_then(|r| r.map_err(|e| ApiError::Other(e.into())))?;

        timeout(self.timeout, self.stdin.write_all(&body))
            .await
            .map_err(|_| ApiError::BadRequest("timeout writing json-rpc body".to_string()))
            .and_then(|r| r.map_err(|e| ApiError::Other(e.into())))?;

        timeout(self.timeout, self.stdin.flush())
            .await
            .map_err(|_| ApiError::BadRequest("timeout flushing json-rpc request".to_string()))
            .and_then(|r| r.map_err(|e| ApiError::Other(e.into())))
    }

    async fn read_message(&mut self) -> Result<Value, ApiError> {
        let mut content_length: Option<usize> = None;
        loop {
            let mut line = String::new();
            let read = timeout(self.timeout, self.stdout.read_line(&mut line))
                .await
                .map_err(|_| ApiError::BadRequest("timeout reading json-rpc header".to_string()))
                .and_then(|r| r.map_err(|e| ApiError::Other(e.into())))?;

            if read == 0 {
                return Err(ApiError::BadRequest(
                    "unexpected EOF from MCP server".to_string(),
                ));
            }

            let trimmed = line.trim_end_matches(['\r', '\n']);
            if trimmed.is_empty() {
                break;
            }

            let mut split = trimmed.splitn(2, ':');
            let key = split.next().unwrap_or_default().trim();
            let value = split.next().unwrap_or_default().trim();
            if key.eq_ignore_ascii_case("content-length") {
                content_length = Some(value.parse::<usize>().map_err(|e| {
                    ApiError::BadRequest(format!("invalid Content-Length header: {e}"))
                })?);
            }
        }

        let len = content_length.ok_or_else(|| {
            ApiError::BadRequest("missing Content-Length header in MCP response".to_string())
        })?;

        let mut body = vec![0_u8; len];
        timeout(self.timeout, self.stdout.read_exact(&mut body))
            .await
            .map_err(|_| ApiError::BadRequest("timeout reading json-rpc body".to_string()))
            .and_then(|r| r.map_err(|e| ApiError::Other(e.into())))?;

        serde_json::from_slice(&body)
            .map_err(|e| ApiError::BadRequest(format!("invalid json-rpc response JSON: {e}")))
    }
}

fn jsonrpc_id_to_u64(v: &Value) -> Option<u64> {
    match v {
        Value::Number(n) => n.as_u64(),
        Value::String(s) => s.parse::<u64>().ok(),
        _ => None,
    }
}

fn path_regex_windows() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r#"(?i)\b[A-Z]:\\(?:[^\\/:*?"<>|\r\n]+\\?)+"#)
            .expect("windows path regex must compile")
    })
}

fn path_regex_unix() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?:/Users|/home|/opt|/var|/tmp|/etc|/mnt|/srv)(?:/[A-Za-z0-9._-]+)+")
            .expect("unix path regex must compile")
    })
}

fn username_regex_windows() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r#"(?i)(\\Users\\)([^\\/:*?"<>|\r\n]+)"#)
            .expect("windows username regex must compile")
    })
}

fn username_regex_unix() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(/(?:home|Users)/)([^/\x20\t\r\n]+)")
            .expect("unix username regex must compile")
    })
}

fn explicit_username_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)\b(user(?:name)?\s*[:=]\s*|user\s+)([A-Za-z0-9._-]{1,64})")
            .expect("explicit username regex must compile")
    })
}

fn ip_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
            .expect("ip regex must compile")
    })
}

fn deterministic_token(prefix: &str, value: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(value.as_bytes());
    let digest = hasher.finalize();
    let mut short_hex = String::with_capacity(12);
    for b in digest.iter().take(6) {
        short_hex.push_str(&format!("{b:02x}"));
    }
    format!("[{prefix}_{short_hex}]")
}

fn redact_string(input: &str, stats: &mut RedactionStats) -> String {
    let mut out = input.to_string();

    out = ip_regex()
        .replace_all(&out, |caps: &regex::Captures<'_>| {
            let matched = caps.get(0).map(|m| m.as_str()).unwrap_or_default();
            stats.total = stats.total.saturating_add(1);
            stats.ip_addresses = stats.ip_addresses.saturating_add(1);
            deterministic_token("IP", matched)
        })
        .to_string();

    out = explicit_username_regex()
        .replace_all(&out, |caps: &regex::Captures<'_>| {
            let prefix = caps.get(1).map(|m| m.as_str()).unwrap_or_default();
            let username = caps.get(2).map(|m| m.as_str()).unwrap_or_default();
            stats.total = stats.total.saturating_add(1);
            stats.usernames = stats.usernames.saturating_add(1);
            format!("{prefix}{}", deterministic_token("USER", username))
        })
        .to_string();

    out = username_regex_windows()
        .replace_all(&out, |caps: &regex::Captures<'_>| {
            let prefix = caps.get(1).map(|m| m.as_str()).unwrap_or_default();
            let username = caps.get(2).map(|m| m.as_str()).unwrap_or_default();
            stats.total = stats.total.saturating_add(1);
            stats.usernames = stats.usernames.saturating_add(1);
            format!("{prefix}{}", deterministic_token("USER", username))
        })
        .to_string();

    out = username_regex_unix()
        .replace_all(&out, |caps: &regex::Captures<'_>| {
            let prefix = caps.get(1).map(|m| m.as_str()).unwrap_or_default();
            let username = caps.get(2).map(|m| m.as_str()).unwrap_or_default();
            stats.total = stats.total.saturating_add(1);
            stats.usernames = stats.usernames.saturating_add(1);
            format!("{prefix}{}", deterministic_token("USER", username))
        })
        .to_string();

    out = path_regex_windows()
        .replace_all(&out, |caps: &regex::Captures<'_>| {
            let path = caps.get(0).map(|m| m.as_str()).unwrap_or_default();
            stats.total = stats.total.saturating_add(1);
            stats.paths = stats.paths.saturating_add(1);
            deterministic_token("PATH", path)
        })
        .to_string();

    path_regex_unix()
        .replace_all(&out, |caps: &regex::Captures<'_>| {
            let path = caps.get(0).map(|m| m.as_str()).unwrap_or_default();
            stats.total = stats.total.saturating_add(1);
            stats.paths = stats.paths.saturating_add(1);
            deterministic_token("PATH", path)
        })
        .to_string()
}

fn redact_value(value: &Value, stats: &mut RedactionStats) -> Value {
    match value {
        Value::String(s) => Value::String(redact_string(s, stats)),
        Value::Array(arr) => Value::Array(arr.iter().map(|v| redact_value(v, stats)).collect()),
        Value::Object(map) => {
            let mut out = serde_json::Map::with_capacity(map.len());
            for (k, v) in map {
                out.insert(k.clone(), redact_value(v, stats));
            }
            Value::Object(out)
        }
        _ => value.clone(),
    }
}

async fn initialize(client: &mut McpStdioClient) -> Result<(), ApiError> {
    let initialize_params = json!({
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {
            "name": "lg-runner",
            "version": env!("CARGO_PKG_VERSION")
        }
    });
    let _initialize_result = client.send_request("initialize", initialize_params).await?;
    client.send_notification("initialized", json!({})).await
}

pub async fn mcp_discover(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: McpDiscoverIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;

    let server_name = inp.server_name.trim().to_string();
    if server_name.is_empty() {
        return Err(ApiError::BadRequest("server_name is required".to_string()));
    }

    let mut outbound_stats = RedactionStats::default();
    let mut inbound_stats = RedactionStats::default();

    let list_params_raw = inp.list_params.unwrap_or_else(|| json!({}));
    let list_params = redact_value(&list_params_raw, &mut outbound_stats);

    let mut client = McpStdioClient::connect(cfg, &inp.server).await?;
    initialize(&mut client).await?;
    let tools_result = client.send_request("tools/list", list_params).await?;
    let tools_result_redacted = redact_value(&tools_result, &mut inbound_stats);

    let tools = tools_result_redacted
        .get("tools")
        .cloned()
        .unwrap_or_else(|| Value::Array(vec![]));

    Ok(ToolEnvelope::ok(
        "mcp_discover",
        serde_json::to_string_pretty(&tools).unwrap_or_default(),
        json!({
            "server_name": server_name,
            "jsonrpc": "2.0",
            "handshake_completed": true,
            "method": "tools/list",
            "result": tools_result_redacted,
            "redaction": {
                "outbound": outbound_stats.into_metadata(),
                "inbound": inbound_stats.into_metadata()
            }
        }),
        0,
    )
    .with_mcp(McpMetadata {
        server_name,
        handshake_completed: true,
        outbound_redactions: outbound_stats.into_metadata(),
        inbound_redactions: inbound_stats.into_metadata(),
    }))
}

pub async fn mcp_execute(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: McpExecuteIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;

    let server_name = inp.server_name.trim().to_string();
    if server_name.is_empty() {
        return Err(ApiError::BadRequest("server_name is required".to_string()));
    }
    let tool_name = inp.tool_name.trim().to_string();
    if tool_name.is_empty() {
        return Err(ApiError::BadRequest("tool_name is required".to_string()));
    }

    let mut outbound_stats = RedactionStats::default();
    let mut inbound_stats = RedactionStats::default();
    let redacted_args = redact_value(&inp.args, &mut outbound_stats);

    let mut client = McpStdioClient::connect(cfg, &inp.server).await?;
    initialize(&mut client).await?;

    let call_params = json!({"name": tool_name, "arguments": redacted_args});
    let call_result = client.send_request("tools/call", call_params).await;

    let mcp_meta = McpMetadata {
        server_name: server_name.clone(),
        handshake_completed: true,
        outbound_redactions: outbound_stats.into_metadata(),
        inbound_redactions: RedactionMetadata::default(),
    };

    match call_result {
        Ok(result) => {
            let inbound_redacted = redact_value(&result, &mut inbound_stats);
            let mut env = ToolEnvelope::ok(
                "mcp_execute",
                serde_json::to_string_pretty(&inbound_redacted).unwrap_or_default(),
                json!({
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "jsonrpc": "2.0",
                    "handshake_completed": true,
                    "method": "tools/call",
                    "result": inbound_redacted,
                    "redaction": {
                        "outbound": outbound_stats.into_metadata(),
                        "inbound": inbound_stats.into_metadata()
                    }
                }),
                0,
            );
            env.mcp = Some(McpMetadata {
                inbound_redactions: inbound_stats.into_metadata(),
                ..mcp_meta
            });
            Ok(env)
        }
        Err(err) => {
            let message = err.to_string();
            let mut env = ToolEnvelope::err(
                "mcp_execute",
                1,
                message.clone(),
                json!({
                    "error": message,
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "jsonrpc": "2.0",
                    "handshake_completed": true,
                    "redaction": {
                        "outbound": outbound_stats.into_metadata(),
                        "inbound": inbound_stats.into_metadata()
                    }
                }),
                0,
            );
            env.mcp = Some(McpMetadata {
                inbound_redactions: inbound_stats.into_metadata(),
                ..mcp_meta
            });
            Ok(env)
        }
    }
}

pub async fn mcp_resources_list(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: McpResourcesListIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;

    let server_name = inp.server_name.trim().to_string();
    if server_name.is_empty() {
        return Err(ApiError::BadRequest("server_name is required".to_string()));
    }

    let mut outbound_stats = RedactionStats::default();
    let mut inbound_stats = RedactionStats::default();

    let list_params_raw = inp.list_params.unwrap_or_else(|| json!({}));
    let list_params = redact_value(&list_params_raw, &mut outbound_stats);

    let mut client = McpStdioClient::connect(cfg, &inp.server).await?;
    initialize(&mut client).await?;
    let result = client.send_request("resources/list", list_params).await?;
    let result_redacted = redact_value(&result, &mut inbound_stats);

    let resources = result_redacted
        .get("resources")
        .cloned()
        .unwrap_or_else(|| Value::Array(vec![]));

    Ok(ToolEnvelope::ok(
        "mcp_resources_list",
        serde_json::to_string_pretty(&resources).unwrap_or_default(),
        json!({
            "server_name": server_name,
            "method": "resources/list",
            "result": result_redacted,
            "redaction": {
                "outbound": outbound_stats.into_metadata(),
                "inbound": inbound_stats.into_metadata()
            }
        }),
        0,
    )
    .with_mcp(McpMetadata {
        server_name,
        handshake_completed: true,
        outbound_redactions: outbound_stats.into_metadata(),
        inbound_redactions: inbound_stats.into_metadata(),
    }))
}

pub async fn mcp_resource_read(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: McpResourceReadIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;

    let server_name = inp.server_name.trim().to_string();
    if server_name.is_empty() {
        return Err(ApiError::BadRequest("server_name is required".to_string()));
    }
    let resource_uri = inp.resource_uri.trim().to_string();
    if resource_uri.is_empty() {
        return Err(ApiError::BadRequest("resource_uri is required".to_string()));
    }

    let mut outbound_stats = RedactionStats::default();
    let mut inbound_stats = RedactionStats::default();

    let uri_redacted = redact_string(&resource_uri, &mut outbound_stats);

    let mut client = McpStdioClient::connect(cfg, &inp.server).await?;
    initialize(&mut client).await?;
    let result = client
        .send_request("resources/read", json!({"uri": uri_redacted}))
        .await?;
    let result_redacted = redact_value(&result, &mut inbound_stats);

    Ok(ToolEnvelope::ok(
        "mcp_resource_read",
        serde_json::to_string_pretty(&result_redacted).unwrap_or_default(),
        json!({
            "server_name": server_name,
            "resource_uri": uri_redacted,
            "method": "resources/read",
            "result": result_redacted,
            "redaction": {
                "outbound": outbound_stats.into_metadata(),
                "inbound": inbound_stats.into_metadata()
            }
        }),
        0,
    )
    .with_mcp(McpMetadata {
        server_name,
        handshake_completed: true,
        outbound_redactions: outbound_stats.into_metadata(),
        inbound_redactions: inbound_stats.into_metadata(),
    }))
}

pub async fn mcp_prompts_list(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: McpPromptsListIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;

    let server_name = inp.server_name.trim().to_string();
    if server_name.is_empty() {
        return Err(ApiError::BadRequest("server_name is required".to_string()));
    }

    let mut outbound_stats = RedactionStats::default();
    let mut inbound_stats = RedactionStats::default();

    let list_params_raw = inp.list_params.unwrap_or_else(|| json!({}));
    let list_params = redact_value(&list_params_raw, &mut outbound_stats);

    let mut client = McpStdioClient::connect(cfg, &inp.server).await?;
    initialize(&mut client).await?;
    let result = client.send_request("prompts/list", list_params).await?;
    let result_redacted = redact_value(&result, &mut inbound_stats);

    let prompts = result_redacted
        .get("prompts")
        .cloned()
        .unwrap_or_else(|| Value::Array(vec![]));

    Ok(ToolEnvelope::ok(
        "mcp_prompts_list",
        serde_json::to_string_pretty(&prompts).unwrap_or_default(),
        json!({
            "server_name": server_name,
            "method": "prompts/list",
            "result": result_redacted,
            "redaction": {
                "outbound": outbound_stats.into_metadata(),
                "inbound": inbound_stats.into_metadata()
            }
        }),
        0,
    )
    .with_mcp(McpMetadata {
        server_name,
        handshake_completed: true,
        outbound_redactions: outbound_stats.into_metadata(),
        inbound_redactions: inbound_stats.into_metadata(),
    }))
}

pub async fn mcp_prompt_get(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: McpPromptGetIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;

    let server_name = inp.server_name.trim().to_string();
    if server_name.is_empty() {
        return Err(ApiError::BadRequest("server_name is required".to_string()));
    }
    let prompt_name = inp.prompt_name.trim().to_string();
    if prompt_name.is_empty() {
        return Err(ApiError::BadRequest("prompt_name is required".to_string()));
    }

    let mut outbound_stats = RedactionStats::default();
    let mut inbound_stats = RedactionStats::default();

    let redacted_args = redact_value(&inp.arguments, &mut outbound_stats);

    let mut client = McpStdioClient::connect(cfg, &inp.server).await?;
    initialize(&mut client).await?;
    let result = client
        .send_request(
            "prompts/get",
            json!({"name": prompt_name, "arguments": redacted_args}),
        )
        .await?;
    let result_redacted = redact_value(&result, &mut inbound_stats);

    Ok(ToolEnvelope::ok(
        "mcp_prompt_get",
        serde_json::to_string_pretty(&result_redacted).unwrap_or_default(),
        json!({
            "server_name": server_name,
            "prompt_name": prompt_name,
            "method": "prompts/get",
            "result": result_redacted,
            "redaction": {
                "outbound": outbound_stats.into_metadata(),
                "inbound": inbound_stats.into_metadata()
            }
        }),
        0,
    )
    .with_mcp(McpMetadata {
        server_name,
        handshake_completed: true,
        outbound_redactions: outbound_stats.into_metadata(),
        inbound_redactions: inbound_stats.into_metadata(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::RunnerConfig;
    use std::path::Path;
    use std::path::PathBuf;

    fn mock_mcp_server_script(temp_dir: &Path) -> PathBuf {
        let script = temp_dir.join("mock_mcp_server.py");
        let content = r##"import json
import sys


def read_message():
    content_length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode("utf-8", errors="replace").strip("\r\n")
        if line == "":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip().lower() == "content-length":
                content_length = int(value.strip())

    if content_length is None:
        return None
    body = sys.stdin.buffer.read(content_length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


while True:
    msg = read_message()
    if msg is None:
        break
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        write_message(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock", "version": "1.0"},
                },
            }
        )
        continue

    if method == "initialized":
        continue

    if method == "tools/list":
        if params.get("force_error") is True:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32001,
                        "message": "forced list error",
                        "data": {"reason": "forced"},
                    },
                }
            )
            continue
        write_message(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo tool",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
        )
        continue

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if name == "fail_tool":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32050,
                        "message": "forced call error",
                        "data": {"tool": name},
                    },
                }
            )
            continue

        write_message(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(arguments)}],
                    "isError": False,
                },
            }
        )
        continue

    if method == "resources/list":
        write_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "resources": [
                    {"uri": "file:///repo/README.md", "name": "README", "mimeType": "text/markdown"}
                ]
            }
        })
        continue

    if method == "resources/read":
        uri = params.get("uri", "")
        write_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "contents": [{"uri": uri, "mimeType": "text/plain", "text": "# Mock Resource Content"}]
            }
        })
        continue

    if method == "prompts/list":
        write_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "prompts": [
                    {"name": "summarize", "description": "Summarize a file", "arguments": [{"name": "path", "required": True}]}
                ]
            }
        })
        continue

    if method == "prompts/get":
        name = params.get("name", "")
        args = params.get("arguments", {})
        write_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "description": f"Prompt: {name}",
                "messages": [{"role": "user", "content": {"type": "text", "text": f"Summarize: {args}"}}]
            }
        })
        continue

    if req_id is not None:
        write_message(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
"##;
        std::fs::write(&script, content).expect("write mock mcp server");
        script
    }

    #[test]
    fn test_redact_string_detects_path_username_and_ip() {
        let mut stats = RedactionStats::default();
        let input = "User chris path C:\\Users\\chris\\repo and /home/chris/.ssh with 192.168.1.20";
        let out = redact_string(input, &mut stats);

        assert!(!out.contains("chris"));
        assert!(!out.contains("192.168.1.20"));
        assert!(stats.total >= 3);
        assert!(stats.usernames >= 1);
        assert!(stats.paths >= 1);
        assert!(stats.ip_addresses >= 1);
    }

    #[test]
    fn test_redact_value_preserves_shape() {
        let mut stats = RedactionStats::default();
        let input = json!({
            "nested": [
                {"v": "C:\\Users\\chris\\x"},
                {"v": "10.1.2.3"}
            ],
            "ok": true,
            "n": 7
        });

        let out = redact_value(&input, &mut stats);
        assert!(out.is_object());
        assert_eq!(out["ok"], json!(true));
        assert_eq!(out["n"], json!(7));
        assert!(!out.to_string().contains("chris"));
        assert!(!out.to_string().contains("10.1.2.3"));
        assert!(stats.total >= 2);
    }

    #[tokio::test]
    async fn test_mcp_discover_requires_server_name() {
        let td = tempfile::tempdir().expect("tempdir");
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");
        let result = mcp_discover(
            &cfg,
            json!({
                "server_name": "",
                "server": {"command": "python", "args": ["-V"]}
            }),
        )
        .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_mcp_execute_requires_tool_name() {
        let td = tempfile::tempdir().expect("tempdir");
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");
        let result = mcp_execute(
            &cfg,
            json!({
                "server_name": "s1",
                "tool_name": "",
                "server": {"command": "python", "args": ["-V"]}
            }),
        )
        .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_mcp_discover_jsonrpc_handshake_and_tools_list() {
        let td = tempfile::tempdir().expect("tempdir");
        let script = mock_mcp_server_script(td.path());
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");

        let env = mcp_discover(
            &cfg,
            json!({
                "server_name": "mock",
                "server": {
                    "command": "python",
                    "args": [script.to_string_lossy().to_string()]
                }
            }),
        )
        .await
        .expect("mcp discover should succeed");

        assert!(env.ok);
        let tools: Value = serde_json::from_str(&env.stdout).expect("stdout json");
        let first_name = tools
            .as_array()
            .and_then(|arr| arr.first())
            .and_then(|v| v.get("name"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        assert_eq!(first_name, "echo");

        let mcp = env.mcp.expect("mcp metadata required");
        assert!(mcp.handshake_completed);
        assert_eq!(mcp.server_name, "mock");
    }

    #[tokio::test]
    async fn test_mcp_execute_jsonrpc_call_and_redaction_counts() {
        let td = tempfile::tempdir().expect("tempdir");
        let script = mock_mcp_server_script(td.path());
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");

        let env = mcp_execute(
            &cfg,
            json!({
                "server_name": "mock",
                "tool_name": "echo",
                "args": {
                    "path": "C:\\Users\\chris\\project\\file.txt",
                    "home": "/home/chris/.ssh/id_rsa",
                    "ip": "192.168.1.20"
                },
                "server": {
                    "command": "python",
                    "args": [script.to_string_lossy().to_string()]
                }
            }),
        )
        .await
        .expect("mcp execute should return envelope");

        assert!(env.ok);
        assert!(!env.stdout.contains("chris"));
        assert!(!env.stdout.contains("192.168.1.20"));

        let mcp_meta = env.mcp.expect("mcp metadata");
        assert!(mcp_meta.outbound_redactions.total >= 3);
        assert!(mcp_meta.outbound_redactions.paths >= 1);
        assert!(mcp_meta.outbound_redactions.usernames >= 1);
        assert!(mcp_meta.outbound_redactions.ip_addresses >= 1);
    }

    #[tokio::test]
    async fn test_mcp_resources_list_returns_resources() {
        let td = tempfile::tempdir().expect("tempdir");
        let script = mock_mcp_server_script(td.path());
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");

        let env = mcp_resources_list(
            &cfg,
            json!({
                "server_name": "mock",
                "server": {
                    "command": "python",
                    "args": [script.to_string_lossy().to_string()]
                }
            }),
        )
        .await
        .expect("mcp_resources_list should succeed");

        assert!(env.ok);
        assert!(env.stdout.contains("README"));
        let mcp = env.mcp.expect("mcp metadata");
        assert!(mcp.handshake_completed);
    }

    #[tokio::test]
    async fn test_mcp_resource_read_returns_contents() {
        let td = tempfile::tempdir().expect("tempdir");
        let script = mock_mcp_server_script(td.path());
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");

        let env = mcp_resource_read(
            &cfg,
            json!({
                "server_name": "mock",
                "resource_uri": "file:///repo/README.md",
                "server": {
                    "command": "python",
                    "args": [script.to_string_lossy().to_string()]
                }
            }),
        )
        .await
        .expect("mcp_resource_read should succeed");

        assert!(env.ok);
        assert!(env.stdout.contains("Mock Resource Content"));
    }

    #[tokio::test]
    async fn test_mcp_prompts_list_returns_prompts() {
        let td = tempfile::tempdir().expect("tempdir");
        let script = mock_mcp_server_script(td.path());
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");

        let env = mcp_prompts_list(
            &cfg,
            json!({
                "server_name": "mock",
                "server": {
                    "command": "python",
                    "args": [script.to_string_lossy().to_string()]
                }
            }),
        )
        .await
        .expect("mcp_prompts_list should succeed");

        assert!(env.ok);
        assert!(env.stdout.contains("summarize"));
    }

    #[tokio::test]
    async fn test_mcp_prompt_get_returns_messages() {
        let td = tempfile::tempdir().expect("tempdir");
        let script = mock_mcp_server_script(td.path());
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");

        let env = mcp_prompt_get(
            &cfg,
            json!({
                "server_name": "mock",
                "prompt_name": "summarize",
                "arguments": {"path": "README.md"},
                "server": {
                    "command": "python",
                    "args": [script.to_string_lossy().to_string()]
                }
            }),
        )
        .await
        .expect("mcp_prompt_get should succeed");

        assert!(env.ok);
        let parsed: Value = serde_json::from_str(&env.stdout).expect("stdout json");
        assert!(parsed.get("messages").is_some());
    }

    #[tokio::test]
    async fn test_mcp_resources_list_requires_server_name() {
        let td = tempfile::tempdir().expect("tempdir");
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");
        let result = mcp_resources_list(
            &cfg,
            json!({
                "server_name": "",
                "server": {"command": "python", "args": ["-V"]}
            }),
        )
        .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_mcp_resource_read_requires_uri() {
        let td = tempfile::tempdir().expect("tempdir");
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");
        let result = mcp_resource_read(
            &cfg,
            json!({
                "server_name": "mock",
                "resource_uri": "",
                "server": {"command": "python", "args": ["-V"]}
            }),
        )
        .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_mcp_execute_propagates_jsonrpc_error() {
        let td = tempfile::tempdir().expect("tempdir");
        let script = mock_mcp_server_script(td.path());
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).expect("runner config");

        let env = mcp_execute(
            &cfg,
            json!({
                "server_name": "mock",
                "tool_name": "fail_tool",
                "args": {},
                "server": {
                    "command": "python",
                    "args": [script.to_string_lossy().to_string()]
                }
            }),
        )
        .await
        .expect("mcp execute should return error envelope");

        assert!(!env.ok);
        assert!(env
            .stderr
            .contains("jsonrpc_error:-32050:forced call error"));
    }
}
