use std::path::PathBuf;

use serde::Deserialize;
use serde_json::{json, Value};
use tokio::fs;

use super::{
    restore_checkpoint_alignment, serialize_semantic_hits, serialize_snapshot,
    snapshot_for_operation,
};
use crate::approval::{require_approval, ApprovalTokenInput};
use crate::config::RunnerConfig;
use crate::envelope::{ToolEnvelope, UndoMetadata};
use crate::errors::ApiError;
use crate::snapshots::{undo_to_snapshot, SnapshotError};

pub(super) fn resolve_under_root(cfg: &RunnerConfig, rel: &str) -> Result<PathBuf, ApiError> {
    let rel = rel.trim();
    if rel.is_empty() {
        return Err(ApiError::BadRequest("empty path".to_string()));
    }
    if rel.contains('\0') {
        return Err(ApiError::BadRequest("nul byte".to_string()));
    }

    let candidate = cfg.root_dir.join(rel);
    let norm = candidate.components().fold(PathBuf::new(), |mut acc, c| {
        acc.push(c);
        acc
    });

    let full = if norm.is_absolute() {
        norm
    } else {
        cfg.root_dir.join(norm)
    };

    let full = full
        .canonicalize()
        .unwrap_or_else(|_| cfg.root_dir.join(rel));

    if !full.starts_with(&cfg.root_dir) {
        return Err(ApiError::Forbidden("path escapes root".to_string()));
    }
    Ok(full)
}

#[derive(Debug, Deserialize)]
struct ReadFileIn {
    path: String,
}

async fn read_file_content(full: &PathBuf) -> Result<String, ApiError> {
    let is_pdf = full
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("pdf"));

    if is_pdf {
        let full_path = full.clone();
        let extracted = tokio::task::spawn_blocking(move || pdf_extract::extract_text(&full_path))
            .await
            .map_err(|e| ApiError::Other(anyhow::anyhow!("pdf extraction join error: {e}")))?;

        return extracted.map_err(|e| ApiError::BadRequest(format!("pdf_extract_failed: {e}")));
    }

    fs::read_to_string(full)
        .await
        .map_err(|e| ApiError::BadRequest(e.to_string()))
}

pub async fn read_file(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: ReadFileIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    if !cfg.can_read(&inp.path) {
        return Err(ApiError::Forbidden("read denied".to_string()));
    }
    let full = resolve_under_root(cfg, &inp.path)?;
    let content = read_file_content(&full).await?;
    Ok(ToolEnvelope::ok(
        "read_file",
        content,
        json!({"path": inp.path}),
        0,
    ))
}

#[derive(Debug, Deserialize)]
struct SearchFilesIn {
    path: String,
    regex: String,
    #[serde(default)]
    file_pattern: Option<String>,
}

pub async fn search_files(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: SearchFilesIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;

    if !cfg.can_read(&inp.path) {
        return Err(ApiError::Forbidden("read denied".to_string()));
    }
    let full = resolve_under_root(cfg, &inp.path)?;

    let re = regex::Regex::new(&inp.regex)
        .map_err(|e| ApiError::BadRequest(format!("invalid regex: {}", e)))?;

    let glob = match inp.file_pattern {
        Some(pat) => Some(
            globset::Glob::new(&pat)
                .map_err(|e| ApiError::BadRequest(format!("invalid glob: {}", e)))?
                .compile_matcher(),
        ),
        None => None,
    };

    let mut out: Vec<Value> = Vec::new();

    for entry in walkdir::WalkDir::new(full)
        .into_iter()
        .filter_map(Result::ok)
    {
        let p = entry.path();
        if p.is_file() {
            let rel = p.strip_prefix(&cfg.root_dir).unwrap_or(p);
            let rel_s = rel.to_string_lossy().to_string();

            if !cfg.can_read(&rel_s) {
                continue;
            }

            if let Some(ref matcher) = glob {
                if !matcher.is_match(&rel_s) {
                    continue;
                }
            }

            if let Ok(content) = std::fs::read_to_string(p) {
                let mut matches_in_file = Vec::new();
                for (line_idx, line) in content.lines().enumerate() {
                    if re.is_match(line) {
                        matches_in_file.push(json!({
                            "line_number": line_idx + 1,
                            "content": line
                        }));
                    }
                }
                if !matches_in_file.is_empty() {
                    out.push(json!({
                        "file": rel_s,
                        "matches": matches_in_file
                    }));
                }
            }
        }
    }

    Ok(ToolEnvelope::ok(
        "search_files",
        serde_json::to_string_pretty(&out).unwrap_or_default(),
        json!({"files_matched": out.len()}),
        0,
    ))
}

#[derive(Debug, Deserialize)]
struct SearchCodebaseIn {
    query: String,
    #[serde(default = "default_search_codebase_limit")]
    limit: usize,
    #[serde(default)]
    path_prefix: Option<String>,
}

fn default_search_codebase_limit() -> usize {
    10
}

#[derive(Debug, Deserialize)]
struct AstIndexSummaryIn {
    #[serde(default = "default_ast_summary_max_files")]
    max_files: usize,
    #[serde(default)]
    path_prefix: Option<String>,
}

impl Default for AstIndexSummaryIn {
    fn default() -> Self {
        Self {
            max_files: default_ast_summary_max_files(),
            path_prefix: None,
        }
    }
}

fn default_ast_summary_max_files() -> usize {
    200
}

pub async fn search_codebase(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: SearchCodebaseIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    let query = inp.query.trim();
    if query.is_empty() {
        return Err(ApiError::BadRequest("query is required".to_string()));
    }

    let limit = inp.limit.clamp(1, 50);
    let path_prefix = inp
        .path_prefix
        .as_deref()
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(|v| v.replace('\\', "/"));

    cfg.indexing.ensure_started();
    let hits = cfg
        .indexing
        .semantic_search(query, limit, path_prefix.as_deref())
        .map_err(ApiError::Other)?;
    let hits_json = serialize_semantic_hits(hits);

    Ok(ToolEnvelope::ok(
        "search_codebase",
        serde_json::to_string_pretty(&hits_json).unwrap_or_default(),
        json!({
            "engine": "sqlite_fts5",
            "local": true,
            "query": query,
            "limit": limit,
            "path_prefix": path_prefix,
            "hits": hits_json.as_array().map_or(0, Vec::len),
            "snapshot_version": cfg.indexing.current_version()
        }),
        0,
    ))
}

pub async fn ast_index_summary(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp = if input.is_null() {
        AstIndexSummaryIn::default()
    } else {
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?
    };

    let max_files = inp.max_files.clamp(1, 2_000);
    let path_prefix = inp
        .path_prefix
        .as_deref()
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(|v| v.replace('\\', "/"));

    cfg.indexing.ensure_started();
    let mut snapshot = cfg.indexing.snapshot();
    if let Some(prefix) = path_prefix.as_deref() {
        snapshot
            .files
            .retain(|entry| entry.path.starts_with(prefix));
    }
    if snapshot.files.len() > max_files {
        snapshot.files.truncate(max_files);
    }
    let symbols_total = snapshot.files.iter().map(|entry| entry.symbols.len()).sum();
    let files_returned = snapshot.files.len();
    snapshot.symbols_total = symbols_total;
    let payload = serialize_snapshot(snapshot.clone());

    Ok(ToolEnvelope::ok(
        "ast_index_summary",
        serde_json::to_string_pretty(&payload).unwrap_or_default(),
        json!({
            "schema_version": snapshot.schema_version,
            "snapshot_version": snapshot.version,
            "files_indexed": snapshot.files_indexed,
            "files_returned": files_returned,
            "symbols_total": symbols_total,
            "path_prefix": path_prefix,
            "local": true
        }),
        0,
    ))
}

#[derive(Debug, Deserialize)]
struct ListFilesIn {
    path: String,
    #[serde(default)]
    recursive: bool,
}

pub async fn list_files(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: ListFilesIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    if !cfg.can_read(&inp.path) {
        return Err(ApiError::Forbidden("read denied".to_string()));
    }
    let full = resolve_under_root(cfg, &inp.path)?;

    let mut out: Vec<String> = Vec::new();
    if inp.recursive {
        for entry in walkdir::WalkDir::new(full)
            .into_iter()
            .filter_map(Result::ok)
        {
            let p = entry.path();
            if p.is_file() {
                let rel = p.strip_prefix(&cfg.root_dir).unwrap_or(p);
                let rel_s = rel.to_string_lossy().to_string();
                if cfg.can_read(&rel_s) {
                    out.push(rel_s);
                }
            }
        }
    } else {
        let mut rd = fs::read_dir(full)
            .await
            .map_err(|e| ApiError::BadRequest(e.to_string()))?;
        while let Some(ent) = rd
            .next_entry()
            .await
            .map_err(|e| ApiError::BadRequest(e.to_string()))?
        {
            let p = ent.path();
            let rel = p.strip_prefix(&cfg.root_dir).unwrap_or(&p);
            let rel_s = rel.to_string_lossy().to_string();
            if cfg.can_read(&rel_s) {
                out.push(rel_s);
            }
        }
    }
    out.sort();
    Ok(ToolEnvelope::ok(
        "list_files",
        serde_json::to_string(&out).unwrap_or_default(),
        json!({"count": out.len()}),
        0,
    ))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum ChangeOp {
    Add,
    Update,
    Delete,
}

#[derive(Debug, Deserialize)]
struct FileChange {
    path: String,
    op: ChangeOp,
    #[serde(default)]
    content: String,
}

#[derive(Debug, Deserialize)]
struct ApplyPatchIn {
    changes: Vec<FileChange>,
    #[serde(default)]
    approval: Option<ApprovalTokenInput>,
}

#[derive(Debug, Deserialize)]
struct UndoIn {
    #[serde(default)]
    snapshot_id: Option<String>,
}

pub async fn apply_patch(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: ApplyPatchIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    if inp.changes.is_empty() {
        return Err(ApiError::BadRequest("no changes".to_string()));
    }

    let approval = require_approval(inp.approval, "apply_patch", "approval:apply_patch")?;
    let snapshot = snapshot_for_operation(cfg, "apply_patch").await?;

    let mut diffs: Vec<Value> = Vec::new();
    for ch in inp.changes {
        if !cfg.can_write(&ch.path) {
            return Err(ApiError::Forbidden("write denied".to_string()));
        }
        let full = resolve_under_root(cfg, &ch.path)?;
        let full_rel = full.strip_prefix(&cfg.root_dir).unwrap_or(&full);
        let full_rel_s = full_rel.to_string_lossy().to_string();

        match ch.op {
            ChangeOp::Add => {
                if full.exists() {
                    return Err(ApiError::BadRequest(format!("exists: {}", ch.path)));
                }
                if let Some(parent) = full.parent() {
                    fs::create_dir_all(parent)
                        .await
                        .map_err(|e| ApiError::Other(e.into()))?;
                }
                fs::write(&full, ch.content.as_bytes())
                    .await
                    .map_err(|e| ApiError::Other(e.into()))?;
                diffs.push(json!({"path": full_rel_s, "op": "add", "bytes": ch.content.len()}));
            }
            ChangeOp::Update => {
                if !full.exists() {
                    return Err(ApiError::BadRequest(format!("missing: {}", ch.path)));
                }
                let old = fs::read_to_string(&full).await.unwrap_or_default();
                fs::write(&full, ch.content.as_bytes())
                    .await
                    .map_err(|e| ApiError::Other(e.into()))?;
                diffs.push(json!({"path": full_rel_s, "op": "update", "old_bytes": old.len(), "new_bytes": ch.content.len()}));
            }
            ChangeOp::Delete => {
                if full.exists() {
                    fs::remove_file(&full)
                        .await
                        .map_err(|e| ApiError::Other(e.into()))?;
                }
                diffs.push(json!({"path": full_rel_s, "op": "delete"}));
            }
        }
    }

    Ok(
        ToolEnvelope::ok("apply_patch", "ok", json!({"changes": diffs}), 0)
            .with_approval(approval)
            .with_snapshot(snapshot),
    )
}

pub async fn undo(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: UndoIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;

    let outcome = match undo_to_snapshot(cfg.root_dir.as_path(), inp.snapshot_id.as_deref()).await {
        Ok(v) => v,
        Err(SnapshotError::NonGitWorkspace) => {
            return Err(ApiError::BadRequest(
                "non_git_workspace: undo requires a git repository".to_string(),
            ))
        }
        Err(SnapshotError::SnapshotNotFound(msg)) => {
            return Err(ApiError::BadRequest(format!("snapshot_not_found: {msg}")))
        }
        Err(SnapshotError::Other(err)) => return Err(ApiError::Other(err)),
    };

    restore_checkpoint_alignment(outcome.checkpoint.clone());

    let undo_meta = UndoMetadata {
        requested_snapshot_id: inp.snapshot_id,
        restored_snapshot_id: outcome.snapshot_id.clone(),
        filesystem_restored: outcome.filesystem_restored,
        checkpoint_restored: outcome.checkpoint_restored,
        checkpoint: outcome.checkpoint.clone(),
        reason: None,
    };

    Ok(ToolEnvelope::ok(
        "undo",
        "ok",
        json!({
            "snapshot_id": outcome.snapshot_id,
            "filesystem_restored": outcome.filesystem_restored,
            "checkpoint_restored": outcome.checkpoint_restored,
            "checkpoint": outcome.checkpoint,
        }),
        0,
    )
    .with_undo(undo_meta))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::RunnerConfig;
    use std::fs as stdfs;
    use std::path::Path;
    use std::time::Duration;

    fn test_cfg() -> (tempfile::TempDir, RunnerConfig) {
        let td = tempfile::tempdir().unwrap();
        stdfs::create_dir_all(td.path().join("py")).unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        (td, cfg)
    }

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
        stdfs::write(root.join("README.md"), "seed\n").expect("seed write");
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

    // --- resolve_under_root tests ---

    #[test]
    fn test_resolve_empty_path() {
        let (_td, cfg) = test_cfg();
        let result = resolve_under_root(&cfg, "");
        assert!(result.is_err());
    }

    #[test]
    fn test_resolve_whitespace_only_path() {
        let (_td, cfg) = test_cfg();
        let result = resolve_under_root(&cfg, "   ");
        assert!(result.is_err());
    }

    #[test]
    fn test_resolve_nul_byte() {
        let (_td, cfg) = test_cfg();
        let result = resolve_under_root(&cfg, "file\0.txt");
        assert!(result.is_err());
    }

    #[test]
    fn test_resolve_valid_path() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("test.txt"), "hello").unwrap();
        let result = resolve_under_root(&cfg, "test.txt");
        assert!(result.is_ok());
        let path = result.unwrap();
        assert!(path.starts_with(&cfg.root_dir));
    }

    // --- read_file tests ---

    #[tokio::test]
    async fn test_read_file_success() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("py/hello.txt"), "world").unwrap();
        let result = read_file(&cfg, json!({"path": "py/hello.txt"})).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        assert_eq!(env.stdout, "world");
    }

    #[tokio::test]
    async fn test_read_file_missing() {
        let (_td, cfg) = test_cfg();
        let result = read_file(&cfg, json!({"path": "missing.txt"})).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_read_file_bad_input() {
        let (_td, cfg) = test_cfg();
        let result = read_file(&cfg, json!({"wrong_key": 123})).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_read_file_pdf_invalid_payload_returns_bad_request() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("py/spec.pdf"), b"not-a-valid-pdf").unwrap();

        let result = read_file(&cfg, json!({"path": "py/spec.pdf"})).await;
        assert!(result.is_err());
        assert!(matches!(result, Err(ApiError::BadRequest(_))));
    }

    #[tokio::test]
    async fn test_search_files_success() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("py/test1.txt"), "hello world\nline 2").unwrap();
        stdfs::write(td.path().join("py/test2.txt"), "foo\nbar").unwrap();

        let result = search_files(
            &cfg,
            json!({
                "path": "py",
                "regex": "hello"
            }),
        )
        .await;

        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        assert!(env.stdout.contains("hello world"));
        assert!(env.stdout.contains("test1.txt"));
        assert!(!env.stdout.contains("test2.txt"));
    }

    #[tokio::test]
    async fn test_search_codebase_success() {
        let (td, cfg) = test_cfg();
        stdfs::write(
            td.path().join("py/semantic.py"),
            "def ultra_memory_window():\n    return 1\n",
        )
        .unwrap();

        cfg.indexing.ensure_started();
        assert!(cfg
            .indexing
            .wait_for_version_at_least(1, Duration::from_secs(4)));

        let result = search_codebase(
            &cfg,
            json!({
                "query": "ultra memory window",
                "limit": 5,
                "path_prefix": "py/"
            }),
        )
        .await;

        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        let hits: Vec<Value> = serde_json::from_str(&env.stdout).unwrap();
        assert!(hits.iter().any(|hit| {
            hit.get("path")
                .and_then(Value::as_str)
                .map(|p| p.ends_with("py/semantic.py"))
                .unwrap_or(false)
        }));
    }

    #[tokio::test]
    async fn test_search_codebase_empty_query_fails() {
        let (_td, cfg) = test_cfg();
        let result = search_codebase(&cfg, json!({"query": "   "})).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_ast_index_summary_success() {
        let (td, cfg) = test_cfg();
        stdfs::create_dir_all(td.path().join("rs")).unwrap();
        stdfs::write(td.path().join("rs/lib.rs"), "pub fn alpha() -> i32 { 1 }\n").unwrap();

        cfg.indexing.ensure_started();
        assert!(cfg
            .indexing
            .wait_for_version_at_least(1, Duration::from_secs(4)));

        let result = ast_index_summary(&cfg, json!({"max_files": 50})).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        let payload: Value = serde_json::from_str(&env.stdout).unwrap();
        let files = payload
            .get("files")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        assert!(files.iter().any(|entry| {
            entry
                .get("path")
                .and_then(Value::as_str)
                .map(|p| p.ends_with("rs/lib.rs"))
                .unwrap_or(false)
        }));
    }

    // --- list_files tests ---

    #[tokio::test]
    async fn test_list_files_non_recursive() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("py/a.txt"), "").unwrap();
        stdfs::write(td.path().join("py/b.txt"), "").unwrap();
        let result = list_files(&cfg, json!({"path": "py", "recursive": false})).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        let files: Vec<String> = serde_json::from_str(&env.stdout).unwrap();
        assert!(files.iter().any(|f| f.contains("a.txt")));
        assert!(files.iter().any(|f| f.contains("b.txt")));
    }

    #[tokio::test]
    async fn test_list_files_recursive() {
        let (td, cfg) = test_cfg();
        stdfs::create_dir_all(td.path().join("py/sub")).unwrap();
        stdfs::write(td.path().join("py/sub/deep.txt"), "").unwrap();
        let result = list_files(&cfg, json!({"path": "py", "recursive": true})).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        let files: Vec<String> = serde_json::from_str(&env.stdout).unwrap();
        assert!(files.iter().any(|f| f.contains("deep.txt")));
    }

    // --- apply_patch tests ---

    #[tokio::test]
    async fn test_apply_patch_add() {
        let (td, cfg) = test_cfg();
        init_git_repo(td.path());
        let input = json!({
            "changes": [{"path": "py/new.txt", "op": "add", "content": "hello"}],
            "approval": {"challenge_id": "approval:apply_patch", "token": "approve:approval:apply_patch"}
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        assert!(env.snapshot.is_some());
    }

    #[tokio::test]
    async fn test_apply_patch_add_existing_fails() {
        let (td, cfg) = test_cfg();
        init_git_repo(td.path());
        stdfs::write(td.path().join("py/exists.txt"), "old").unwrap();
        let input = json!({
            "changes": [{"path": "py/exists.txt", "op": "add", "content": "new"}],
            "approval": {"challenge_id": "approval:apply_patch", "token": "approve:approval:apply_patch"}
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_apply_patch_update() {
        let (td, cfg) = test_cfg();
        init_git_repo(td.path());
        stdfs::write(td.path().join("py/update.txt"), "old content").unwrap();
        let input = json!({
            "changes": [{"path": "py/update.txt", "op": "update", "content": "new content"}],
            "approval": {"challenge_id": "approval:apply_patch", "token": "approve:approval:apply_patch"}
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_ok());
        let content = stdfs::read_to_string(td.path().join("py/update.txt")).unwrap();
        assert_eq!(content, "new content");
    }

    #[tokio::test]
    async fn test_apply_patch_update_missing_fails() {
        let (td, cfg) = test_cfg();
        init_git_repo(td.path());
        let input = json!({
            "changes": [{"path": "py/missing.txt", "op": "update", "content": "x"}],
            "approval": {"challenge_id": "approval:apply_patch", "token": "approve:approval:apply_patch"}
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_apply_patch_delete() {
        let (td, cfg) = test_cfg();
        init_git_repo(td.path());
        stdfs::write(td.path().join("py/delete_me.txt"), "bye").unwrap();
        let input = json!({
            "changes": [{"path": "py/delete_me.txt", "op": "delete"}],
            "approval": {"challenge_id": "approval:apply_patch", "token": "approve:approval:apply_patch"}
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_ok());
        assert!(!td.path().join("py/delete_me.txt").exists());
    }

    #[tokio::test]
    async fn test_apply_patch_empty_changes_fails() {
        let (td, cfg) = test_cfg();
        init_git_repo(td.path());
        let input = json!({
            "changes": [],
            "approval": {"challenge_id": "approval:apply_patch", "token": "approve:approval:apply_patch"}
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_apply_patch_missing_approval_rejected() {
        let (td, cfg) = test_cfg();
        init_git_repo(td.path());
        let input = json!({
            "changes": [{"path": "py/new_reject.txt", "op": "add", "content": "hello"}]
        });
        let result = apply_patch(&cfg, input).await;
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
    }

    #[tokio::test]
    async fn test_undo_non_git_repo_fails_deterministically() {
        let (_td, cfg) = test_cfg();
        let result = undo(&cfg, json!({})).await;
        assert!(matches!(result, Err(ApiError::BadRequest(_))));
        if let Err(ApiError::BadRequest(msg)) = result {
            assert!(msg.contains("non_git_workspace"));
        }
    }
}
