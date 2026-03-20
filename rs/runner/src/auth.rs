use axum::{
    extract::Request,
    http::{header, StatusCode},
    middleware::Next,
    response::Response,
};
use opentelemetry::propagation::TextMapPropagator;
use opentelemetry_sdk::propagation::TraceContextPropagator;

use crate::config::RunnerConfig;

/// Constant-time byte-slice equality check.
/// Returns true iff a and b have the same length and same bytes.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// A thin adapter so that `axum::http::HeaderMap` can be used as a
/// `TextMapPropagator` carrier.
struct HeaderMapCarrier<'a>(&'a axum::http::HeaderMap);

impl opentelemetry::propagation::Extractor for HeaderMapCarrier<'_> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(|v| v.to_str().ok())
    }

    fn keys(&self) -> Vec<&str> {
        self.0
            .keys()
            .map(|k| k.as_str())
            .collect()
    }
}

/// Extract the W3C `traceparent` header (and optional `tracestate`) from
/// the incoming request and attach the remote span context to the current
/// `tracing` span via the OTel layer.
fn propagate_trace_context(headers: &axum::http::HeaderMap) {
    let propagator = TraceContextPropagator::new();
    let parent_ctx = propagator.extract(&HeaderMapCarrier(headers));
    // Attach the extracted context so the tracing-opentelemetry layer can
    // pick it up when a new span is created for this request.
    let _guard = opentelemetry::Context::attach(parent_ctx);
    // The guard is intentionally dropped here; the actual span creation
    // happens via tower-http's TraceLayer which runs after this middleware.
    // We record the trace/span IDs into the current tracing span fields so
    // they are visible in structured logs.
    use tracing::Span;
    if let Some(tp) = headers.get("traceparent").and_then(|v| v.to_str().ok()) {
        Span::current().record("traceparent", tp);
    }
}

pub async fn require_api_key(
    axum::extract::State(cfg): axum::extract::State<RunnerConfig>,
    req: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    let request_id = req
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();

    propagate_trace_context(req.headers());

    let Some(expected) = cfg.api_key.as_deref() else {
        return Ok(next.run(req).await);
    };
    let expected = expected.trim();
    if expected.is_empty() {
        return Ok(next.run(req).await);
    }

    let Some(auth) = req.headers().get(header::AUTHORIZATION) else {
        tracing::warn!(request_id = %request_id, "runner_auth_missing_authorization");
        return Err(StatusCode::UNAUTHORIZED);
    };
    let Ok(auth) = auth.to_str() else {
        tracing::warn!(request_id = %request_id, "runner_auth_invalid_authorization_header");
        return Err(StatusCode::UNAUTHORIZED);
    };
    let Some(given) = auth.strip_prefix("Bearer ") else {
        tracing::warn!(request_id = %request_id, "runner_auth_missing_bearer_prefix");
        return Err(StatusCode::UNAUTHORIZED);
    };
    let given = given.trim();
    if !constant_time_eq(given.as_bytes(), expected.as_bytes()) {
        tracing::warn!(request_id = %request_id, "runner_auth_invalid_token");
        return Err(StatusCode::UNAUTHORIZED);
    }
    Ok(next.run(req).await)
}

pub async fn rate_limit(
    axum::extract::State(cfg): axum::extract::State<RunnerConfig>,
    req: Request,
    next: Next,
) -> Result<Response, crate::errors::ApiError> {
    let request_id = req
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();
    let mut bucket = cfg.rate_limiter.lock().await;
    if bucket.try_acquire() {
        drop(bucket);
        Ok(next.run(req).await)
    } else {
        tracing::warn!(request_id = %request_id, "rate_limit_exceeded");
        Err(crate::errors::ApiError::RateLimitExceeded)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_constant_time_eq_equal() {
        assert!(constant_time_eq(b"hello", b"hello"));
    }

    #[test]
    fn test_constant_time_eq_different_value() {
        assert!(!constant_time_eq(b"hello", b"world"));
    }

    #[test]
    fn test_constant_time_eq_different_length() {
        assert!(!constant_time_eq(b"hello", b"hell"));
    }

    #[test]
    fn test_constant_time_eq_empty() {
        assert!(constant_time_eq(b"", b""));
    }

    #[test]
    fn test_propagate_trace_context_no_header() {
        // Should not panic when no traceparent header is present.
        let headers = axum::http::HeaderMap::new();
        propagate_trace_context(&headers);
    }

    #[test]
    fn test_propagate_trace_context_valid_header() {
        let mut headers = axum::http::HeaderMap::new();
        headers.insert(
            "traceparent",
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
                .parse()
                .unwrap(),
        );
        // Should not panic with a well-formed traceparent.
        propagate_trace_context(&headers);
    }
}
