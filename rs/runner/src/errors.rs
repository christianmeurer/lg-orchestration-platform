use axum::{http::StatusCode, response::IntoResponse, Json};

use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum ApiError {
    #[error("bad_request: {0}")]
    BadRequest(String),
    #[error("forbidden: {0}")]
    Forbidden(String),
    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

impl IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        let (status, msg) = match &self {
            ApiError::BadRequest(m) => (StatusCode::BAD_REQUEST, m.clone()),
            ApiError::Forbidden(m) => (StatusCode::FORBIDDEN, m.clone()),
            ApiError::Other(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
        };
        let body = json!({"ok": false, "error": msg});
        (status, Json(body)).into_response()
    }
}
