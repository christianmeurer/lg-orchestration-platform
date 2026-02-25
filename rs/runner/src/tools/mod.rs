use std::time::Instant;

use serde_json::{json, Value};

use crate::config::RunnerConfig;
use crate::envelope::{ToolEnvelope, ToolExecuteRequest};
use crate::errors::ApiError;

mod exec;
mod fs;

pub async fn dispatch_tool(
    cfg: &RunnerConfig,
    req: ToolExecuteRequest,
) -> Result<ToolEnvelope, ApiError> {
    let started = Instant::now();
    let tool = req.tool.trim().to_string();
    let out = match tool.as_str() {
        "health" => Ok(ToolEnvelope::ok(
            "health",
            "ok",
            json!({}),
            started.elapsed().as_millis(),
        )),
        "read_file" => fs::read_file(cfg, req.input).await,
        "search_files" => fs::search_files(cfg, req.input).await,
        "list_files" => fs::list_files(cfg, req.input).await,
        "apply_patch" => fs::apply_patch(cfg, req.input).await,
        "exec" => exec::exec(cfg, req.input).await,
        other => Err(ApiError::BadRequest(format!("unknown tool: {other}"))),
    };
    match out {
        Ok(mut env) => {
            env.timing_ms = started.elapsed().as_millis();
            Ok(env)
        }
        Err(e) => {
            let mut env = ToolEnvelope::err(
                tool,
                1,
                e.to_string(),
                Value::Null,
                started.elapsed().as_millis(),
            );
            env.timing_ms = started.elapsed().as_millis();
            Ok(env)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::envelope::ToolExecuteRequest;

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
        };
        let result = dispatch_tool(&cfg, req).await;
        assert!(result.is_ok());
        let env = result.unwrap();
        assert!(env.ok);
        assert_eq!(env.stdout, "content");
    }

    #[tokio::test]
    async fn test_dispatch_sets_timing() {
        let (_td, cfg) = test_cfg();
        let req = ToolExecuteRequest {
            tool: "health".to_string(),
            input: json!({}),
        };
        let env = dispatch_tool(&cfg, req).await.unwrap();
        // timing_ms should be set (>= 0)
        assert!(env.timing_ms < 10_000); // sanity check: less than 10 seconds
    }

    #[tokio::test]
    async fn test_dispatch_trims_tool_name() {
        let (_td, cfg) = test_cfg();
        let req = ToolExecuteRequest {
            tool: "  health  ".to_string(),
            input: json!({}),
        };
        let env = dispatch_tool(&cfg, req).await.unwrap();
        assert_eq!(env.tool, "health");
        assert!(env.ok);
    }
}
