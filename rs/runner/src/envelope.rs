// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Diagnostic {
    pub file: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub column: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fingerprint: Option<String>,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct RouteMetadata {
    #[serde(default)]
    pub stage: String,
    #[serde(default)]
    pub lane: String,
    #[serde(default)]
    pub provider: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub task_class: String,
    #[serde(default)]
    pub cache_affinity: String,
    #[serde(default)]
    pub prefix_segment: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IsolationMetadata {
    pub backend: String,
    pub degraded: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default)]
    pub policy_constraints: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ApprovalMetadata {
    pub required: bool,
    pub status: String,
    pub operation_class: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub challenge_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CheckpointPointer {
    pub thread_id: String,
    pub checkpoint_ns: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub checkpoint_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SnapshotMetadata {
    pub snapshot_id: String,
    pub created: bool,
    pub operation_class: String,
    pub git_commit: String,
    pub non_git_workspace: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub checkpoint: Option<CheckpointPointer>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UndoMetadata {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub requested_snapshot_id: Option<String>,
    pub restored_snapshot_id: String,
    pub filesystem_restored: bool,
    pub checkpoint_restored: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub checkpoint: Option<CheckpointPointer>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct RedactionMetadata {
    pub total: u32,
    pub paths: u32,
    pub usernames: u32,
    pub ip_addresses: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct McpMetadata {
    pub server_name: String,
    pub handshake_completed: bool,
    pub outbound_redactions: RedactionMetadata,
    pub inbound_redactions: RedactionMetadata,
}

#[derive(Debug, Deserialize)]
pub struct ToolExecuteRequest {
    pub tool: String,
    #[serde(default)]
    pub input: Value,
    #[serde(default)]
    pub checkpoint: Option<CheckpointPointer>,
    #[serde(default)]
    pub route: Option<RouteMetadata>,
}

#[derive(Debug, Deserialize)]
pub struct ToolBatchExecuteRequest {
    pub calls: Vec<ToolExecuteRequest>,
}

#[derive(Debug, Serialize)]
pub struct ToolBatchExecuteResponse {
    pub results: Vec<ToolEnvelope>,
}

#[derive(Debug, Serialize)]
pub struct ToolEnvelope {
    pub tool: String,
    pub ok: bool,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    #[serde(default)]
    pub diagnostics: Vec<Diagnostic>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub isolation: Option<IsolationMetadata>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub approval: Option<ApprovalMetadata>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub snapshot: Option<SnapshotMetadata>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub undo: Option<UndoMetadata>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mcp: Option<McpMetadata>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub route: Option<RouteMetadata>,
    pub timing_ms: u64,
    pub artifacts: Value,
}

impl ToolEnvelope {
    /// Construct a successful [`ToolEnvelope`].
    ///
    /// `timing_ms` defaults to `0`; callers at the dispatch boundary
    /// (i.e. [`crate::tools::dispatch_tool`]) overwrite it once after the
    /// tool completes so there is a single source of truth for wall-clock time.
    pub fn ok(tool: impl Into<String>, stdout: impl Into<String>, artifacts: Value) -> Self {
        Self {
            tool: tool.into(),
            ok: true,
            exit_code: 0,
            stdout: stdout.into(),
            stderr: String::new(),
            diagnostics: Vec::new(),
            isolation: None,
            approval: None,
            snapshot: None,
            undo: None,
            mcp: None,
            route: None,
            timing_ms: 0,
            artifacts,
        }
    }

    /// Construct a failed [`ToolEnvelope`].
    ///
    /// `timing_ms` defaults to `0` for the same reason as [`Self::ok`].
    pub fn err(
        tool: impl Into<String>,
        exit_code: i32,
        stderr: impl Into<String>,
        artifacts: Value,
    ) -> Self {
        Self {
            tool: tool.into(),
            ok: false,
            exit_code,
            stdout: String::new(),
            stderr: stderr.into(),
            diagnostics: Vec::new(),
            isolation: None,
            approval: None,
            snapshot: None,
            undo: None,
            mcp: None,
            route: None,
            timing_ms: 0,
            artifacts,
        }
    }

    pub fn with_diagnostics(mut self, diagnostics: Vec<Diagnostic>) -> Self {
        self.diagnostics = diagnostics;
        self
    }

    pub fn with_isolation(mut self, isolation: IsolationMetadata) -> Self {
        self.isolation = Some(isolation);
        self
    }

    pub fn with_approval(mut self, approval: ApprovalMetadata) -> Self {
        self.approval = Some(approval);
        self
    }

    pub fn with_snapshot(mut self, snapshot: SnapshotMetadata) -> Self {
        self.snapshot = Some(snapshot);
        self
    }

    pub fn with_undo(mut self, undo: UndoMetadata) -> Self {
        self.undo = Some(undo);
        self
    }

    pub fn with_mcp(mut self, mcp: McpMetadata) -> Self {
        self.mcp = Some(mcp);
        self
    }

    pub fn with_route(mut self, route: RouteMetadata) -> Self {
        self.route = Some(route);
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_envelope_ok_fields() {
        let env = ToolEnvelope::ok("read_file", "content", json!({"path": "a.txt"}));
        assert_eq!(env.tool, "read_file");
        assert!(env.ok);
        assert_eq!(env.exit_code, 0);
        assert_eq!(env.stdout, "content");
        assert!(env.stderr.is_empty());
        assert!(env.diagnostics.is_empty());
        assert!(env.isolation.is_none());
        assert!(env.approval.is_none());
        assert!(env.snapshot.is_none());
        assert!(env.undo.is_none());
        assert!(env.mcp.is_none());
        // timing_ms defaults to 0; dispatch_tool overwrites it
        assert_eq!(env.timing_ms, 0);
        assert_eq!(env.artifacts, json!({"path": "a.txt"}));
    }

    #[test]
    fn test_envelope_err_fields() {
        let env = ToolEnvelope::err("exec", 1, "failed", json!(null));
        assert_eq!(env.tool, "exec");
        assert!(!env.ok);
        assert_eq!(env.exit_code, 1);
        assert!(env.stdout.is_empty());
        assert_eq!(env.stderr, "failed");
        assert!(env.diagnostics.is_empty());
        assert!(env.isolation.is_none());
        assert!(env.approval.is_none());
        assert!(env.snapshot.is_none());
        assert!(env.undo.is_none());
        assert!(env.mcp.is_none());
        assert_eq!(env.timing_ms, 0);
    }

    #[test]
    fn test_envelope_ok_serializes() {
        let env = ToolEnvelope::ok("health", "ok", json!({}));
        let json_str = serde_json::to_string(&env).unwrap();
        let val: Value = serde_json::from_str(&json_str).unwrap();
        assert_eq!(val["tool"], "health");
        assert_eq!(val["ok"], true);
        assert_eq!(val["exit_code"], 0);
        assert_eq!(val["diagnostics"].as_array().map_or(0, std::vec::Vec::len), 0);
    }

    #[test]
    fn test_envelope_with_diagnostics() {
        let env =
            ToolEnvelope::err("exec", 1, "fail", json!({})).with_diagnostics(vec![Diagnostic {
                file: "src/main.rs".to_string(),
                line: Some(10),
                column: Some(5),
                code: Some("E0432".to_string()),
                fingerprint: Some("abcd1234".to_string()),
                message: "unresolved import".to_string(),
            }]);
        assert_eq!(env.diagnostics.len(), 1);
        assert_eq!(env.diagnostics[0].code.as_deref(), Some("E0432"));
    }

    #[test]
    fn test_envelope_with_isolation_and_approval() {
        let env = ToolEnvelope::ok("exec", "ok", json!({}))
            .with_isolation(IsolationMetadata {
                backend: "safe_fallback".to_string(),
                degraded: true,
                reason: Some("firecracker_unavailable".to_string()),
                policy_constraints: vec!["network=deny".to_string()],
            })
            .with_approval(ApprovalMetadata {
                required: true,
                status: "approved".to_string(),
                operation_class: "state_modifying".to_string(),
                challenge_id: Some("approval:apply_patch".to_string()),
                reason: None,
            })
            .with_snapshot(SnapshotMetadata {
                snapshot_id: "snap-1".to_string(),
                created: true,
                operation_class: "apply_patch".to_string(),
                git_commit: "abc123".to_string(),
                non_git_workspace: false,
                reason: None,
                checkpoint: Some(CheckpointPointer {
                    thread_id: "thread-1".to_string(),
                    checkpoint_ns: "main".to_string(),
                    checkpoint_id: Some("cp-1".to_string()),
                    run_id: None,
                }),
            })
            .with_undo(UndoMetadata {
                requested_snapshot_id: Some("snap-1".to_string()),
                restored_snapshot_id: "snap-1".to_string(),
                filesystem_restored: true,
                checkpoint_restored: true,
                checkpoint: None,
                reason: None,
            })
            .with_mcp(McpMetadata {
                server_name: "test-server".to_string(),
                handshake_completed: true,
                outbound_redactions: RedactionMetadata {
                    total: 1,
                    paths: 1,
                    usernames: 0,
                    ip_addresses: 0,
                },
                inbound_redactions: RedactionMetadata::default(),
            });

        assert_eq!(env.isolation.as_ref().map(|x| x.backend.as_str()), Some("safe_fallback"));
        assert_eq!(env.approval.as_ref().map(|x| x.status.as_str()), Some("approved"));
        assert_eq!(env.snapshot.as_ref().map(|x| x.snapshot_id.as_str()), Some("snap-1"));
        assert_eq!(env.undo.as_ref().map(|x| x.restored_snapshot_id.as_str()), Some("snap-1"));
        assert_eq!(env.mcp.as_ref().map(|x| x.server_name.as_str()), Some("test-server"));
    }

    #[test]
    fn test_execute_request_deserializes() {
        let json_str = r#"{"tool": "read_file", "input": {"path": "README.md"}}"#;
        let req: ToolExecuteRequest = serde_json::from_str(json_str).unwrap();
        assert_eq!(req.tool, "read_file");
        assert_eq!(req.input["path"], "README.md");
    }

    #[test]
    fn test_execute_request_default_input() {
        let json_str = r#"{"tool": "health"}"#;
        let req: ToolExecuteRequest = serde_json::from_str(json_str).unwrap();
        assert_eq!(req.tool, "health");
        assert!(req.input.is_null());
    }

    #[test]
    fn test_batch_request_deserializes() {
        let json_str = r#"{"calls": [{"tool": "a"}, {"tool": "b", "input": {}}]}"#;
        let req: ToolBatchExecuteRequest = serde_json::from_str(json_str).unwrap();
        assert_eq!(req.calls.len(), 2);
        assert_eq!(req.calls[0].tool, "a");
        assert_eq!(req.calls[1].tool, "b");
    }

    #[test]
    fn test_batch_response_serializes() {
        let resp = ToolBatchExecuteResponse {
            results: vec![
                ToolEnvelope::ok("a", "ok", json!({})),
                ToolEnvelope::err("b", 1, "fail", json!(null)),
            ],
        };
        let json_str = serde_json::to_string(&resp).unwrap();
        let val: Value = serde_json::from_str(&json_str).unwrap();
        assert_eq!(val["results"].as_array().unwrap().len(), 2);
    }
}
