mod auth;
mod config;
mod envelope;
mod errors;
mod tools;

use axum::{routing::get, routing::post, Json, Router};
use clap::Parser;
use std::net::SocketAddr;
use tower_http::trace::TraceLayer;
use tracing::Level;
use tracing_subscriber::{fmt, EnvFilter};

use crate::config::RunnerConfig;
use crate::envelope::{
    ToolBatchExecuteRequest, ToolBatchExecuteResponse, ToolEnvelope, ToolExecuteRequest,
};
use crate::tools::dispatch_tool;

#[derive(Parser, Debug)]
struct Args {
    #[arg(long, default_value = "127.0.0.1:8088")]
    bind: String,
    #[arg(long, default_value = ".")]
    root_dir: String,
    #[arg(long, default_value = "dev")]
    profile: String,
    #[arg(long, default_value = "")]
    api_key: String,
    #[arg(long, default_value = "100")]
    rate_limit_rps: u64,
}

async fn healthz() -> &'static str {
    "ok"
}

async fn capabilities() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "tools": [
            "health",
            "read_file",
            "list_files",
            "apply_patch",
            "exec",
        ],
        "batch": true
    }))
}

async fn execute_tool(
    axum::extract::State(cfg): axum::extract::State<RunnerConfig>,
    Json(req): Json<ToolExecuteRequest>,
) -> Result<Json<ToolEnvelope>, crate::errors::ApiError> {
    let env = dispatch_tool(&cfg, req).await?;
    Ok(Json(env))
}

async fn batch_execute_tool(
    axum::extract::State(cfg): axum::extract::State<RunnerConfig>,
    Json(req): Json<ToolBatchExecuteRequest>,
) -> Result<Json<ToolBatchExecuteResponse>, crate::errors::ApiError> {
    let mut out: Vec<ToolEnvelope> = Vec::with_capacity(req.calls.len());
    for call in req.calls {
        out.push(dispatch_tool(&cfg, call).await?);
    }
    Ok(Json(ToolBatchExecuteResponse { results: out }))
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    fmt()
        .with_env_filter(
            EnvFilter::builder()
                .with_default_directive(Level::INFO.into())
                .from_env_lossy(),
        )
        .json()
        .init();

    let api_key = if args.api_key.trim().is_empty() {
        None
    } else {
        Some(args.api_key.trim().to_string())
    };

    let cfg = RunnerConfig::with_rate_limit(
        args.root_dir,
        Some(args.profile.as_str()),
        api_key,
        args.rate_limit_rps,
    )?;

    let protected = Router::new()
        .route("/v1/tools/execute", post(execute_tool))
        .route("/v1/tools/batch_execute", post(batch_execute_tool))
        .route_layer(axum::middleware::from_fn_with_state(
            cfg.clone(),
            crate::auth::require_api_key,
        ))
        .route_layer(axum::middleware::from_fn_with_state(
            cfg.clone(),
            crate::auth::rate_limit,
        ));

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/capabilities", get(capabilities))
        .merge(protected)
        .layer(TraceLayer::new_for_http())
        .with_state(cfg);

    let addr: SocketAddr = args.bind.parse()?;
    tracing::info!(%addr, rate_limit_rps = args.rate_limit_rps, "runner_listening");
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}
