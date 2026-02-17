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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_bad_request_display() {
        let e = ApiError::BadRequest("invalid input".to_string());
        assert_eq!(e.to_string(), "bad_request: invalid input");
    }

    #[test]
    fn test_forbidden_display() {
        let e = ApiError::Forbidden("access denied".to_string());
        assert_eq!(e.to_string(), "forbidden: access denied");
    }

    #[test]
    fn test_other_display() {
        let e = ApiError::Other(anyhow::anyhow!("something broke"));
        assert_eq!(e.to_string(), "something broke");
    }

    #[test]
    fn test_bad_request_response_status() {
        let e = ApiError::BadRequest("test".to_string());
        let resp = e.into_response();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[test]
    fn test_forbidden_response_status() {
        let e = ApiError::Forbidden("test".to_string());
        let resp = e.into_response();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    #[test]
    fn test_other_response_status() {
        let e = ApiError::Other(anyhow::anyhow!("err"));
        let resp = e.into_response();
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }

    #[test]
    fn test_from_anyhow() {
        let anyhow_err = anyhow::anyhow!("test error");
        let api_err: ApiError = anyhow_err.into();
        assert!(matches!(api_err, ApiError::Other(_)));
    }
}
