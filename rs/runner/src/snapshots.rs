// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, LazyLock, Mutex};

use anyhow::anyhow;
use serde::{Deserialize, Serialize};
use tokio::fs;
use tokio::process::Command;
use tokio::sync::Mutex as TokioMutex;

use crate::envelope::CheckpointPointer;
use crate::errors::ApiError;

const SNAPSHOT_REF_PREFIX: &str = "refs/lg_orch/snapshots/";

/// Validate that a snapshot ID is safe to embed in a git ref name.
///
/// Accepts only alphanumeric characters, hyphens, and underscores,
/// with a maximum length of 64 characters.  This prevents git flag
/// injection (e.g. `--force`) and ref-name collisions (e.g. `refs/heads/main`).
fn validate_snapshot_id(id: &str) -> Result<(), ApiError> {
    if id.is_empty() || id.len() > 64 {
        return Err(ApiError::BadRequest(format!(
            "snapshot_id must be 1–64 characters, got {}",
            id.len()
        )));
    }
    if !id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err(ApiError::BadRequest(
            "snapshot_id must contain only [a-zA-Z0-9_-]".to_string(),
        ));
    }
    // Reject IDs starting with '-' to prevent git flag injection (e.g. `--force`).
    if id.starts_with('-') {
        return Err(ApiError::BadRequest(
            "snapshot_id must not start with '-'".to_string(),
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Per-repository serialisation lock
// ---------------------------------------------------------------------------

/// Global map from canonical repository root path to an async mutex.
///
/// This ensures that concurrent [`undo_to_snapshot`] calls targeting the same
/// repository do not race on `git reset --hard` / `git clean -fd`.
static REPO_LOCKS: LazyLock<Mutex<HashMap<PathBuf, Arc<TokioMutex<()>>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn repo_lock(repo: &Path) -> Arc<TokioMutex<()>> {
    let mut map = REPO_LOCKS.lock().unwrap();
    map.entry(repo.to_path_buf()).or_insert_with(|| Arc::new(TokioMutex::new(()))).clone()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotRecord {
    pub snapshot_id: String,
    pub operation_class: String,
    pub git_commit: String,
    pub non_git_workspace: bool,
    pub checkpoint: Option<CheckpointPointer>,
    pub created_at_unix: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UndoOutcome {
    pub snapshot_id: String,
    pub filesystem_restored: bool,
    pub checkpoint_restored: bool,
    pub checkpoint: Option<CheckpointPointer>,
}

#[derive(Debug, thiserror::Error)]
pub enum SnapshotError {
    #[error("non_git_workspace")]
    NonGitWorkspace,
    #[error("snapshot_not_found: {0}")]
    SnapshotNotFound(String),
    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

async fn run_git(root_dir: &Path, args: &[&str]) -> anyhow::Result<(bool, String, String)> {
    let out = Command::new("git")
        .args(args)
        .current_dir(root_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await?;
    Ok((
        out.status.success(),
        String::from_utf8_lossy(&out.stdout).trim().to_string(),
        String::from_utf8_lossy(&out.stderr).trim().to_string(),
    ))
}

async fn git_dir(root_dir: &Path) -> anyhow::Result<String> {
    let (ok, stdout, stderr) = run_git(root_dir, &["rev-parse", "--git-dir"]).await?;
    if !ok || stdout.is_empty() {
        return Err(anyhow!("failed to resolve git-dir: {stderr}"));
    }
    Ok(stdout)
}

async fn metadata_dir(root_dir: &Path) -> anyhow::Result<PathBuf> {
    let git_dir_raw = git_dir(root_dir).await?;
    let git_path = Path::new(&git_dir_raw);
    let absolute_git =
        if git_path.is_absolute() { git_path.to_path_buf() } else { root_dir.join(git_path) };
    Ok(absolute_git.join("lg_orch").join("snapshots"))
}

pub async fn is_git_repo(root_dir: &Path) -> anyhow::Result<bool> {
    let (ok, stdout, _) = run_git(root_dir, &["rev-parse", "--is-inside-work-tree"]).await?;
    Ok(ok && stdout == "true")
}

pub async fn create_snapshot(
    root_dir: &Path,
    operation_class: &str,
    checkpoint: Option<CheckpointPointer>,
) -> Result<SnapshotRecord, SnapshotError> {
    if !is_git_repo(root_dir).await? {
        return Err(SnapshotError::NonGitWorkspace);
    }

    let (ok, head, stderr) = run_git(root_dir, &["rev-parse", "HEAD"]).await?;
    if !ok || head.is_empty() {
        return Err(SnapshotError::Other(anyhow!("failed to resolve HEAD: {stderr}")));
    }

    let now = chrono::Utc::now().timestamp();
    let snapshot_id = format!("snap-{now}-{}", uuid::Uuid::new_v4().simple());
    let ref_name = format!("{SNAPSHOT_REF_PREFIX}{snapshot_id}");
    let snapshot_record = SnapshotRecord {
        snapshot_id: snapshot_id.clone(),
        operation_class: operation_class.to_string(),
        git_commit: head.clone(),
        non_git_workspace: false,
        checkpoint: checkpoint.clone(),
        created_at_unix: now,
    };

    let (ok_ref, _, stderr_ref) = run_git(root_dir, &["update-ref", &ref_name, &head]).await?;
    if !ok_ref {
        return Err(SnapshotError::Other(anyhow!("failed to create snapshot ref: {stderr_ref}")));
    }

    let meta_dir = metadata_dir(root_dir).await?;
    fs::create_dir_all(&meta_dir)
        .await
        .map_err(|err| anyhow!("failed to create snapshot metadata dir: {err}"))?;
    let meta_path = meta_dir.join(format!("{snapshot_id}.json"));
    let payload = serde_json::to_vec_pretty(&snapshot_record)
        .map_err(|err| anyhow!("failed to serialize snapshot metadata: {err}"))?;
    fs::write(&meta_path, payload)
        .await
        .map_err(|err| anyhow!("failed to write snapshot metadata: {err}"))?;

    Ok(snapshot_record)
}

pub async fn undo_to_snapshot(
    root_dir: &Path,
    snapshot_id: Option<&str>,
) -> Result<UndoOutcome, SnapshotError> {
    if !is_git_repo(root_dir).await? {
        return Err(SnapshotError::NonGitWorkspace);
    }

    let resolved_snapshot = if let Some(id) = snapshot_id {
        let trimmed = id.trim().to_string();
        validate_snapshot_id(&trimmed)
            .map_err(|e| SnapshotError::Other(anyhow::anyhow!("{e}")))?;
        trimmed
    } else {
        let (ok, stdout, stderr) = run_git(
            root_dir,
            &[
                "for-each-ref",
                "--sort=-creatordate",
                "--count=1",
                "--format=%(refname:short)",
                SNAPSHOT_REF_PREFIX,
            ],
        )
        .await?;
        if !ok || stdout.is_empty() {
            return Err(SnapshotError::Other(anyhow!(
                "failed to resolve latest snapshot: {stderr}"
            )));
        }
        stdout
            .split('/')
            .next_back()
            .map(ToString::to_string)
            .ok_or_else(|| SnapshotError::Other(anyhow!("invalid snapshot ref format")))?
    };

    let ref_name = format!("{SNAPSHOT_REF_PREFIX}{resolved_snapshot}");
    let (ok_commit, commit, stderr_commit) = run_git(root_dir, &["rev-parse", &ref_name]).await?;
    if !ok_commit || commit.is_empty() {
        return Err(SnapshotError::SnapshotNotFound(format!(
            "{resolved_snapshot}: {stderr_commit}"
        )));
    }

    // Acquire per-repository lock before mutating the working tree so that
    // concurrent undo calls targeting the same repo cannot interleave.
    let lock = repo_lock(root_dir);
    let _guard = lock.lock().await;

    let (ok_reset, _, stderr_reset) = run_git(root_dir, &["reset", "--hard", &commit]).await?;
    if !ok_reset {
        return Err(SnapshotError::Other(anyhow!("failed git reset --hard: {stderr_reset}")));
    }
    let (ok_clean, _, stderr_clean) = run_git(root_dir, &["clean", "-fd"]).await?;
    if !ok_clean {
        return Err(SnapshotError::Other(anyhow!("failed git clean -fd: {stderr_clean}")));
    }

    let meta_path = metadata_dir(root_dir).await?.join(format!("{resolved_snapshot}.json"));
    let checkpoint = match fs::read_to_string(meta_path).await {
        Ok(payload) => {
            serde_json::from_str::<SnapshotRecord>(&payload).ok().and_then(|r| r.checkpoint)
        }
        Err(_) => None,
    };

    Ok(UndoOutcome {
        snapshot_id: resolved_snapshot,
        filesystem_restored: true,
        checkpoint_restored: checkpoint.is_some(),
        checkpoint,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run_git_sync(root: &Path, args: &[&str]) {
        let out = std::process::Command::new("git")
            .args(args)
            .current_dir(root)
            .output()
            .expect("git should execute");
        assert!(
            out.status.success(),
            "git command failed: {:?} stderr={}",
            args,
            String::from_utf8_lossy(&out.stderr)
        );
    }

    fn init_git_repo(root: &Path) {
        run_git_sync(root, &["init"]);
        std::fs::write(root.join("README.md"), "seed\n").expect("seed write");
        run_git_sync(root, &["add", "README.md"]);
        run_git_sync(
            root,
            &[
                "-c",
                "user.name=LG Runner Tests",
                "-c",
                "user.email=tests@example.invalid",
                "commit",
                "-m",
                "seed",
            ],
        );
    }

    #[tokio::test]
    async fn test_non_git_workspace_returns_error() {
        let td = tempfile::tempdir().expect("tempdir");
        let err = create_snapshot(td.path(), "apply_patch", None)
            .await
            .expect_err("expected non-git error");
        assert!(matches!(err, SnapshotError::NonGitWorkspace));
    }

    #[tokio::test]
    async fn test_create_snapshot_and_undo_restore_filesystem_and_checkpoint() {
        let td = tempfile::tempdir().expect("tempdir");
        init_git_repo(td.path());

        std::fs::write(td.path().join("README.md"), "before\n").expect("write before");
        run_git_sync(td.path(), &["add", "README.md"]);
        run_git_sync(
            td.path(),
            &[
                "-c",
                "user.name=LG Runner Tests",
                "-c",
                "user.email=tests@example.invalid",
                "commit",
                "-m",
                "before snapshot",
            ],
        );

        let pointer = CheckpointPointer {
            thread_id: "thread-1".to_string(),
            checkpoint_ns: "main".to_string(),
            checkpoint_id: Some("cp-1".to_string()),
            run_id: Some("run-1".to_string()),
        };

        let snapshot = create_snapshot(td.path(), "apply_patch", Some(pointer.clone()))
            .await
            .expect("snapshot created");

        std::fs::write(td.path().join("README.md"), "after\n").expect("write after");
        std::fs::write(td.path().join("temp.txt"), "tmp\n").expect("write temp");

        let out = undo_to_snapshot(td.path(), Some(snapshot.snapshot_id.as_str()))
            .await
            .expect("undo should succeed");

        assert!(out.filesystem_restored);
        assert!(out.checkpoint_restored);
        assert_eq!(out.checkpoint, Some(pointer));
        assert_eq!(
            std::fs::read_to_string(td.path().join("README.md"))
                .expect("read restored file")
                .replace("\r\n", "\n"),
            "before\n"
        );
        assert!(!td.path().join("temp.txt").exists());
    }

    #[tokio::test]
    async fn test_undo_snapshot_not_found_returns_error() {
        let td = tempfile::tempdir().expect("tempdir");
        init_git_repo(td.path());
        let err = undo_to_snapshot(td.path(), Some("snap-does-not-exist"))
            .await
            .expect_err("expected snapshot not found");
        assert!(matches!(err, SnapshotError::SnapshotNotFound(_)));
    }

    #[test]
    fn test_validate_snapshot_id_valid() {
        assert!(validate_snapshot_id("abc-123_XYZ").is_ok());
        assert!(validate_snapshot_id("a").is_ok());
        assert!(validate_snapshot_id(&"x".repeat(64)).is_ok());
    }

    #[test]
    fn test_validate_snapshot_id_empty() {
        assert!(validate_snapshot_id("").is_err());
    }

    #[test]
    fn test_validate_snapshot_id_too_long() {
        assert!(validate_snapshot_id(&"x".repeat(65)).is_err());
    }

    #[test]
    fn test_validate_snapshot_id_git_flag_injection() {
        assert!(validate_snapshot_id("--force").is_err());
        assert!(validate_snapshot_id("refs/heads/main").is_err());
        assert!(validate_snapshot_id("../escape").is_err());
    }
}
