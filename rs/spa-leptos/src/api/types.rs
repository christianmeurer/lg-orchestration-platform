use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub enum RunStatus {
    #[serde(rename = "queued")]
    Queued,
    #[serde(rename = "running")]
    Running,
    #[serde(rename = "completed")]
    Completed,
    #[serde(rename = "failed")]
    Failed,
    #[serde(rename = "suspended")]
    Suspended,
}

impl RunStatus {
    pub fn is_terminal(&self) -> bool {
        matches!(self, Self::Completed | Self::Failed)
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct RunSummary {
    pub run_id: String,
    pub request: String,
    pub status: RunStatus,
    #[serde(default)]
    pub pending_approval: bool,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub elapsed_ms: Option<u64>,
    #[serde(default)]
    pub current_node: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RunDetail {
    pub run_id: String,
    pub request: String,
    pub status: RunStatus,
    #[serde(default)]
    pub pending_approval: bool,
    #[serde(default)]
    pub intent: Option<String>,
    #[serde(default)]
    pub plan_steps: Vec<PlanStep>,
    #[serde(default)]
    pub current_node: Option<String>,
    #[serde(default)]
    pub final_output: Option<String>,
    #[serde(default)]
    pub verifier_ok: Option<bool>,
    #[serde(default)]
    pub approval_history: Vec<ApprovalEntry>,
    #[serde(default)]
    pub trace_events: Vec<TraceEvent>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PlanStep {
    pub id: String,
    pub description: String,
    #[serde(default)]
    pub tools: Vec<PlanTool>,
    #[serde(default)]
    pub expected_outcome: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PlanTool {
    pub tool: String,
    #[serde(default)]
    pub input: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type")]
pub enum SseEvent {
    #[serde(rename = "done")]
    Done,
    #[serde(rename = "tool_stdout")]
    ToolStdout { tool: String, line: String },
    #[serde(rename = "final_output")]
    FinalOutput { text: String },
    #[serde(rename = "approval_requested")]
    ApprovalRequested {
        #[serde(default)]
        challenge_id: Option<String>,
        #[serde(default)]
        summary: Option<String>,
        #[serde(default)]
        operation_class: Option<String>,
    },
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TraceEvent {
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub ts_ms: Option<u64>,
    #[serde(default)]
    pub node: Option<String>,
    #[serde(default)]
    pub data: Option<serde_json::Value>,
}

impl TraceEvent {
    /// Extract the node name — may be top-level `node` or nested in `data.name`
    pub fn node_name(&self) -> String {
        if let Some(ref n) = self.node {
            if !n.is_empty() {
                return n.clone();
            }
        }
        // Fall back to data.name (actual server format)
        self.data
            .as_ref()
            .and_then(|d| d.get("name"))
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string()
    }

    /// Extract phase from data.phase ("start"/"end")
    pub fn phase(&self) -> Option<String> {
        self.data
            .as_ref()
            .and_then(|d| d.get("phase"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct ApprovalEntry {
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default)]
    pub actor: Option<String>,
    #[serde(default)]
    pub challenge_id: Option<String>,
    #[serde(default)]
    pub ts: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ApprovalRequest {
    pub run_id: String,
    pub challenge_id: Option<String>,
    pub summary: Option<String>,
    pub operation_class: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct SubmitRunRequest {
    pub request: String,
}

#[derive(Debug, Serialize)]
pub struct ApproveRequest {
    pub actor: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub challenge_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct VerifierCheck {
    pub name: String,
    pub ok: bool,
    #[serde(default)]
    pub tool: Option<String>,
    #[serde(default)]
    pub exit_code: Option<i32>,
    #[serde(default)]
    pub summary: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct VerifierReport {
    pub ok: bool,
    #[serde(default)]
    pub acceptance_ok: Option<bool>,
    #[serde(default)]
    pub checks: Vec<VerifierCheck>,
    #[serde(default)]
    pub halt_reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ToolEnvelope {
    pub tool: String,
    pub ok: bool,
    #[serde(default)]
    pub exit_code: Option<i32>,
    #[serde(default)]
    pub stdout: Option<String>,
    #[serde(default)]
    pub stderr: Option<String>,
    #[serde(default)]
    pub timing_ms: Option<u64>,
    #[serde(default)]
    pub diagnostics: Vec<Diagnostic>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Diagnostic {
    pub file: String,
    #[serde(default)]
    pub line: Option<u32>,
    #[serde(default)]
    pub message: Option<String>,
    #[serde(default)]
    pub code: Option<String>,
}
