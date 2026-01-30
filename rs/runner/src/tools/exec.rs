use std::process::Stdio;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};
use tokio::process::Command;
use tokio::time::timeout;

use crate::config::RunnerConfig;
use crate::envelope::ToolEnvelope;
use crate::errors::ApiError;

#[derive(Debug, Deserialize)]
struct ExecIn {
    cmd: String,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default)]
    timeout_s: Option<u64>,
}

fn allowed_cmd(cmd: &str) -> bool {
    matches!(
        cmd,
        "uv" | "python" | "pytest" | "ruff" | "mypy" | "cargo" | "git"
    )
}

pub async fn exec(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: ExecIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    let cmd = inp.cmd.trim();
    if !allowed_cmd(cmd) {
        return Err(ApiError::Forbidden(format!("cmd not allowed: {cmd}")));
    }

    let cwd = inp
        .cwd
        .as_deref()
        .map(|p| super::fs::resolve_under_root(cfg, p))
        .transpose()?;

    let mut c = Command::new(cmd);
    c.args(&inp.args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(cwd) = cwd {
        c.current_dir(cwd);
    } else {
        c.current_dir(&cfg.root_dir);
    }

    let t = Duration::from_secs(inp.timeout_s.unwrap_or(600));
    let child = c.spawn().map_err(|e| ApiError::Other(e.into()))?;

    let out = timeout(t, child.wait_with_output())
        .await
        .map_err(|_| ApiError::Other(anyhow::anyhow!("timeout")))?
        .map_err(|e| ApiError::Other(e.into()))?;

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    let code = out.status.code().unwrap_or(1);
    if code == 0 {
        Ok(ToolEnvelope::ok(
            "exec",
            stdout,
            json!({"cmd": cmd, "args": inp.args, "exit_code": code}),
            0,
        ))
    } else {
        Ok(ToolEnvelope::err(
            "exec",
            code,
            stderr,
            json!({"cmd": cmd, "args": inp.args, "exit_code": code, "stdout": stdout}),
            0,
        ))
    }
}
