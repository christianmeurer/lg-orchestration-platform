use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::Sha256;

use crate::envelope::ApprovalMetadata;
use crate::errors::ApiError;

type HmacSha256 = Hmac<Sha256>;

/// Default token validity window in seconds (5 minutes).
pub const DEFAULT_TOKEN_TTL_SECS: u64 = 300;

static APPROVAL_SECRET: OnceLock<Vec<u8>> = OnceLock::new();

/// Returns the active approval secret.
///
/// Resolution order:
/// 1. `LG_RUNNER_APPROVAL_SECRET` environment variable (hex-encoded bytes or raw UTF-8).
/// 2. Process-lifetime random 32-byte secret generated once on first call.
///
/// A warning is emitted when falling back to the ephemeral random secret so that
/// operators know to set a fixed secret in production.
fn approval_secret() -> &'static [u8] {
    APPROVAL_SECRET.get_or_init(|| {
        match std::env::var("LG_RUNNER_APPROVAL_SECRET") {
            Ok(v) if !v.trim().is_empty() => v.into_bytes(),
            _ => {
                tracing::warn!(
                    "LG_RUNNER_APPROVAL_SECRET is not set; \
                     using a random ephemeral secret. \
                     Approval tokens will not survive process restarts. \
                     Set LG_RUNNER_APPROVAL_SECRET in production."
                );
                let random_bytes: [u8; 32] = rand::random();
                random_bytes.to_vec()
            }
        }
    })
}

/// Returns the previous approval secret used for rotation, if set.
///
/// When `LG_RUNNER_APPROVAL_SECRET_PREVIOUS` is present, tokens signed
/// with that secret are still accepted during the rotation window.
fn approval_secret_previous() -> Option<Vec<u8>> {
    std::env::var("LG_RUNNER_APPROVAL_SECRET_PREVIOUS")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(|v| v.into_bytes())
}

/// Returns the current Unix timestamp in seconds.
fn unix_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Constant-time byte-slice equality to prevent timing attacks during HMAC comparison.
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

/// Computes `HMAC-SHA256( "{challenge_id}|{iat}|{nonce}" , secret )` and returns
/// the lower-hex string representation.
#[must_use]
fn compute_hmac(challenge_id: &str, iat: u64, nonce: &str, secret: &[u8]) -> String {
    let message = format!("{challenge_id}|{iat}|{nonce}");
    let mut mac = HmacSha256::new_from_slice(secret)
        .expect("HMAC accepts keys of any length");
    mac.update(message.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// Generates a signed approval token for `challenge_id`.
///
/// Token format: `{challenge_id}.{iat}.{nonce}.{hmac_hex}`
///
/// - `iat`      — Unix timestamp (seconds) at generation time.
/// - `nonce`    — 16 random bytes encoded as a 32-character lowercase hex string.
/// - `hmac_hex` — HMAC-SHA256 over `"{challenge_id}|{iat}|{nonce}"` with the
///                server secret, hex-encoded.
///
/// Called by the orchestration layer or test helpers to mint a token that is
/// then submitted to the runner.  The Rust binary crate itself only *verifies*
/// tokens, so this function is dead from the `main` entrypoint; the annotation
/// suppresses the lint for non-test builds while keeping the symbol available.
#[must_use]
#[cfg_attr(not(test), allow(dead_code))]
pub fn generate_token(challenge_id: &str) -> String {
    let iat = unix_now();
    let nonce_bytes: [u8; 16] = rand::random();
    let nonce = hex::encode(nonce_bytes);
    let hmac_hex = compute_hmac(challenge_id, iat, &nonce, approval_secret());
    format!("{challenge_id}.{iat}.{nonce}.{hmac_hex}")
}

/// Verifies a token previously produced by [`generate_token`].
///
/// Returns `Ok(())` on success, or an `Err` string describing the failure reason.
fn verify_token(
    expected_challenge_id: &str,
    token: &str,
    ttl_secs: u64,
) -> Result<(), &'static str> {
    // Token parts: challenge_id, iat, nonce, hmac_hex
    // challenge_id itself may contain dots, so we split from the right for the
    // last three fixed-width fields and reconstruct challenge_id from the rest.
    let parts: Vec<&str> = token.splitn(4, '.').collect();
    if parts.len() != 4 {
        return Err("malformed_token");
    }

    // The first segment must be the challenge_id embedded in the token.
    // We compare it against the caller-supplied expected value first.
    let token_challenge_id = parts[0];
    let iat_str = parts[1];
    let nonce = parts[2];
    let token_hmac = parts[3];

    if token_challenge_id != expected_challenge_id {
        return Err("challenge_id_mismatch");
    }

    let iat: u64 = iat_str.parse().map_err(|_| "malformed_token")?;
    let now = unix_now();

    // Reject tokens issued in the future (clock skew tolerance: 0 s).
    if iat > now {
        return Err("token_not_yet_valid");
    }
    // Reject expired tokens.
    if now.saturating_sub(iat) > ttl_secs {
        return Err("token_expired");
    }

    // Verify against current secret first, then previous secret for rotation.
    let current_secret = approval_secret();
    let expected_hmac = compute_hmac(expected_challenge_id, iat, nonce, current_secret);
    if constant_time_eq(token_hmac.as_bytes(), expected_hmac.as_bytes()) {
        return Ok(());
    }

    if let Some(prev_secret) = approval_secret_previous() {
        let prev_hmac = compute_hmac(expected_challenge_id, iat, nonce, &prev_secret);
        if constant_time_eq(token_hmac.as_bytes(), prev_hmac.as_bytes()) {
            return Ok(());
        }
    }

    Err("token_mismatch")
}

#[derive(Debug, Clone, Deserialize)]
pub struct ApprovalTokenInput {
    pub challenge_id: String,
    pub token: String,
}

/// Enforces that a valid signed approval token is present for `challenge_id`.
///
/// Returns [`ApprovalMetadata`] with `status = "approved"` on success.
/// Returns [`ApiError::ApprovalRequired`] with a descriptive status on failure.
///
/// `ttl_secs` controls how long a token remains valid after issuance
/// (see [`DEFAULT_TOKEN_TTL_SECS`]).
pub fn require_approval(
    approval: Option<ApprovalTokenInput>,
    operation_class: &str,
    challenge_id: &str,
    ttl_secs: u64,
) -> Result<ApprovalMetadata, ApiError> {
    let Some(input) = approval else {
        return Err(ApiError::ApprovalRequired(ApprovalMetadata {
            required: true,
            status: "challenge_required".to_string(),
            operation_class: operation_class.to_string(),
            challenge_id: Some(challenge_id.to_string()),
            reason: Some("missing_approval_token".to_string()),
        }));
    };

    if input.challenge_id.trim() != challenge_id {
        return Err(ApiError::ApprovalRequired(ApprovalMetadata {
            required: true,
            status: "invalid_token".to_string(),
            operation_class: operation_class.to_string(),
            challenge_id: Some(challenge_id.to_string()),
            reason: Some("challenge_id_mismatch".to_string()),
        }));
    }

    match verify_token(challenge_id, input.token.trim(), ttl_secs) {
        Ok(()) => Ok(ApprovalMetadata {
            required: true,
            status: "approved".to_string(),
            operation_class: operation_class.to_string(),
            challenge_id: Some(challenge_id.to_string()),
            reason: None,
        }),
        Err(reason) => Err(ApiError::ApprovalRequired(ApprovalMetadata {
            required: true,
            status: "invalid_token".to_string(),
            operation_class: operation_class.to_string(),
            challenge_id: Some(challenge_id.to_string()),
            reason: Some(reason.to_string()),
        })),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ttl() -> u64 {
        DEFAULT_TOKEN_TTL_SECS
    }

    #[test]
    fn test_fresh_token_verifies() {
        let challenge_id = "approval:apply_patch";
        let token = generate_token(challenge_id);
        let result = require_approval(
            Some(ApprovalTokenInput {
                challenge_id: challenge_id.to_string(),
                token,
            }),
            "apply_patch",
            challenge_id,
            ttl(),
        );
        assert!(result.is_ok());
        assert_eq!(result.unwrap().status, "approved");
    }

    #[test]
    fn test_missing_approval_rejected() {
        let result = require_approval(None, "apply_patch", "approval:apply_patch", ttl());
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
        if let Err(ApiError::ApprovalRequired(m)) = result {
            assert_eq!(m.status, "challenge_required");
            assert_eq!(m.reason.as_deref(), Some("missing_approval_token"));
        }
    }

    #[test]
    fn test_tampered_challenge_id_rejected() {
        let challenge_id = "approval:apply_patch";
        let token = generate_token(challenge_id);
        // Present the correct token but claim a different challenge_id in the input.
        let result = require_approval(
            Some(ApprovalTokenInput {
                challenge_id: "approval:exec:state_modifying".to_string(),
                token,
            }),
            "apply_patch",
            challenge_id,
            ttl(),
        );
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
        if let Err(ApiError::ApprovalRequired(m)) = result {
            assert_eq!(m.status, "invalid_token");
        }
    }

    #[test]
    fn test_tampered_hmac_rejected() {
        let challenge_id = "approval:apply_patch";
        let token = generate_token(challenge_id);
        // Flip the last character of the HMAC.
        let mut tampered = token.clone();
        let last = tampered.pop().unwrap();
        let replacement = if last == 'a' { 'b' } else { 'a' };
        tampered.push(replacement);

        let result = require_approval(
            Some(ApprovalTokenInput {
                challenge_id: challenge_id.to_string(),
                token: tampered,
            }),
            "apply_patch",
            challenge_id,
            ttl(),
        );
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
        if let Err(ApiError::ApprovalRequired(m)) = result {
            assert_eq!(m.reason.as_deref(), Some("token_mismatch"));
        }
    }

    #[test]
    fn test_expired_token_rejected() {
        let challenge_id = "approval:apply_patch";
        // Manually construct a token with iat = 0 (always expired for any TTL > 0).
        let iat: u64 = 0;
        let nonce_bytes: [u8; 16] = rand::random();
        let nonce = hex::encode(nonce_bytes);
        let hmac_hex = compute_hmac(challenge_id, iat, &nonce, approval_secret());
        let token = format!("{challenge_id}.{iat}.{nonce}.{hmac_hex}");

        let result = require_approval(
            Some(ApprovalTokenInput {
                challenge_id: challenge_id.to_string(),
                token,
            }),
            "apply_patch",
            challenge_id,
            ttl(),
        );
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
        if let Err(ApiError::ApprovalRequired(m)) = result {
            assert_eq!(m.reason.as_deref(), Some("token_expired"));
        }
    }

    #[test]
    fn test_previous_secret_rotation_path() {
        // Simulate a token signed with an "old" secret stored in the previous-secret slot.
        let old_secret = b"old-test-secret-value-for-rotation";
        let challenge_id = "approval:apply_patch";
        let iat = unix_now();
        let nonce_bytes: [u8; 16] = rand::random();
        let nonce = hex::encode(nonce_bytes);
        let hmac_hex = compute_hmac(challenge_id, iat, &nonce, old_secret);
        let token = format!("{challenge_id}.{iat}.{nonce}.{hmac_hex}");

        // Point the previous-secret env var at the old secret value.
        std::env::set_var(
            "LG_RUNNER_APPROVAL_SECRET_PREVIOUS",
            std::str::from_utf8(old_secret).unwrap(),
        );

        let result = require_approval(
            Some(ApprovalTokenInput {
                challenge_id: challenge_id.to_string(),
                token,
            }),
            "apply_patch",
            challenge_id,
            ttl(),
        );

        std::env::remove_var("LG_RUNNER_APPROVAL_SECRET_PREVIOUS");

        // The token was signed with the previous secret, so it must be accepted.
        assert!(
            result.is_ok(),
            "expected rotation to succeed, got: {result:?}"
        );
        assert_eq!(result.unwrap().status, "approved");
    }

    #[test]
    fn test_malformed_token_rejected() {
        let challenge_id = "approval:apply_patch";
        let result = require_approval(
            Some(ApprovalTokenInput {
                challenge_id: challenge_id.to_string(),
                token: "not-a-valid-token".to_string(),
            }),
            "apply_patch",
            challenge_id,
            ttl(),
        );
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
    }
}
