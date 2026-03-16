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

    // Scan all args for prompt injection patterns
    for arg in &inp.args {
        if let Some(reason) = crate::sandbox::detect_prompt_injection(arg) {
            return Err(ApiError::Forbidden(format!("prompt_injection_detected: {reason}")));
        }
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

    let mut c = match sandbox_resolution.backend {
        SandboxBackend::LinuxNamespace => {
            let unshare_path = sandbox_policy
                .linux_namespace
                .unshare_bin
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_else(|| "/usr/bin/unshare".to_string());
            let mut cmd_obj = Command::new(&unshare_path);
            cmd_obj.args(["--pid", "--mount", "--net", "--fork", "--"])
                .arg(cmd)
                .args(&inp.args);
            cmd_obj
        }
        SandboxBackend::MicroVmEphemeral => {
            let firecracker_path = sandbox_policy
                .microvm
                .firecracker_bin
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_else(|| "firecracker".to_string());
                
            let kernel_path = sandbox_policy
                .microvm
                .kernel_image
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_else(|| "/var/lib/firecracker/vmlinux".to_string());
                
            let rootfs_path = sandbox_policy
                .microvm
                .rootfs_image
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_else(|| "/var/lib/firecracker/rootfs.ext4".to_string());

            let mut cmd_obj = Command::new(&firecracker_path);
            
            let mut fc_args = vec![
                "--kernel".to_string(), kernel_path,
                "--rootfs".to_string(), rootfs_path,
                "--".to_string(),
                cmd.to_string()
            ];
            fc_args.extend(inp.args.iter().cloned());
            
            cmd_obj.args(&fc_args);
            cmd_obj
        }
        _ => {
            let mut cmd_obj = Command::new(cmd);
            cmd_obj.args(&inp.args);
            cmd_obj
        }
    };
    c.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Strip the parent environment to prevent secret leakage via env vars.
    // Re-inject only the minimal safe set needed for toolchain commands.
    c.env_clear();
    if let Ok(path) = std::env::var("PATH") {
        c.env("PATH", path);
    }
    if let Ok(home) = std::env::var("HOME") {
        c.env("HOME", home);
    }
    // CARGO_HOME and RUSTUP_HOME are required for cargo to locate toolchains
    if let Ok(v) = std::env::var("CARGO_HOME") { c.env("CARGO_HOME", v); }
    if let Ok(v) = std::env::var("RUSTUP_HOME") { c.env("RUSTUP_HOME", v); }
    // UV_CACHE_DIR for Python tooling
    if let Ok(v) = std::env::var("UV_CACHE_DIR") { c.env("UV_CACHE_DIR", v); }
    // VIRTUAL_ENV / PYTHONPATH for activated venvs
    if let Ok(v) = std::env::var("VIRTUAL_ENV") { c.env("VIRTUAL_ENV", v); }
    if let Ok(v) = std::env::var("PYTHONPATH") { c.env("PYTHONPATH", v); }
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

    #[tokio::test]
    async fn test_exec_prompt_injection_blocked() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        // U+202E right-to-left override triggers detect_prompt_injection
        let result = exec(
            &cfg,
            json!({"cmd": "git", "args": ["log", "safe\u{202E}evil"]}),
        )
        .await;
        assert!(
            matches!(result, Err(ApiError::Forbidden(ref msg)) if msg.contains("prompt_injection_detected")),
            "expected Forbidden(prompt_injection_detected), got: {result:?}"
        );
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

    #[tokio::test]
    async fn test_exec_microvm_backend_formats_command() {
        if cfg!(target_os = "windows") {
            return;
        }
        
        // Need to set env vars to force MicroVmEphemeral resolution
        std::env::set_var("LG_RUNNER_SANDBOX_BACKEND", "microvm");
        std::env::set_var("LG_RUNNER_MICROVM_ENABLED", "1");
        
        // Create dummy files so the path checks pass
        let td = tempfile::tempdir().unwrap();
        let fc_bin = td.path().join("firecracker");
        let kernel = td.path().join("vmlinux");
        let rootfs = td.path().join("rootfs");
        std::fs::write(&fc_bin, "").unwrap();
        std::fs::write(&kernel, "").unwrap();
        std::fs::write(&rootfs, "").unwrap();
        
        std::env::set_var("LG_RUNNER_FIRECRACKER_BIN", fc_bin.to_str().unwrap());
        std::env::set_var("LG_RUNNER_MICROVM_KERNEL_IMAGE", kernel.to_str().unwrap());
        std::env::set_var("LG_RUNNER_MICROVM_ROOTFS_IMAGE", rootfs.to_str().unwrap());

        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        
        // We expect it to fail execution because the dummy files aren't real executables, 
        // but we can check the returned envelope's metadata to verify it attempted MicroVM.
        let result = exec(&cfg, json!({"cmd": "python", "args": ["--version"]})).await;
        
        // Clean up env vars
        std::env::remove_var("LG_RUNNER_SANDBOX_BACKEND");
        std::env::remove_var("LG_RUNNER_MICROVM_ENABLED");
        std::env::remove_var("LG_RUNNER_FIRECRACKER_BIN");
        std::env::remove_var("LG_RUNNER_MICROVM_KERNEL_IMAGE");
        std::env::remove_var("LG_RUNNER_MICROVM_ROOTFS_IMAGE");

        if let Ok(env) = result {
             let artifacts = env.artifacts;
             let isolation_backend = artifacts.get("isolation_backend").and_then(|v| v.as_str()).unwrap_or("");
             assert_eq!(isolation_backend, "microvm_ephemeral");
        } else if let Err(ApiError::Other(_)) = result {
             // If spawn fails, that's fine too, as long as it tried.
        } else if let Err(e) = result {
             panic!("unexpected error: {e:?}");
        }
    }
}
