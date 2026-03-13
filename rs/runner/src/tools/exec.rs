use std::process::Stdio;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};
use tokio::process::Command;
use tokio::time::timeout;

use crate::approval::{require_approval, ApprovalTokenInput};
use crate::config::RunnerConfig;
use crate::diagnostics::parse_structured_diagnostics;
use crate::envelope::{Diagnostic, ToolEnvelope};
use crate::errors::ApiError;
use crate::sandbox::{SandboxBackend, SandboxPolicy};
use crate::tools::snapshot_for_operation;

const STDERR_ARTIFACT_MAX_CHARS: usize = 8_000;

#[derive(Debug, Deserialize)]
struct ExecIn {
    cmd: String,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default)]
    timeout_s: Option<u64>,
    #[serde(default)]
    approval: Option<ApprovalTokenInput>,
}

fn allowed_cmd(cmd: &str) -> bool {
    matches!(
        cmd,
        "uv" | "python" | "pytest" | "ruff" | "mypy" | "cargo" | "git"
    )
}

fn truncate_chars(input: &str, max_chars: usize) -> (String, bool) {
    let total = input.chars().count();
    if total <= max_chars {
        return (input.to_string(), false);
    }
    let truncated: String = input.chars().take(max_chars).collect();
    (truncated, true)
}

fn diagnostics_to_artifact_value(diagnostics: &[Diagnostic]) -> Value {
    Value::Array(
        diagnostics
            .iter()
            .map(|d| serde_json::to_value(d).unwrap_or(Value::Null))
            .collect(),
    )
}

pub async fn exec(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: ExecIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    let cmd = inp.cmd.trim();
    if !allowed_cmd(cmd) {
        return Err(ApiError::Forbidden(format!("cmd not allowed: {cmd}")));
    }

    let mut operation_class = "non_destructive_exec";
    if is_state_modifying_command(cmd, &inp.args) {
        operation_class = "state_modifying_exec";
    }

    let approval_metadata = if operation_class == "state_modifying_exec" {
        Some(require_approval(
            inp.approval.clone(),
            operation_class,
            "approval:exec:state_modifying",
        )?)
    } else {
        None
    };

    let snapshot_metadata = if operation_class == "state_modifying_exec" {
        Some(snapshot_for_operation(cfg, operation_class).await?)
    } else {
        None
    };

    let sandbox_policy = SandboxPolicy::from_env();
    let sandbox_resolution = sandbox_policy.resolve_backend();
    let isolation = sandbox_resolution.to_isolation_metadata();

    let cwd = inp
        .cwd
        .as_deref()
        .map(|p| super::fs::resolve_under_root(cfg, p))
        .transpose()?;

    let mut c;
    match sandbox_resolution.backend {
        SandboxBackend::LinuxNamespace => {
            let unshare_path = sandbox_policy
                .linux_namespace
                .unshare_bin
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_else(|| "/usr/bin/unshare".to_string());
            c = Command::new(&unshare_path);
            c.args(["--pid", "--mount", "--net", "--fork", "--"])
                .arg(cmd)
                .args(&inp.args);
        }
        _ => {
            c = Command::new(cmd);
            c.args(&inp.args);
        }
    }
    c.stdin(Stdio::null())
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
        let mut env = ToolEnvelope::ok(
            "exec",
            stdout,
            json!({
                "cmd": cmd,
                "args": inp.args,
                "exit_code": code,
                "operation_class": operation_class,
                "isolation_backend": isolation.backend,
                "isolation_degraded": isolation.degraded,
                "isolation_reason": isolation.reason,
                "isolation_policy_constraints": isolation.policy_constraints,
            }),
            0,
        )
        .with_isolation(isolation);
        if let Some(approval) = approval_metadata {
            env = env.with_approval(approval);
        }
        if let Some(snapshot) = snapshot_metadata {
            env = env.with_snapshot(snapshot);
        }
        Ok(env)
    } else {
        let diagnostics = parse_structured_diagnostics(&stderr);
        let (stderr_excerpt, stderr_truncated) = truncate_chars(&stderr, STDERR_ARTIFACT_MAX_CHARS);
        let stderr_chars = stderr.chars().count();
        let diagnostics_artifact = diagnostics_to_artifact_value(&diagnostics);
        let mut env = ToolEnvelope::err(
            "exec",
            code,
            stderr,
            json!({
                "cmd": cmd,
                "args": inp.args,
                "exit_code": code,
                "operation_class": operation_class,
                "isolation_backend": isolation.backend,
                "isolation_degraded": isolation.degraded,
                "isolation_reason": isolation.reason,
                "isolation_policy_constraints": isolation.policy_constraints,
                "stdout": stdout,
                "stderr_excerpt": stderr_excerpt,
                "stderr_truncated": stderr_truncated,
                "stderr_chars": stderr_chars,
                "diagnostics": diagnostics_artifact,
            }),
            0,
        )
        .with_diagnostics(diagnostics)
        .with_isolation(isolation);
        if let Some(approval) = approval_metadata {
            env = env.with_approval(approval);
        }
        if let Some(snapshot) = snapshot_metadata {
            env = env.with_snapshot(snapshot);
        }
        Ok(env)
    }
}

fn is_state_modifying_command(cmd: &str, args: &[String]) -> bool {
    let normalized_cmd = cmd.trim().to_ascii_lowercase();
    if normalized_cmd == "git" {
        let sub = args
            .first()
            .map(|s| s.trim().to_ascii_lowercase())
            .unwrap_or_default();
        return matches!(
            sub.as_str(),
            "commit" | "push" | "apply" | "cherry-pick" | "revert"
        );
    }
    if normalized_cmd == "cargo" {
        let sub = args
            .first()
            .map(|s| s.trim().to_ascii_lowercase())
            .unwrap_or_default();
        return matches!(sub.as_str(), "fix" | "install");
    }
    if normalized_cmd == "python" || normalized_cmd == "uv" {
        let joined = args.join(" ").to_ascii_lowercase();
        return joined.contains("write_text(")
            || joined.contains("open(")
            || joined.contains("-m pip install")
            || joined.contains("uv add");
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_allowed_cmd_valid() {
        assert!(allowed_cmd("uv"));
        assert!(allowed_cmd("python"));
        assert!(allowed_cmd("pytest"));
        assert!(allowed_cmd("ruff"));
        assert!(allowed_cmd("mypy"));
        assert!(allowed_cmd("cargo"));
        assert!(allowed_cmd("git"));
    }

    #[test]
    fn test_allowed_cmd_blocked() {
        assert!(!allowed_cmd("rm"));
        assert!(!allowed_cmd("curl"));
        assert!(!allowed_cmd("wget"));
        assert!(!allowed_cmd("sh"));
        assert!(!allowed_cmd("bash"));
        assert!(!allowed_cmd("powershell"));
        assert!(!allowed_cmd("cmd"));
        assert!(!allowed_cmd("node"));
        assert!(!allowed_cmd("npm"));
        assert!(!allowed_cmd(""));
    }

    #[test]
    fn test_allowed_cmd_case_sensitive() {
        assert!(!allowed_cmd("Python"));
        assert!(!allowed_cmd("GIT"));
        assert!(!allowed_cmd("Cargo"));
    }

    #[test]
    fn test_is_state_modifying_command_detects_git_commit() {
        assert!(is_state_modifying_command("git", &["commit".to_string()]));
    }

    #[test]
    fn test_is_state_modifying_command_detects_non_destructive_cargo_test() {
        assert!(!is_state_modifying_command("cargo", &["test".to_string()]));
    }

    #[tokio::test]
    async fn test_exec_forbidden_command() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        let result = exec(&cfg, json!({"cmd": "rm", "args": ["-rf", "/"]})).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_exec_bad_input() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        let result = exec(&cfg, json!({"wrong": "fields"})).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_exec_state_modifying_requires_approval() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        let result = exec(&cfg, json!({"cmd": "git", "args": ["commit"]})).await;
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
    }

    #[test]
    fn test_diagnostics_to_artifact_value_array_shape() {
        let diagnostics = vec![Diagnostic {
            file: "src/main.rs".to_string(),
            line: Some(3),
            column: Some(2),
            code: Some("E0432".to_string()),
            fingerprint: Some("abcd1234".to_string()),
            message: "unresolved import".to_string(),
        }];
        let value = diagnostics_to_artifact_value(&diagnostics);
        let arr = value.as_array().unwrap();
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["file"], "src/main.rs");
        assert_eq!(arr[0]["line"], 3);
        assert_eq!(arr[0]["column"], 2);
        assert_eq!(arr[0]["code"], "E0432");
    }

    #[test]
    fn test_truncate_chars_truncates_and_flags() {
        let (s, truncated) = truncate_chars("abcdefghij", 5);
        assert_eq!(s, "abcde");
        assert!(truncated);
    }

    #[tokio::test]
    async fn test_exec_sandbox_resolution_recorded_in_artifacts() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        let result = exec(&cfg, json!({"cmd": "git", "args": ["--version"]})).await;
        // git --version should succeed; if not available in CI, it may fail with Other
        match result {
            Ok(env) => {
                let artifacts = env.artifacts;
                assert!(artifacts.get("isolation_backend").is_some());
            }
            Err(ApiError::Other(_)) => {
                // git not available in this environment; acceptable
            }
            Err(e) => panic!("unexpected error: {e:?}"),
        }
    }
}
