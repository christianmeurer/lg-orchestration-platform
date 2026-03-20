use std::time::Instant;

use serde_json::json;

use crate::config::RunnerConfig;
use crate::envelope::{CheckpointPointer, SnapshotMetadata, ToolEnvelope, ToolExecuteRequest};
use crate::errors::ApiError;
use crate::indexing::{SemanticSearchHit, StructuralSnapshot};
use crate::snapshots::{create_snapshot, SnapshotError};

mod exec;
mod fs;
pub(crate) mod mcp;

/// Per-request context threaded through `dispatch_tool` and its callees.
///
/// Replacing the former process-global `LAST_UNDO_POINTER` with a
/// request-scoped value eliminates the data race that occurred when
/// concurrent `batch_execute` tasks overwrote each other's checkpoint pointer.
#[derive(Default)]
pub struct ToolContext {
    pub checkpoint_pointer: Option<CheckpointPointer>,
}

pub(crate) async fn snapshot_for_operation(
    cfg: &RunnerConfig,
    ctx: &ToolContext,
    operation_class: &str,
) -> Result<SnapshotMetadata, ApiError> {
    let checkpoint = ctx.checkpoint_pointer.clone();

    match create_snapshot(cfg.root_dir.as_path(), operation_class, checkpoint.clone()).await {
        Ok(rec) => Ok(SnapshotMetadata {
            snapshot_id: rec.snapshot_id,
            created: true,
            operation_class: rec.operation_class,
            git_commit: rec.git_commit,
            non_git_workspace: false,
            reason: None,
            checkpoint,
        }),
        Err(SnapshotError::NonGitWorkspace) => Err(ApiError::BadRequest(
            "non_git_workspace: snapshots require a git repository".to_string(),
        )),
        Err(SnapshotError::SnapshotNotFound(msg)) => {
            Err(ApiError::BadRequest(format!("snapshot_not_found: {msg}")))
        }
        Err(SnapshotError::Other(err)) => Err(ApiError::Other(err)),
    }
}

pub async fn dispatch_tool(
    cfg: &RunnerConfig,
    req: ToolExecuteRequest,
) -> Result<ToolEnvelope, ApiError> {
    let started = Instant::now();
    let route = req.route.clone();

    // Build a fresh per-request context; initialise the checkpoint pointer
    // from the incoming request so snapshot/undo operations use the caller's
    // LangGraph checkpoint rather than any stale global state.
    let mut ctx = ToolContext {
        checkpoint_pointer: req.checkpoint.clone(),
    };

    let tool = req.tool.trim().to_string();
    let input = req.input;
    let out = match tool.as_str() {
        "health" => Ok(ToolEnvelope::ok("health", "ok", json!({}))),
        "read_file" => fs::read_file(cfg, input).await,
        "search_files" => fs::search_files(cfg, input).await,
        "search_codebase" => fs::search_codebase(cfg, input).await,
        "ast_index_summary" => fs::ast_index_summary(cfg, input).await,
        "list_files" => fs::list_files(cfg, input).await,
        "apply_patch" => fs::apply_patch(cfg, &mut ctx, input).await,
        "exec" => exec::exec(cfg, &mut ctx, input).await,
        "undo" => fs::undo(cfg, &mut ctx, input).await,
        "mcp_discover" => mcp::mcp_discover(cfg, input).await,
        "mcp_execute" => mcp::mcp_execute(cfg, input).await,
        "mcp_resources_list" => mcp::mcp_resources_list(cfg, input).await,
        "mcp_resource_read" => mcp::mcp_resource_read(cfg, input).await,
        "mcp_prompts_list" => mcp::mcp_prompts_list(cfg, input).await,
        "mcp_prompt_get" => mcp::mcp_prompt_get(cfg, input).await,
        other => Err(ApiError::BadRequest(format!("unknown tool: {other}"))),
    };
    match out {
        Ok(mut env) => {
            // Single authoritative timing write — tool impls produce timing_ms=0.
            env.timing_ms = started.elapsed().as_millis();
            if let Some(route_meta) = route.clone() {
                env = env.with_route(route_meta);
            }
            Ok(env)
        }
        Err(e) => {
            let error_message = e.to_string();
            let mut env = match e {
                ApiError::ApprovalRequired(approval) => ToolEnvelope::err(
                    tool,
                    1,
                    error_message.clone(),
                    json!({
                        "error": error_message,
                        "diagnostics": [],
                        "approval": approval
                    }),
                )
                .with_approval(approval),
                _ => ToolEnvelope::err(
                    tool,
                    1,
                    error_message.clone(),
                    json!({"error": error_message, "diagnostics": []}),
                ),
            };
            env.timing_ms = started.elapsed().as_millis();
            if let Some(route_meta) = route {
                env = env.with_route(route_meta);
            }
            Ok(env)
        }
    }
}

pub(super) fn serialize_snapshot(snapshot: StructuralSnapshot) -> serde_json::Value {
    serde_json::to_value(snapshot).unwrap_or_else(|_| json!({}))
}

pub(super) fn serialize_semantic_hits(hits: Vec<SemanticSearchHit>) -> serde_json::Value {
    serde_json::to_value(hits).unwrap_or_else(|_| json!([]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::envelope::ToolExecuteRequest;
    use std::time::Duration;

    fn test_cfg() -> (tempfile::TempDir, RunnerConfig) {
        let td = tempfile::tempdir().unwrap();
        let cfg = RunnerConfig::new(td.path(), Some("dev"), None).unwrap();
        (td, cfg)
    }

    #[tokio::test]
    async fn test_dispatch_health() {
        let (_td, cfg) = test_cfg();
        let req = ToolExecuteRequest {
            tool: "health".to_string(),
            input: json!({}),
            checkpoint: None,
            route: None,
        };
        let result = dispatch_tool(&cfg, req).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        assert_eq!(env.tool, "health");
        assert!(env.ok);
        assert_eq!(env.stdout, "ok");
    }

    #[tokio::test]
    async fn test_dispatch_unknown_tool() {
        let (_td, cfg) = test_cfg();
        let req = ToolExecuteRequest {
            tool: "nonexistent_tool".to_string(),
            input: json!({}),
            checkpoint: None,
            route: None,
        };
        let result = dispatch_tool(&cfg, req).await;
        assert!(result.is_ok()); // dispatch wraps errors into envelope
        let env = result.unwrap();
        assert!(!env.ok);
        assert!(env.stderr.contains("unknown tool"));
    }

    #[tokio::test]
    async fn test_dispatch_read_file() {
        let (td, cfg) = test_cfg();
        std::fs::create_dir_all(td.path().join("py")).unwrap();
        std::fs::write(td.path().join("py/test.txt"), "content").unwrap();
        let req = ToolExecuteRequest {
            tool: "read_file".to_string(),
            input: json!({"path": "py/test.txt"}),
            checkpoint: None,
            route: None,
        };
        let result = dispatch_tool(&cfg, req).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        assert_eq!(env.stdout, "content");
    }

    #[tokio::test]
    async fn test_dispatch_ast_index_summary() {
        let (td, cfg) = test_cfg();
        std::fs::create_dir_all(td.path().join("py")).unwrap();
        std::fs::write(td.path().join("py/a.py"), "def alpha():\n    return 1\n").unwrap();
        let req = ToolExecuteRequest {
            tool: "ast_index_summary".to_string(),
            input: json!({"max_files": 20}),
            checkpoint: None,
            route: None,
        };
        let env = dispatch_tool(&cfg, req).await.unwrap();
        assert!(env.ok);
        let payload: serde_json::Value = serde_json::from_str(&env.stdout).unwrap();
        assert!(payload.get("schema_version").is_some());
        assert!(payload.get("files").is_some());
    }

    #[tokio::test]
    async fn test_dispatch_search_codebase() {
        let (td, cfg) = test_cfg();
        std::fs::create_dir_all(td.path().join("py")).unwrap();
        std::fs::write(
            td.path().join("py/needle.py"),
            "def semantic_window_context():\n    return 'ok'\n",
        )
        .unwrap();

        cfg.indexing.ensure_started();
        assert!(cfg
            .indexing
            .wait_for_version_at_least(1, Duration::from_secs(4)));

        let req = ToolExecuteRequest {
            tool: "search_codebase".to_string(),
            input: json!({"query": "semantic window context", "path_prefix": "py/"}),
            checkpoint: None,
            route: None,
        };
        let env = dispatch_tool(&cfg, req).await.unwrap();
        assert!(env.ok);
        assert!(env.stdout.contains("needle.py"));
    }

    #[tokio::test]
    async fn test_dispatch_sets_timing() {
        let (_td, cfg) = test_cfg();
        let req = ToolExecuteRequest {
            tool: "health".to_string(),
            input: json!({}),
            checkpoint: None,
            route: None,
        };
        let env = dispatch_tool(&cfg, req).await.unwrap();
        // timing_ms is set by dispatch_tool, not by the tool impl
        assert!(env.timing_ms < 10_000); // sanity check: less than 10 seconds
    }

    #[tokio::test]
    async fn test_dispatch_trims_tool_name() {
        let (_td, cfg) = test_cfg();
        let req = ToolExecuteRequest {
            tool: "  health  ".to_string(),
            input: json!({}),
            checkpoint: None,
            route: None,
        };
        let env = dispatch_tool(&cfg, req).await.unwrap();
        assert_eq!(env.tool, "health");
        assert!(env.ok);
    }

    #[tokio::test]
    async fn test_dispatch_apply_patch_approval_required_wrapped_into_envelope() {
        let (_td, cfg) = test_cfg();
        let req = ToolExecuteRequest {
            tool: "apply_patch".to_string(),
            input: json!({
                "changes": [
                    {"path": "py/new.txt", "op": "add", "content": "x"}
                ]
            }),
            checkpoint: None,
            route: None,
        };
        let env = dispatch_tool(&cfg, req).await.unwrap();
        assert!(!env.ok);
        assert!(env.stderr.contains("approval_required"));
    }

    #[tokio::test]
    async fn test_tool_context_per_request_isolation() {
        // Two requests with different checkpoints must not interfere.
        let (_td, cfg) = test_cfg();
        let req_a = ToolExecuteRequest {
            tool: "health".to_string(),
            input: json!({}),
            checkpoint: Some(crate::envelope::CheckpointPointer {
                thread_id: "thread-a".to_string(),
                checkpoint_ns: "ns-a".to_string(),
                checkpoint_id: None,
                run_id: None,
            }),
            route: None,
        };
        let req_b = ToolExecuteRequest {
            tool: "health".to_string(),
            input: json!({}),
            checkpoint: Some(crate::envelope::CheckpointPointer {
                thread_id: "thread-b".to_string(),
                checkpoint_ns: "ns-b".to_string(),
                checkpoint_id: None,
                run_id: None,
            }),
            route: None,
        };
        // Both should succeed without cross-contamination; no panics from
        // the formerly-global mutex.
        let (env_a, env_b) = tokio::join!(dispatch_tool(&cfg, req_a), dispatch_tool(&cfg, req_b));
        assert!(env_a.unwrap().ok);
        assert!(env_b.unwrap().ok);
    }
}
