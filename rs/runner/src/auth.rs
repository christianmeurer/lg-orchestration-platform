// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
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
        self.0.keys().map(|k| k.as_str()).collect()
    }
}

/// Extract the W3C `traceparent` header (and optional `tracestate`) from
/// the incoming request and store the extracted OTel context in request
/// extensions so that tower-http's TraceLayer can attach it when creating
/// the request span.
///
/// The context must NOT be attached via a guard here — the guard would be
/// dropped before the handler runs, losing the parent span link.  Instead,
/// we store the context in `req.extensions_mut()` and let downstream layers
/// or the handler attach it at the appropriate point.
fn propagate_trace_context(req: &mut Request) {
    let propagator = TraceContextPropagator::new();
    let parent_ctx = propagator.extract(&HeaderMapCarrier(req.headers()));
    // Store the extracted context in request extensions so it can be used
    // by the TraceLayer or handler to attach the parent span context.
    req.extensions_mut().insert(parent_ctx);
    // Record the traceparent value into the current tracing span fields so
    // they are visible in structured logs.
    use tracing::Span;
    if let Some(tp) = req.headers().get("traceparent").and_then(|v| v.to_str().ok()) {
        Span::current().record("traceparent", tp);
    }
}

pub async fn require_api_key(
    axum::extract::State(cfg): axum::extract::State<RunnerConfig>,
    mut req: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    let request_id = req
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();

    propagate_trace_context(&mut req);

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
        let mut req = Request::builder().body(axum::body::Body::empty()).unwrap();
        propagate_trace_context(&mut req);
    }

    #[test]
    fn test_propagate_trace_context_valid_header() {
        let mut req = Request::builder()
            .header("traceparent", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
            .body(axum::body::Body::empty())
            .unwrap();
        // Should not panic with a well-formed traceparent.
        propagate_trace_context(&mut req);
        // Verify the OTel context was stored in extensions.
        assert!(req.extensions().get::<opentelemetry::Context>().is_some());
    }
}
