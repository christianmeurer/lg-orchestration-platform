use gloo_net::http::{Request, RequestBuilder};
use leptos::prelude::*;
use serde::Deserialize;
use serde_json::json;

use super::types::*;

#[derive(Deserialize)]
struct RunsResponse {
    runs: Vec<RunSummary>,
}

// ---------------------------------------------------------------------------
// ApiConfig
// ---------------------------------------------------------------------------

#[derive(Clone)]
pub struct ApiConfig {
    pub base_url: String,
    pub token: RwSignal<Option<String>>,
}

impl ApiConfig {
    /// Read `lula_api_url` and `lula_token` from `localStorage`.
    pub fn new() -> Self {
        let (base_url, token) = web_sys::window()
            .and_then(|w| w.local_storage().ok().flatten())
            .map(|storage| {
                let url = storage
                    .get_item("lula_api_url")
                    .ok()
                    .flatten()
                    .unwrap_or_default();
                let tok = storage.get_item("lula_token").ok().flatten();
                (url, tok)
            })
            .unwrap_or_default();

        Self {
            base_url,
            token: RwSignal::new(token),
        }
    }

    /// Persist `base_url` and the current token value back to `localStorage`.
    pub fn save(&self) {
        if let Some(storage) = web_sys::window()
            .and_then(|w| w.local_storage().ok().flatten())
        {
            let _ = storage.set_item("lula_api_url", &self.base_url);
            match self.token.get_untracked() {
                Some(tok) => {
                    let _ = storage.set_item("lula_token", &tok);
                }
                None => {
                    let _ = storage.remove_item("lula_token");
                }
            }
        }
    }

    /// Build a `RequestBuilder` with the `Authorization: Bearer <token>` header
    /// when a token is available.
    fn build_request(&self, method: &str, path: &str) -> RequestBuilder {
        let url = format!("{}{}", self.base_url, path);
        let req: RequestBuilder = match method {
            "POST" => Request::post(&url),
            "PUT" => Request::put(&url),
            "DELETE" => Request::delete(&url),
            _ => Request::get(&url),
        };
        if let Some(tok) = self.token.get_untracked() {
            req.header("Authorization", &format!("Bearer {}", tok))
        } else {
            req
        }
    }
}

// ---------------------------------------------------------------------------
// REST helpers
// ---------------------------------------------------------------------------

/// GET /v1/runs
pub async fn fetch_runs(config: &ApiConfig) -> Result<Vec<RunSummary>, String> {
    let resp = config
        .build_request("GET", "/v1/runs")
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.ok() {
        return Err(format!("HTTP {}", resp.status()));
    }

    resp.json::<RunsResponse>()
        .await
        .map(|r| r.runs)
        .map_err(|e| format!("Parse error: {e}"))
}

/// GET /v1/runs/{id}
pub async fn fetch_run(config: &ApiConfig, run_id: &str) -> Result<RunDetail, String> {
    let resp = config
        .build_request("GET", &format!("/v1/runs/{}", run_id))
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.ok() {
        return Err(format!("HTTP {}", resp.status()));
    }

    resp.json::<RunDetail>().await.map_err(|e| e.to_string())
}

/// POST /v1/runs — body: `{"request": "..."}`
pub async fn submit_run(config: &ApiConfig, request: &str) -> Result<RunSummary, String> {
    let body = json!({ "request": request });
    let resp = config
        .build_request("POST", "/v1/runs")
        .header("Content-Type", "application/json")
        .body(body.to_string())
        .map_err(|e| e.to_string())?
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.ok() {
        return Err(format!("HTTP {}", resp.status()));
    }

    resp.json::<RunSummary>().await.map_err(|e| e.to_string())
}

/// POST /v1/runs/{id}/approve — body: `{"actor": "spa", "challenge_id": ...}`
pub async fn approve_run(
    config: &ApiConfig,
    run_id: &str,
    challenge_id: Option<String>,
) -> Result<(), String> {
    let body = ApproveRequest {
        actor: "spa".to_string(),
        challenge_id,
    };
    let body_str = serde_json::to_string(&body).map_err(|e| e.to_string())?;

    let resp = config
        .build_request("POST", &format!("/v1/runs/{}/approve", run_id))
        .header("Content-Type", "application/json")
        .body(body_str)
        .map_err(|e| e.to_string())?
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.ok() {
        return Err(format!("HTTP {}", resp.status()));
    }

    Ok(())
}

/// POST /v1/runs/{id}/reject
pub async fn reject_run(config: &ApiConfig, run_id: &str) -> Result<(), String> {
    let resp = config
        .build_request("POST", &format!("/v1/runs/{}/reject", run_id))
        .header("Content-Type", "application/json")
        .body("{}")
        .map_err(|e| e.to_string())?
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.ok() {
        return Err(format!("HTTP {}", resp.status()));
    }

    Ok(())
}
