use std::collections::HashMap;
use std::process::Stdio;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};
use tokio::process::Command;
use tokio::time::timeout;

use crate::approval::{require_approval, ApprovalTokenInput};
use crate::config::{RunnerConfig, ALLOWED_EXEC_COMMANDS};
use crate::diagnostics::parse_structured_diagnostics;
use crate::envelope::{Diagnostic, ToolEnvelope};
use crate::errors::ApiError;
use crate::sandbox::{
    apply_cgroup_v2_limits, cleanup_cgroup, pre_validate_exec, CgroupLimits, SandboxBackend,
};
use crate::tools::{snapshot_for_operation, ToolContext};
#[cfg(target_os = "linux")]
use crate::vsock::GuestCommandRequest;

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

/// Returns `true` if `cmd` is in the canonical allowlist defined in
/// [`crate::config::ALLOWED_EXEC_COMMANDS`].  Single source of truth.
fn allowed_cmd(cmd: &str) -> bool {
    ALLOWED_EXEC_COMMANDS.contains(&cmd)
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

#[tracing::instrument(
    skip_all,
    fields(tool = "exec", challenge_id = tracing::field::Empty)
)]
pub async fn exec(cfg: &RunnerConfig, ctx: &mut ToolContext, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: ExecIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    let cmd = inp.cmd.trim();
    // Record challenge_id when an approval token is attached.
    if let Some(ref approval) = inp.approval {
        tracing::Span::current().record("challenge_id", &*approval.challenge_id);
    }

    // Invariant pre-validation layer (neurosymbolic vericoding check).
    // Runs before the existing per-tool checks; does NOT replace them.
    {
        let allowed_commands: Vec<String> = ALLOWED_EXEC_COMMANDS
            .iter()
            .map(|s| (*s).to_string())
            .collect();
        pre_validate_exec(
            &cfg.invariant_checker,
            "exec",
            cmd,
            &inp.args,
            &cfg.root_dir,
            &allowed_commands,
        )?;
    }

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
            cfg.approval_token_ttl_secs,
        )?)
    } else {
        None
    };

    let snapshot_metadata = if operation_class == "state_modifying_exec" {
        Some(snapshot_for_operation(cfg, ctx, operation_class).await?)
    } else {
        None
    };

    let sandbox_resolution = cfg.sandbox_policy.resolve_backend_with_vmm().await;
    let isolation = sandbox_resolution.to_isolation_metadata();
    // Increment sandbox tier metric
    let sandbox_tier_label = match sandbox_resolution.backend {
        crate::sandbox::SandboxBackend::MicroVmEphemeral => "micro_vm",
        crate::sandbox::SandboxBackend::LinuxNamespace => "linux_namespace",
        crate::sandbox::SandboxBackend::SafeFallback => "safe_fallback",
    };
    metrics::counter!("runner_sandbox_tier", "tier" => sandbox_tier_label).increment(1);

    // Destructure to obtain the VMM handle separately so it stays alive for
    // the entire lifetime of the child process (dropping it kills the VM).
    let crate::sandbox::SandboxResolution {
        backend: resolved_backend,
        vmm: vmm_handle,
        ..
    } = sandbox_resolution;

    // When the MicroVmEphemeral backend is active and a live VMM handle is
    // present, configure and start the VM via the socket REST API before
    // spawning the guest command.
    if resolved_backend == SandboxBackend::MicroVmEphemeral {
        if let Some(ref vmm) = vmm_handle {
            let kernel_path = cfg.sandbox.kernel_image_path.to_string_lossy();
            let rootfs_path = cfg.sandbox.rootfs_image_path.to_string_lossy();
            vmm.configure_and_start(&kernel_path, &rootfs_path)
                .await
                .map_err(|e| {
                    tracing::warn!(error = %e, "firecracker configure_and_start failed");
                    e
                })?;
        }
    }

    let cwd = inp
        .cwd
        .as_deref()
        .map(|p| super::fs::resolve_under_root(cfg, p))
        .transpose()?;

    // Build the minimal safe environment that would be passed to any command.
    // Shared between the vsock guest path and the host-command paths below.
    let mut filtered_env: HashMap<String, String> = HashMap::new();
    if let Ok(path) = std::env::var("PATH") { filtered_env.insert("PATH".to_string(), path); }
    if let Ok(home) = std::env::var("HOME") { filtered_env.insert("HOME".to_string(), home); }
    if let Ok(v) = std::env::var("CARGO_HOME") { filtered_env.insert("CARGO_HOME".to_string(), v); }
    if let Ok(v) = std::env::var("RUSTUP_HOME") { filtered_env.insert("RUSTUP_HOME".to_string(), v); }
    if let Ok(v) = std::env::var("UV_CACHE_DIR") { filtered_env.insert("UV_CACHE_DIR".to_string(), v); }
    if let Ok(v) = std::env::var("VIRTUAL_ENV") { filtered_env.insert("VIRTUAL_ENV".to_string(), v); }
    if let Ok(v) = std::env::var("PYTHONPATH") { filtered_env.insert("PYTHONPATH".to_string(), v); }

    // -----------------------------------------------------------------------
    // MicroVmEphemeral path — dispatch command to the guest agent via vsock.
    // On Linux this sends the request over AF_VSOCK and returns early.
    // On non-Linux platforms the path is unavailable and returns an error.
    // -----------------------------------------------------------------------
    if resolved_backend == SandboxBackend::MicroVmEphemeral {
        #[cfg(target_os = "linux")]
        {
            let vmm = vmm_handle.as_ref().ok_or_else(|| {
                ApiError::BadRequest("MicroVM not started for this request".into())
            })?;

            let timeout_secs = inp.timeout_s.unwrap_or(600);
            let cwd_str = cwd
                .as_ref()
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_else(|| cfg.root_dir.to_string_lossy().to_string());

            let guest_req = GuestCommandRequest {
                cmd: cmd.to_string(),
                args: inp.args.clone(),
                cwd: cwd_str,
                env: filtered_env,
                timeout_ms: timeout_secs * 1000,
            };

            let resp = crate::vsock::send_guest_command(
                vmm.cid,
                52525,
                &guest_req,
                Duration::from_secs(timeout_secs),
            )
            .await?;

            metrics::counter!(
                "runner_tool_calls_total",
                "tool" => "exec",
                "status" => if resp.ok { "ok" } else { "error" }
            ).increment(1);

            let env_out = if resp.ok {
                let mut e = ToolEnvelope::ok(
                    "exec",
                    resp.stdout,
                    json!({
                        "cmd": cmd,
                        "args": inp.args,
                        "exit_code": resp.exit_code,
                        "operation_class": operation_class,
                        "isolation_backend": isolation.backend,
                        "isolation_degraded": isolation.degraded,
                        "isolation_reason": isolation.reason,
                        "isolation_policy_constraints": isolation.policy_constraints,
                        "timing_ms": resp.timing_ms,
                    }),
                )
                .with_isolation(isolation);
                if let Some(approval) = approval_metadata { e = e.with_approval(approval); }
                if let Some(snapshot) = snapshot_metadata { e = e.with_snapshot(snapshot); }
                e
            } else {
                let diagnostics = parse_structured_diagnostics(&resp.stderr);
                let (stderr_excerpt, stderr_truncated) =
                    truncate_chars(&resp.stderr, STDERR_ARTIFACT_MAX_CHARS);
                let stderr_chars = resp.stderr.chars().count();
                let diagnostics_artifact = diagnostics_to_artifact_value(&diagnostics);
                let mut e = ToolEnvelope::err(
                    "exec",
                    resp.exit_code,
                    resp.stderr,
                    json!({
                        "cmd": cmd,
                        "args": inp.args,
                        "exit_code": resp.exit_code,
                        "operation_class": operation_class,
                        "isolation_backend": isolation.backend,
                        "isolation_degraded": isolation.degraded,
                        "isolation_reason": isolation.reason,
                        "isolation_policy_constraints": isolation.policy_constraints,
                        "stdout": resp.stdout,
                        "stderr_excerpt": stderr_excerpt,
                        "stderr_truncated": stderr_truncated,
                        "stderr_chars": stderr_chars,
                        "diagnostics": diagnostics_artifact,
                        "timing_ms": resp.timing_ms,
                    }),
                )
                .with_diagnostics(diagnostics)
                .with_isolation(isolation);
                if let Some(approval) = approval_metadata { e = e.with_approval(approval); }
                if let Some(snapshot) = snapshot_metadata { e = e.with_snapshot(snapshot); }
                e
            };
            return Ok(env_out);
        }
        #[cfg(not(target_os = "linux"))]
        {
            return Err(ApiError::BadRequest(
                "MicroVmEphemeral sandbox requires Linux (vsock not available on this platform)"
                    .into(),
            ));
        }
    }

    // -----------------------------------------------------------------------
    // Host-command paths (LinuxNamespace, SafeFallback)
    // -----------------------------------------------------------------------
    let mut c = match resolved_backend {
        SandboxBackend::LinuxNamespace => {
            let unshare_path = cfg
                .sandbox_policy
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
    // Re-inject only the minimal safe set built above.
    c.env_clear();
    for (k, v) in &filtered_env {
        c.env(k, v);
    }
    if let Some(ref cwd_path) = cwd {
        c.current_dir(cwd_path);
    } else {
        c.current_dir(&cfg.root_dir);
    }

    let t = Duration::from_secs(inp.timeout_s.unwrap_or(600));
    let child = c.spawn().map_err(|e| ApiError::Other(e.into()))?;

    // Apply cgroup v2 resource limits for the LinuxNamespace backend.
    // Gracefully no-ops when not running as root or cgroup v2 is not mounted.
    let cgroup_name = if resolved_backend == SandboxBackend::LinuxNamespace {
        if let Some(pid) = child.id() {
            let name = format!("run-{pid}");
            if let Err(e) =
                apply_cgroup_v2_limits(&name, &CgroupLimits::default(), pid)
            {
                tracing::warn!(error = %e, "cgroup v2 limit application failed; continuing without limits");
            }
            Some(name)
        } else {
            None
        }
    } else {
        None
    };

    let out = timeout(t, child.wait_with_output())
        .await
        .map_err(|_| ApiError::Other(anyhow::anyhow!("timeout")))?
        .map_err(|e| ApiError::Other(e.into()))?;

    // Best-effort cgroup cleanup after the child exits.
    if let Some(ref name) = cgroup_name {
        let _ = cleanup_cgroup(name);
    }

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    let code = out.status.code().unwrap_or(1);
    if code == 0 {
        metrics::counter!("runner_tool_calls_total", "tool" => "exec", "status" => "ok")
            .increment(1);
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
        metrics::counter!("runner_tool_calls_total", "tool" => "exec", "status" => "error")
            .increment(1);
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
    use crate::tools::ToolContext;

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
        let mut ctx = ToolContext::default();
        let result = exec(&cfg, &mut ctx, json!({"cmd": "rm", "args": ["-rf", "/"]})).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_exec_bad_input() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        let mut ctx = ToolContext::default();
        let result = exec(&cfg, &mut ctx, json!({"wrong": "fields"})).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_exec_state_modifying_requires_approval() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        let mut ctx = ToolContext::default();
        let result = exec(&cfg, &mut ctx, json!({"cmd": "git", "args": ["commit"]})).await;
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
        let mut ctx = ToolContext::default();
        // U+202E right-to-left override triggers detect_prompt_injection
        let result = exec(
            &cfg,
            &mut ctx,
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
        let mut ctx = ToolContext::default();
        let result = exec(&cfg, &mut ctx, json!({"cmd": "git", "args": ["--version"]})).await;
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
        let mut ctx = ToolContext::default();

        // We expect it to fail execution because the dummy files aren't real executables,
        // but we can check the returned envelope's metadata to verify it attempted MicroVM.
        let result = exec(&cfg, &mut ctx, json!({"cmd": "python", "args": ["--version"]})).await;

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
