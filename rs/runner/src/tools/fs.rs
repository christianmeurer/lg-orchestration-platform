use std::path::PathBuf;

use serde::Deserialize;
use serde_json::{json, Value};
use tokio::fs;

use crate::config::RunnerConfig;
use crate::envelope::ToolEnvelope;
use crate::errors::ApiError;

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

pub async fn read_file(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: ReadFileIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    if !cfg.can_read(&inp.path) {
        return Err(ApiError::Forbidden("read denied".to_string()));
    }
    let full = resolve_under_root(cfg, &inp.path)?;
    let content = fs::read_to_string(&full)
        .await
        .map_err(|e| ApiError::BadRequest(e.to_string()))?;
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
        Some(pat) => {
            Some(globset::Glob::new(&pat)
                .map_err(|e| ApiError::BadRequest(format!("invalid glob: {}", e)))?
                .compile_matcher())
        },
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
}

pub async fn apply_patch(cfg: &RunnerConfig, input: Value) -> Result<ToolEnvelope, ApiError> {
    let inp: ApplyPatchIn =
        serde_json::from_value(input).map_err(|e| ApiError::BadRequest(e.to_string()))?;
    if inp.changes.is_empty() {
        return Err(ApiError::BadRequest("no changes".to_string()));
    }

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

    Ok(ToolEnvelope::ok(
        "apply_patch",
        "ok",
        json!({"changes": diffs}),
        0,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::RunnerConfig;
    use std::fs as stdfs;

    fn test_cfg() -> (tempfile::TempDir, RunnerConfig) {
        let td = tempfile::tempdir().unwrap();
        stdfs::create_dir_all(td.path().join("py")).unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        (td, cfg)
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
    async fn test_search_files_success() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("py/test1.txt"), "hello world\nline 2").unwrap();
        stdfs::write(td.path().join("py/test2.txt"), "foo\nbar").unwrap();
        
        let result = search_files(&cfg, json!({
            "path": "py",
            "regex": "hello"
        })).await;
        
        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        assert!(env.stdout.contains("hello world"));
        assert!(env.stdout.contains("test1.txt"));
        assert!(!env.stdout.contains("test2.txt"));
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
        let (_td, cfg) = test_cfg();
        let input = json!({
            "changes": [{"path": "py/new.txt", "op": "add", "content": "hello"}]
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
    }

    #[tokio::test]
    async fn test_apply_patch_add_existing_fails() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("py/exists.txt"), "old").unwrap();
        let input = json!({
            "changes": [{"path": "py/exists.txt", "op": "add", "content": "new"}]
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_apply_patch_update() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("py/update.txt"), "old content").unwrap();
        let input = json!({
            "changes": [{"path": "py/update.txt", "op": "update", "content": "new content"}]
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_ok());
        let content = stdfs::read_to_string(td.path().join("py/update.txt")).unwrap();
        assert_eq!(content, "new content");
    }

    #[tokio::test]
    async fn test_apply_patch_update_missing_fails() {
        let (_td, cfg) = test_cfg();
        let input = json!({
            "changes": [{"path": "py/missing.txt", "op": "update", "content": "x"}]
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_apply_patch_delete() {
        let (td, cfg) = test_cfg();
        stdfs::write(td.path().join("py/delete_me.txt"), "bye").unwrap();
        let input = json!({
            "changes": [{"path": "py/delete_me.txt", "op": "delete"}]
        });
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_ok());
        assert!(!td.path().join("py/delete_me.txt").exists());
    }

    #[tokio::test]
    async fn test_apply_patch_empty_changes_fails() {
        let (_td, cfg) = test_cfg();
        let input = json!({"changes": []});
        let result = apply_patch(&cfg, input).await;
        assert!(result.is_err());
    }
}
