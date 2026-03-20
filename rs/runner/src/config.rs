use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::approval::DEFAULT_TOKEN_TTL_SECS;
use std::sync::Arc;
use std::time::Instant;

use globset::{Glob, GlobSet, GlobSetBuilder};
use tokio::sync::Mutex;

use crate::indexing::IndexingService;
use crate::invariants::{build_checker, InvariantChecker};
use crate::sandbox::SandboxPolicy;

/// Configuration for the Kubernetes sandbox environment.
///
/// Populated from environment variables with sensible defaults that match
/// the `runner-deployment.yaml` manifest produced by Wave 9.
#[derive(Debug, Clone)]
pub struct SandboxConfig {
    /// Kubernetes `runtimeClassName` — informational, logged at startup.
    pub runtime_class: String,
    /// Writable workspace directory.  All tool write operations must target
    /// paths under this prefix when `enforce_read_only_root` is `true`.
    pub workspace_path: PathBuf,
    /// When `true`, the runner rejects write operations that target paths
    /// outside `workspace_path`.
    pub enforce_read_only_root: bool,
    /// Path to the guest kernel image passed to Firecracker's `/boot-source` API.
    /// Read from `LG_SANDBOX_KERNEL_IMAGE_PATH`. `None` means unset.
    pub kernel_image_path: Option<String>,
    /// Path to the root filesystem image passed to Firecracker's `/drives/rootfs` API.
    /// Read from `LG_SANDBOX_ROOTFS_PATH`. `None` means unset.
    pub rootfs_path: Option<String>,
}

impl Default for SandboxConfig {
    fn default() -> Self {
        Self {
            runtime_class: "gvisor".to_string(),
            workspace_path: PathBuf::from("/workspace"),
            enforce_read_only_root: true,
            kernel_image_path: None,
            rootfs_path: None,
        }
    }
}

impl SandboxConfig {
    /// Construct from environment variables, falling back to defaults.
    ///
    /// | Env var                          | Default        |
    /// |----------------------------------|----------------|
    /// | `LG_SANDBOX_RUNTIME_CLASS`       | `"gvisor"`     |
    /// | `LG_SANDBOX_WORKSPACE_PATH`      | `"/workspace"` |
    /// | `LG_SANDBOX_ENFORCE_READONLY`    | `"true"`       |
    /// | `LG_SANDBOX_KERNEL_IMAGE_PATH`   | `None`         |
    /// | `LG_SANDBOX_ROOTFS_PATH`         | `None`         |
    #[must_use]
    pub fn from_env() -> Self {
        let runtime_class = std::env::var("LG_SANDBOX_RUNTIME_CLASS")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| "gvisor".to_string());

        let workspace_path = std::env::var("LG_SANDBOX_WORKSPACE_PATH")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("/workspace"));

        let enforce_read_only_root = std::env::var("LG_SANDBOX_ENFORCE_READONLY")
            .ok()
            .map(|v| {
                matches!(
                    v.trim().to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            })
            .unwrap_or(true);

        let kernel_image_path = std::env::var("LG_SANDBOX_KERNEL_IMAGE_PATH")
            .ok()
            .filter(|v| !v.trim().is_empty());

        let rootfs_path = std::env::var("LG_SANDBOX_ROOTFS_PATH")
            .ok()
            .filter(|v| !v.trim().is_empty());

        Self {
            runtime_class,
            workspace_path,
            enforce_read_only_root,
            kernel_image_path,
            rootfs_path,
        }
    }
}

pub struct RateLimiter {
    tokens: f64,
    max_tokens: f64,
    refill_rate: f64,
    last_refill: Instant,
}

impl RateLimiter {
    pub fn new(rps: u64) -> Self {
        Self {
            tokens: rps as f64,
            max_tokens: rps as f64,
            refill_rate: rps as f64,
            last_refill: Instant::now(),
        }
    }

    pub fn try_acquire(&mut self) -> bool {
        let now = Instant::now();
        let elapsed = now.duration_since(self.last_refill).as_secs_f64();
        self.tokens = (self.tokens + elapsed * self.refill_rate).min(self.max_tokens);
        self.last_refill = now;
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

/// Commands permitted by the exec tool's allowlist.
/// Kept in sync with `tools/exec.rs::allowed_cmd`.
pub const ALLOWED_EXEC_COMMANDS: &[&str] = &[
    "uv", "python", "pytest", "ruff", "mypy", "cargo", "git",
];

/// Opaque per-process pool of live MCP subprocess clients.
///
/// Values are type-erased (`Box<dyn Any + Send>`) so that `config.rs`
/// does not need to import the `McpStdioClient` type from `tools/mcp.rs`,
/// which would create a circular module dependency.  `tools/mcp.rs`
/// downcasts the boxes to its own `PoolEntry` type.
pub type McpPool = Arc<Mutex<HashMap<String, Box<dyn std::any::Any + Send + 'static>>>>;

/// Construct an empty [`McpPool`].
pub fn new_mcp_pool() -> McpPool {
    Arc::new(Mutex::new(HashMap::new()))
}

#[derive(Clone)]
pub struct RunnerConfig {
    pub root_dir: PathBuf,
    pub allow_read: GlobSet,
    pub allow_write: GlobSet,
    pub api_key: Option<String>,
    pub rate_limiter: Arc<Mutex<RateLimiter>>,
    pub indexing: Arc<IndexingService>,
    pub sandbox_policy: SandboxPolicy,
    pub sandbox: SandboxConfig,
    pub invariant_checker: Arc<InvariantChecker>,
    /// Maximum age (in seconds) for which an approval token is considered valid.
    ///
    /// Defaults to [`crate::approval::DEFAULT_TOKEN_TTL_SECS`] (300 s).
    /// Override via `LG_RUNNER_APPROVAL_TOKEN_TTL_SECS` or pass explicitly
    /// through [`RunnerConfig::with_rate_limit`].
    pub approval_token_ttl_secs: u64,
    /// Per-process pool of live MCP subprocess clients.
    ///
    /// Shared across all Axum handler invocations via [`Arc`] cloning.
    /// Populated lazily by `tools/mcp.rs::get_or_connect`.
    pub mcp_pool: McpPool,
}

impl std::fmt::Debug for RunnerConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RunnerConfig")
            .field("root_dir", &self.root_dir)
            .field("api_key", &self.api_key.as_ref().map(|_| "[REDACTED]"))
            .field("rate_limiter", &"<RateLimiter>")
            .field("indexing", &"<IndexingService>")
            .field("sandbox", &self.sandbox)
            .finish()
    }
}

impl RunnerConfig {
    #[allow(dead_code)]
    pub fn new(
        root_dir: impl AsRef<Path>,
        profile: Option<&str>,
        api_key: Option<String>,
    ) -> anyhow::Result<Self> {
        Self::with_rate_limit(root_dir, profile, api_key, 100)
    }

    pub fn with_rate_limit(
        root_dir: impl AsRef<Path>,
        profile: Option<&str>,
        api_key: Option<String>,
        rate_limit_rps: u64,
    ) -> anyhow::Result<Self> {
        let root_dir = root_dir.as_ref().canonicalize()?;

        let profile = profile.unwrap_or("dev");
        let (read_globs, write_globs) = allowlists_for_profile(profile);
        let allow_read = build_globset(&read_globs)?;
        let allow_write = build_globset(&write_globs)?;
        let indexing = Arc::new(IndexingService::new(root_dir.clone(), allow_read.clone())?);

        let sandbox_policy = SandboxPolicy::from_env();
        let sandbox = SandboxConfig::from_env();
        let allowed_commands: Vec<String> = ALLOWED_EXEC_COMMANDS
            .iter()
            .map(|s| (*s).to_string())
            .collect();
        let invariant_checker = build_checker(&root_dir, &allowed_commands);

        tracing::info!(
            runtime_class = %sandbox.runtime_class,
            workspace_path = %sandbox.workspace_path.display(),
            enforce_read_only_root = sandbox.enforce_read_only_root,
            "sandbox configuration loaded"
        );

        let approval_token_ttl_secs = std::env::var("LG_RUNNER_APPROVAL_TOKEN_TTL_SECS")
            .ok()
            .and_then(|v| v.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_TOKEN_TTL_SECS);

        Ok(Self {
            root_dir,
            allow_read,
            allow_write,
            api_key,
            rate_limiter: Arc::new(Mutex::new(RateLimiter::new(rate_limit_rps))),
            indexing,
            sandbox_policy,
            sandbox,
            invariant_checker,
            approval_token_ttl_secs,
            mcp_pool: new_mcp_pool(),
        })
    }

    pub fn can_read(&self, rel: &str) -> bool {
        let rel = rel.replace('\\', "/");
        self.allow_read.is_match(&rel)
    }

    pub fn can_write(&self, rel: &str) -> bool {
        let rel = rel.replace('\\', "/");
        self.allow_write.is_match(&rel)
    }
}

fn build_globset(globs: &[&str]) -> anyhow::Result<GlobSet> {
    let mut b = GlobSetBuilder::new();
    for g in globs {
        b.add(Glob::new(g)?);
    }
    Ok(b.build()?)
}

fn allowlists_for_profile(profile: &str) -> (Vec<&'static str>, Vec<&'static str>) {
    match profile {
        "dev" => (
            vec![
                ".",
                "README.md",
                "LICENSE",
                "py",
                "py/**",
                "rs",
                "rs/**",
                "docs",
                "docs/**",
                "prompts",
                "prompts/**",
                "schemas",
                "schemas/**",
                "configs",
                "configs/**",
                "eval",
                "eval/**",
                "scripts",
                "scripts/**",
                ".github/**",
                ".editorconfig",
                ".gitignore",
            ],
            vec![
                "py",
                "py/**",
                "rs",
                "rs/**",
                "docs",
                "docs/**",
                "prompts",
                "prompts/**",
                "schemas",
                "schemas/**",
                "configs",
                "configs/**",
                "eval",
                "eval/**",
                "scripts",
                "scripts/**",
                ".github/**",
            ],
        ),
        "stage" => (
            vec![
                ".",
                "README.md",
                "LICENSE",
                "py",
                "py/**",
                "rs",
                "rs/**",
                "docs",
                "docs/**",
                "prompts",
                "prompts/**",
                "schemas",
                "schemas/**",
                "configs",
                "configs/**",
                "eval",
                "eval/**",
                "scripts",
                "scripts/**",
                ".github/**",
                ".editorconfig",
                ".gitignore",
            ],
            vec![
                "py",
                "py/**",
                "rs",
                "rs/**",
                "docs",
                "docs/**",
                "prompts",
                "prompts/**",
                "schemas",
                "schemas/**",
                "configs",
                "configs/**",
                "eval",
                "eval/**",
                "scripts",
                "scripts/**",
                ".github/**",
            ],
        ),
        "prod" => (
            vec![
                ".",
                "README.md",
                "LICENSE",
                "py",
                "py/**",
                "rs",
                "rs/**",
                "docs",
                "docs/**",
                "prompts",
                "prompts/**",
                "schemas",
                "schemas/**",
                "configs",
                "configs/**",
                "eval",
                "eval/**",
                "scripts",
                "scripts/**",
                ".github/**",
                ".editorconfig",
                ".gitignore",
            ],
            vec![],
        ),
        _ => (
            vec![
                ".",
                "README.md",
                "LICENSE",
                "py",
                "py/**",
                "rs",
                "rs/**",
                "docs",
                "docs/**",
                "prompts",
                "prompts/**",
                "schemas",
                "schemas/**",
                "configs",
                "configs/**",
                "eval",
                "eval/**",
                "scripts",
                "scripts/**",
                ".github/**",
                ".editorconfig",
                ".gitignore",
            ],
            vec![
                "py",
                "py/**",
                "rs",
                "rs/**",
                "docs",
                "docs/**",
                "prompts",
                "prompts/**",
                "schemas",
                "schemas/**",
                "configs",
                "configs/**",
                "eval",
                "eval/**",
                "scripts",
                "scripts/**",
                ".github/**",
            ],
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_config_new_with_current_dir() {
        let cfg = RunnerConfig::new(".", Some("dev"), None).unwrap();
        assert!(cfg.root_dir.is_absolute());
    }

    #[test]
    fn test_config_new_with_tempdir() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        assert!(cfg.root_dir.is_absolute());
        assert!(cfg.root_dir.exists());
    }

    #[test]
    fn test_config_new_nonexistent_fails() {
        let result = RunnerConfig::new("/nonexistent/path/that/does/not/exist", Some("dev"), None);
        assert!(result.is_err());
    }

    #[test]
    fn test_can_read_has_allowlist() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        assert!(cfg.can_read("py/src/lg_orch/main.py"));
        assert!(!cfg.can_read(".git/config"));
    }

    #[test]
    fn test_can_write_has_allowlist() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        assert!(cfg.can_write("py/src/lg_orch/main.py"));
        assert!(!cfg.can_write("README.md"));
    }

    #[test]
    fn test_root_dir_is_canonical() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        assert_eq!(cfg.root_dir, cfg.root_dir.canonicalize().unwrap());
    }

    #[test]
    fn test_prod_disables_writes() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("prod"), None).unwrap();
        assert!(!cfg.can_write("py/src/lg_orch/main.py"));
    }

    #[tokio::test]
    async fn test_rate_limiter_allows_within_limit() {
        let mut rl = RateLimiter::new(10);
        for _ in 0..10 {
            assert!(rl.try_acquire());
        }
    }

    #[tokio::test]
    async fn test_rate_limiter_denies_over_limit() {
        let mut rl = RateLimiter::new(2);
        assert!(rl.try_acquire());
        assert!(rl.try_acquire());
        assert!(!rl.try_acquire());
    }

    #[test]
    fn test_config_with_custom_rate_limit() {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::with_rate_limit(td.path(), Some("dev"), None, 50).unwrap();
        assert!(cfg.root_dir.is_absolute());
    }

    #[test]
    fn test_config_debug_redacts_api_key() {
        let td = tempfile::tempdir().unwrap();
        let cfg =
            RunnerConfig::new(td.path(), Some("dev"), Some("secret-key".to_string())).unwrap();
        let debug_str = format!("{:?}", cfg);
        assert!(!debug_str.contains("secret-key"));
        assert!(debug_str.contains("[REDACTED]"));
    }
}
