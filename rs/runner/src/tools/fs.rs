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
                out.push(rel.to_string_lossy().to_string());
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
            out.push(rel.to_string_lossy().to_string());
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
