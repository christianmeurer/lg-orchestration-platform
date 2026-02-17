use axum::{
    extract::Request,
    http::{header, StatusCode},
    middleware::Next,
    response::Response,
};

use crate::config::RunnerConfig;

pub async fn require_api_key(
    axum::extract::State(cfg): axum::extract::State<RunnerConfig>,
    req: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    let Some(expected) = cfg.api_key.as_deref() else {
        return Ok(next.run(req).await);
    };
    let expected = expected.trim();
    if expected.is_empty() {
        return Ok(next.run(req).await);
    }

    let Some(auth) = req.headers().get(header::AUTHORIZATION) else {
        return Err(StatusCode::UNAUTHORIZED);
    };
    let Ok(auth) = auth.to_str() else {
        return Err(StatusCode::UNAUTHORIZED);
    };
    let Some(given) = auth.strip_prefix("Bearer ") else {
        return Err(StatusCode::UNAUTHORIZED);
    };
    let given = given.trim();
    if given != expected {
        return Err(StatusCode::UNAUTHORIZED);
    }
    Ok(next.run(req).await)
}

pub async fn rate_limit(
    axum::extract::State(cfg): axum::extract::State<RunnerConfig>,
    req: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    let mut bucket = cfg.rate_limiter.lock().await;
    if bucket.try_acquire() {
        drop(bucket);
        Ok(next.run(req).await)
    } else {
        tracing::warn!("rate_limit_exceeded");
        Err(StatusCode::TOO_MANY_REQUESTS)
    }
}
