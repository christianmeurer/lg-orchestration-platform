mod approval;
mod auth;
mod config;
mod diagnostics;
mod envelope;
mod errors;
mod indexing;
mod invariants;
mod sandbox;
mod snapshots;
mod tools;

use axum::{routing::get, routing::post, Json, Router};
use axum::http::header::CONTENT_TYPE;
use axum::response::IntoResponse;
use clap::Parser;
use std::net::SocketAddr;
use std::sync::Arc;
use tower_http::trace::TraceLayer;
use tracing::Level;
use tracing_subscriber::{fmt, layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

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

const PROMETHEUS_CONTENT_TYPE: &str = "text/plain; version=0.0.4; charset=utf-8";

async fn healthz() -> &'static str {
    "ok"
}

async fn metrics_handler(
    axum::extract::Extension(handle): axum::extract::Extension<
        Option<Arc<metrics_exporter_prometheus::PrometheusHandle>>,
    >,
) -> impl IntoResponse {
    let body = handle
        .as_ref()
        .map(|h| h.render())
        .unwrap_or_default();
    ([(CONTENT_TYPE, PROMETHEUS_CONTENT_TYPE)], body)
}

async fn capabilities() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "tools": [
            "health",
            "read_file",
            "search_files",
            "search_codebase",
            "ast_index_summary",
            "list_files",
            "apply_patch",
            "exec",
            "undo",
            "mcp_discover",
            "mcp_execute",
            "mcp_resources_list",
            "mcp_resource_read",
            "mcp_prompts_list",
            "mcp_prompt_get"
        ],
        "batch": true,
        "mcp_enabled": true,
        "mcp": {
            "protocol": "json-rpc-2.0",
            "methods": ["initialize", "tools/list", "tools/call", "resources/list", "resources/read", "prompts/list", "prompts/get"],
            "redaction": {
                "enabled": true,
                "fields": ["paths", "usernames", "ip_addresses"]
            }
        }
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
    let cfg = Arc::new(cfg);
    let n = req.calls.len();
    let mut set: tokio::task::JoinSet<(usize, Result<ToolEnvelope, crate::errors::ApiError>)> =
        tokio::task::JoinSet::new();
    for (idx, call) in req.calls.into_iter().enumerate() {
        let cfg = Arc::clone(&cfg);
        set.spawn(async move { (idx, dispatch_tool(&cfg, call).await) });
    }
    let mut out: Vec<Option<ToolEnvelope>> = (0..n).map(|_| None).collect();
    while let Some(join_result) = set.join_next().await {
        let (idx, result) = join_result.map_err(|e| {
            crate::errors::ApiError::Other(anyhow::anyhow!("batch task panicked: {e}"))
        })?;
        out[idx] = Some(result?);
    }
    let results = out.into_iter().map(Option::unwrap).collect();
    Ok(Json(ToolBatchExecuteResponse { results }))
}

/// Attempt to initialise an OTLP tracer provider.
///
/// Returns `Some(tracer)` on success or `None` if the exporter fails to
/// build (e.g. the endpoint is unreachable at startup).  The caller logs a
/// warning in the `None` case and continues without OTLP export.
fn try_init_otlp(
    endpoint: &str,
    service_name: &str,
) -> Option<opentelemetry_sdk::trace::Tracer> {
    use opentelemetry::KeyValue;
    use opentelemetry_otlp::WithExportConfig;
    use opentelemetry_sdk::runtime;

    let resource = opentelemetry_sdk::Resource::new(vec![
        KeyValue::new(
            opentelemetry_semantic_conventions::resource::SERVICE_NAME,
            service_name.to_string(),
        ),
    ]);

    let result = opentelemetry_otlp::new_pipeline()
        .tracing()
        .with_exporter(
            opentelemetry_otlp::new_exporter()
                .tonic()
                .with_endpoint(endpoint),
        )
        .with_trace_config(opentelemetry_sdk::trace::Config::default().with_resource(resource))
        .install_batch(runtime::Tokio);

    match result {
        Ok(tracer) => Some(tracer),
        Err(e) => {
            // Cannot use tracing here since the subscriber isn't initialised yet.
            eprintln!(
                "{{\"level\":\"warn\",\"msg\":\"otlp_init_failed\",\"error\":\"{e}\",\"endpoint\":\"{endpoint}\"}}"
            );
            None
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();

    // ------------------------------------------------------------------
    // Prometheus metrics recorder
    // ------------------------------------------------------------------
    let prometheus_handle: Option<Arc<metrics_exporter_prometheus::PrometheusHandle>> =
        match metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder() {
            Ok(h) => {
                metrics::describe_counter!(
                    "runner_tool_calls_total",
                    "Total tool calls dispatched by the runner"
                );
                metrics::describe_histogram!(
                    "runner_tool_duration_seconds",
                    "Wall-clock duration of individual tool calls in seconds"
                );
                metrics::describe_counter!(
                    "runner_sandbox_tier",
                    "Sandbox backend selections by tier"
                );
                Some(Arc::new(h))
            }
            Err(e) => {
                eprintln!(
                    "{{\"level\":\"warn\",\"msg\":\"prometheus_recorder_init_failed\",\"error\":\"{e}\"}}"
                );
                None
            }
        };

    // ------------------------------------------------------------------
    // Subscriber: JSON formatter + optional OTel layer
    // ------------------------------------------------------------------
    let otlp_endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT")
        .unwrap_or_else(|_| "http://localhost:4317".to_string());

    let maybe_tracer = try_init_otlp(&otlp_endpoint, "lula-runner");

    let filter = EnvFilter::builder()
        .with_default_directive(Level::INFO.into())
        .from_env_lossy();

    let otel_layer = maybe_tracer.map(|tracer| {
        tracing_opentelemetry::layer().with_tracer(tracer)
    });

    tracing_subscriber::registry()
        .with(filter)
        .with(fmt::layer().json())
        .with(otel_layer)
        .init();

    // Install W3C TraceContext propagator globally so that `traceparent`
    // headers injected by the Python orchestrator are extracted correctly.
    opentelemetry::global::set_text_map_propagator(
        opentelemetry_sdk::propagation::TraceContextPropagator::new(),
    );

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
    cfg.indexing.ensure_started();

    // ------------------------------------------------------------------
    // MCP connection pool — periodic cleanup of stale entries
    // ------------------------------------------------------------------
    {
        let pool_ref = cfg.mcp_pool.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(tokio::time::Duration::from_secs(60));
            loop {
                interval.tick().await;
                crate::tools::mcp::purge_mcp_pool(&pool_ref).await;
            }
        });
    }

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
        // /metrics is outside the authenticated layer — publicly accessible for K8s scraping
        .route("/metrics", get(metrics_handler))
        .merge(protected)
        .layer(axum::Extension(prometheus_handle))
        .layer(TraceLayer::new_for_http())
        .with_state(cfg);

    let addr: SocketAddr = args.bind.parse()?;
    tracing::info!(%addr, rate_limit_rps = args.rate_limit_rps, "runner_listening");
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    opentelemetry::global::shutdown_tracer_provider();
    Ok(())
}
