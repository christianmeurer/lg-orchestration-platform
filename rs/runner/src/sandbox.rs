use std::env;
use std::path::PathBuf;

use crate::envelope::IsolationMetadata;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxBackend {
    MicroVmEphemeral,
    SafeFallback,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxPreference {
    Auto,
    PreferMicroVm,
    SafeFallbackOnly,
}

#[derive(Debug, Clone)]
pub struct MicroVmSettings {
    pub enabled: bool,
    pub firecracker_bin: Option<PathBuf>,
    pub kernel_image: Option<PathBuf>,
    pub rootfs_image: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct SandboxPolicy {
    pub preference: SandboxPreference,
    pub microvm: MicroVmSettings,
}

#[derive(Debug, Clone)]
pub struct SandboxResolution {
    pub backend: SandboxBackend,
    pub degraded: bool,
    pub reason: Option<String>,
    pub policy_constraints: Vec<String>,
}

impl SandboxResolution {
    pub fn to_isolation_metadata(&self) -> IsolationMetadata {
        let backend = match self.backend {
            SandboxBackend::MicroVmEphemeral => "microvm_ephemeral",
            SandboxBackend::SafeFallback => "safe_fallback",
        };
        IsolationMetadata {
            backend: backend.to_string(),
            degraded: self.degraded,
            reason: self.reason.clone(),
            policy_constraints: self.policy_constraints.clone(),
        }
    }
}

impl SandboxPolicy {
    pub fn from_env() -> Self {
        let preference = match env::var("LG_RUNNER_SANDBOX_BACKEND") {
            Ok(v) => {
                let normalized = v.trim().to_ascii_lowercase();
                match normalized.as_str() {
                    "microvm" | "prefer_microvm" => SandboxPreference::PreferMicroVm,
                    "safe" | "safe_fallback" | "fallback" => SandboxPreference::SafeFallbackOnly,
                    _ => SandboxPreference::Auto,
                }
            }
            Err(_) => SandboxPreference::Auto,
        };

        let enabled = env::var("LG_RUNNER_MICROVM_ENABLED")
            .ok()
            .map(|v| parse_bool(&v))
            .unwrap_or(false);
        let firecracker_bin = env::var("LG_RUNNER_FIRECRACKER_BIN")
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .map(PathBuf::from);
        let kernel_image = env::var("LG_RUNNER_MICROVM_KERNEL_IMAGE")
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .map(PathBuf::from);
        let rootfs_image = env::var("LG_RUNNER_MICROVM_ROOTFS_IMAGE")
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .map(PathBuf::from);

        Self {
            preference,
            microvm: MicroVmSettings {
                enabled,
                firecracker_bin,
                kernel_image,
                rootfs_image,
            },
        }
    }

    pub fn resolve_backend(&self) -> SandboxResolution {
        let mut policy_constraints = vec![
            "command_allowlist".to_string(),
            "cwd_scoped_to_runner_root".to_string(),
            "stdin_null_noninteractive".to_string(),
            "timeout_enforced".to_string(),
        ];

        if self.preference == SandboxPreference::SafeFallbackOnly {
            policy_constraints.push("backend=safe_fallback_explicit".to_string());
            return SandboxResolution {
                backend: SandboxBackend::SafeFallback,
                degraded: false,
                reason: None,
                policy_constraints,
            };
        }

        if let Some(reason) = self.microvm_unavailable_reason() {
            policy_constraints.push("backend=safe_fallback_degraded".to_string());
            return SandboxResolution {
                backend: SandboxBackend::SafeFallback,
                degraded: true,
                reason: Some(reason),
                policy_constraints,
            };
        }

        policy_constraints.push("backend=microvm_ephemeral_firecracker_style".to_string());
        SandboxResolution {
            backend: SandboxBackend::MicroVmEphemeral,
            degraded: false,
            reason: None,
            policy_constraints,
        }
    }

    fn microvm_unavailable_reason(&self) -> Option<String> {
        if !self.microvm.enabled {
            return Some("microvm_disabled".to_string());
        }
        if cfg!(target_os = "windows") {
            return Some("microvm_requires_linux".to_string());
        }

        let Some(firecracker) = self.microvm.firecracker_bin.as_ref() else {
            return Some("firecracker_binary_not_configured".to_string());
        };
        if !firecracker.exists() {
            return Some("firecracker_binary_not_found".to_string());
        }

        let Some(kernel) = self.microvm.kernel_image.as_ref() else {
            return Some("microvm_kernel_image_not_configured".to_string());
        };
        if !kernel.exists() {
            return Some("microvm_kernel_image_not_found".to_string());
        }

        let Some(rootfs) = self.microvm.rootfs_image.as_ref() else {
            return Some("microvm_rootfs_image_not_configured".to_string());
        };
        if !rootfs.exists() {
            return Some("microvm_rootfs_image_not_found".to_string());
        }

        None
    }
}

fn parse_bool(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_safe_fallback_explicit_policy() {
        let policy = SandboxPolicy {
            preference: SandboxPreference::SafeFallbackOnly,
            microvm: MicroVmSettings {
                enabled: true,
                firecracker_bin: None,
                kernel_image: None,
                rootfs_image: None,
            },
        };

        let resolution = policy.resolve_backend();
        assert_eq!(resolution.backend, SandboxBackend::SafeFallback);
        assert!(!resolution.degraded);
        assert!(resolution.reason.is_none());
    }

    #[test]
    fn test_microvm_preferred_degrades_with_reason() {
        let policy = SandboxPolicy {
            preference: SandboxPreference::PreferMicroVm,
            microvm: MicroVmSettings {
                enabled: true,
                firecracker_bin: None,
                kernel_image: None,
                rootfs_image: None,
            },
        };

        let resolution = policy.resolve_backend();
        assert_eq!(resolution.backend, SandboxBackend::SafeFallback);
        assert!(resolution.degraded);
        assert!(resolution.reason.is_some());
    }
}
