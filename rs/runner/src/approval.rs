use serde::Deserialize;

use crate::envelope::ApprovalMetadata;
use crate::errors::ApiError;

#[derive(Debug, Clone, Deserialize)]
pub struct ApprovalTokenInput {
    pub challenge_id: String,
    pub token: String,
}

pub fn require_approval(
    approval: Option<ApprovalTokenInput>,
    operation_class: &str,
    challenge_id: &str,
) -> Result<ApprovalMetadata, ApiError> {
    match approval {
        None => Err(ApiError::ApprovalRequired(ApprovalMetadata {
            required: true,
            status: "challenge_required".to_string(),
            operation_class: operation_class.to_string(),
            challenge_id: Some(challenge_id.to_string()),
            reason: Some("missing_approval_token".to_string()),
        })),
        Some(token) => {
            if token.challenge_id.trim() != challenge_id {
                return Err(ApiError::ApprovalRequired(ApprovalMetadata {
                    required: true,
                    status: "invalid_token".to_string(),
                    operation_class: operation_class.to_string(),
                    challenge_id: Some(challenge_id.to_string()),
                    reason: Some("challenge_id_mismatch".to_string()),
                }));
            }

            let expected = expected_token_for_challenge(challenge_id);
            if token.token.trim() != expected {
                return Err(ApiError::ApprovalRequired(ApprovalMetadata {
                    required: true,
                    status: "invalid_token".to_string(),
                    operation_class: operation_class.to_string(),
                    challenge_id: Some(challenge_id.to_string()),
                    reason: Some("token_mismatch".to_string()),
                }));
            }

            Ok(ApprovalMetadata {
                required: true,
                status: "approved".to_string(),
                operation_class: operation_class.to_string(),
                challenge_id: Some(challenge_id.to_string()),
                reason: None,
            })
        }
    }
}

pub fn expected_token_for_challenge(challenge_id: &str) -> String {
    format!("approve:{challenge_id}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_require_approval_missing_rejected() {
        let result = require_approval(None, "apply_patch", "approval:apply_patch");
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
    }

    #[test]
    fn test_require_approval_invalid_token_rejected() {
        let result = require_approval(
            Some(ApprovalTokenInput {
                challenge_id: "approval:apply_patch".to_string(),
                token: "approve:other".to_string(),
            }),
            "apply_patch",
            "approval:apply_patch",
        );
        assert!(matches!(result, Err(ApiError::ApprovalRequired(_))));
    }

    #[test]
    fn test_require_approval_valid_token_accepted() {
        let challenge_id = "approval:apply_patch";
        let result = require_approval(
            Some(ApprovalTokenInput {
                challenge_id: challenge_id.to_string(),
                token: expected_token_for_challenge(challenge_id),
            }),
            "apply_patch",
            challenge_id,
        );
        assert!(result.is_ok());
        let metadata = result.unwrap();
        assert_eq!(metadata.status, "approved");
    }
}
