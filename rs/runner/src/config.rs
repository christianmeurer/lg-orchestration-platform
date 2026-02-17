use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use globset::{Glob, GlobSet, GlobSetBuilder};
use tokio::sync::Mutex;

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

#[derive(Clone)]
pub struct RunnerConfig {
    pub root_dir: PathBuf,
    pub allow_read: GlobSet,
    pub allow_write: GlobSet,
    pub api_key: Option<String>,
    pub rate_limiter: Arc<Mutex<RateLimiter>>,
}

impl std::fmt::Debug for RunnerConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RunnerConfig")
            .field("root_dir", &self.root_dir)
            .field("api_key", &self.api_key.as_ref().map(|_| "[REDACTED]"))
            .field("rate_limiter", &"<RateLimiter>")
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

        Ok(Self {
            root_dir,
            allow_read,
            allow_write,
            api_key,
            rate_limiter: Arc::new(Mutex::new(RateLimiter::new(rate_limit_rps))),
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
