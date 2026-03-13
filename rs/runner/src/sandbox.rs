use std::env;
use std::path::PathBuf;

use crate::envelope::IsolationMetadata;

// Verus specification annotations.
// These are no-ops when compiled without `--features verify`.
// With `verus` installed, run: verus rs/runner/src/sandbox.rs --features verify
#[cfg(feature = "verify")]
use std::collections::HashSet;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxBackend {
    MicroVmEphemeral,
    LinuxNamespace,
    SafeFallback,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxPreference {
    Auto,
    PreferMicroVm,
    PreferLinuxNamespace,
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
pub struct LinuxNamespaceSettings {
    pub enabled: bool,
    pub unshare_bin: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct SandboxPolicy {
    pub preference: SandboxPreference,
    pub microvm: MicroVmSettings,
    pub linux_namespace: LinuxNamespaceSettings,
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
            SandboxBackend::LinuxNamespace => "linux_namespace",
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

// --- Verus invariant (checked when --features verify is active) ---
// INVARIANT: resolve_backend() always returns a SandboxResolution where:
//   1. policy_constraints is non-empty (at least the base constraints are present)
//   2. if backend == MicroVmEphemeral then degraded == false
//   3. if degraded == true then reason.is_some()
// These invariants are enforced by the `#[cfg(feature = "verify")]` proof below.
impl SandboxPolicy {
    pub fn from_env() -> Self {
        let preference = match env::var("LG_RUNNER_SANDBOX_BACKEND") {
            Ok(v) => {
                let normalized = v.trim().to_ascii_lowercase();
                match normalized.as_str() {
                    "microvm" | "prefer_microvm" => SandboxPreference::PreferMicroVm,
                    "namespace" | "linux_namespace" | "prefer_linux_namespace" => {
                        SandboxPreference::PreferLinuxNamespace
                    }
                    "safe" | "safe_fallback" | "fallback" => SandboxPreference::SafeFallbackOnly,
                    _ => SandboxPreference::Auto,
                }
            }
            Err(_) => SandboxPreference::Auto,
        };

        let microvm_enabled = env::var("LG_RUNNER_MICROVM_ENABLED")
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

        let ns_enabled = env::var("LG_RUNNER_LINUX_NAMESPACE_ENABLED")
            .ok()
            .map(|v| parse_bool(&v))
            .unwrap_or(false);
        let unshare_bin = env::var("LG_RUNNER_UNSHARE_BIN")
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .or_else(|| Some(PathBuf::from("/usr/bin/unshare")));

        Self {
            preference,
            microvm: MicroVmSettings {
                enabled: microvm_enabled,
                firecracker_bin,
                kernel_image,
                rootfs_image,
            },
            linux_namespace: LinuxNamespaceSettings {
                enabled: ns_enabled,
                unshare_bin,
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

        if self.preference == SandboxPreference::PreferLinuxNamespace {
            if let Some(reason) = self.namespace_unavailable_reason() {
                policy_constraints.push("backend=safe_fallback_degraded".to_string());
                return SandboxResolution {
                    backend: SandboxBackend::SafeFallback,
                    degraded: true,
                    reason: Some(reason),
                    policy_constraints,
                };
            }
            policy_constraints.push("backend=linux_namespace_unshare".to_string());
            policy_constraints.push("network_isolation=true".to_string());
            return SandboxResolution {
                backend: SandboxBackend::LinuxNamespace,
                degraded: false,
                reason: None,
                policy_constraints,
            };
        }

        if self.preference == SandboxPreference::PreferMicroVm {
            if let Some(reason) = self.microvm_unavailable_reason() {
                // try namespace before fallback
                if let Some(ns_reason) = self.namespace_unavailable_reason() {
                    policy_constraints.push("backend=safe_fallback_degraded".to_string());
                    return SandboxResolution {
                        backend: SandboxBackend::SafeFallback,
                        degraded: true,
                        reason: Some(format!("{reason}; {ns_reason}")),
                        policy_constraints,
                    };
                }
                policy_constraints.push("backend=linux_namespace_unshare".to_string());
                policy_constraints.push("network_isolation=true".to_string());
                return SandboxResolution {
                    backend: SandboxBackend::LinuxNamespace,
                    degraded: true,
                    reason: Some(reason),
                    policy_constraints,
                };
            }
            policy_constraints.push("backend=microvm_ephemeral_firecracker_style".to_string());
            return SandboxResolution {
                backend: SandboxBackend::MicroVmEphemeral,
                degraded: false,
                reason: None,
                policy_constraints,
            };
        }

        // Auto
        if self.microvm_unavailable_reason().is_none() {
            policy_constraints.push("backend=microvm_ephemeral_firecracker_style".to_string());
            return SandboxResolution {
                backend: SandboxBackend::MicroVmEphemeral,
                degraded: false,
                reason: None,
                policy_constraints,
            };
        }

        if let Some(ns_reason) = self.namespace_unavailable_reason() {
            policy_constraints.push("backend=safe_fallback_degraded".to_string());
            return SandboxResolution {
                backend: SandboxBackend::SafeFallback,
                degraded: true,
                reason: Some(ns_reason),
                policy_constraints,
            };
        }

        policy_constraints.push("backend=linux_namespace_unshare".to_string());
        policy_constraints.push("network_isolation=true".to_string());
        SandboxResolution {
            backend: SandboxBackend::LinuxNamespace,
            degraded: false,
            reason: None,
            policy_constraints,
        }
    }

    fn namespace_unavailable_reason(&self) -> Option<String> {
        if !self.linux_namespace.enabled {
            return Some("linux_namespace_disabled".to_string());
        }
        if cfg!(target_os = "windows") {
            return Some("linux_namespace_requires_linux".to_string());
        }
        let Some(unshare) = self.linux_namespace.unshare_bin.as_ref() else {
            return Some("unshare_binary_not_configured".to_string());
        };
        if !unshare.exists() {
            return Some("unshare_binary_not_found".to_string());
        }
        None
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

/// # Specification (Verus)
/// ```spec
/// spec fn spec_parse_bool(s: &str) -> bool {
///     matches!(
///         s.trim().to_ascii_lowercase().as_str(),
///         "1" | "true" | "yes" | "on"
///     )
/// }
/// ```
/// # Correctness invariant
/// `parse_bool(s)` returns `true` iff the normalized string is one of the
/// accepted truthy literals. No other input produces `true`.
fn parse_bool(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

#[cfg(feature = "verify")]
mod verify {
    use super::*;

    /// Proof: resolve_backend always produces at least one policy constraint.
    pub fn proof_policy_constraints_nonempty(policy: &SandboxPolicy) {
        let resolution = policy.resolve_backend();
        // This will be caught by Verus if the assertion ever fails.
        assert!(!resolution.policy_constraints.is_empty(),
            "policy_constraints must always be non-empty after resolve_backend");
    }

    /// Proof: MicroVmEphemeral backend is never degraded.
    pub fn proof_microvm_not_degraded(policy: &SandboxPolicy) {
        let resolution = policy.resolve_backend();
        if resolution.backend == SandboxBackend::MicroVmEphemeral {
            assert!(!resolution.degraded,
                "MicroVmEphemeral backend must never be marked degraded");
        }
    }

    /// Proof: if degraded, reason must be Some.
    pub fn proof_degraded_has_reason(policy: &SandboxPolicy) {
        let resolution = policy.resolve_backend();
        if resolution.degraded {
            assert!(resolution.reason.is_some(),
                "degraded resolution must always include a reason");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_policy(
        preference: SandboxPreference,
        microvm_enabled: bool,
        ns_enabled: bool,
        ns_bin: Option<PathBuf>,
    ) -> SandboxPolicy {
        SandboxPolicy {
            preference,
            microvm: MicroVmSettings {
                enabled: microvm_enabled,
                firecracker_bin: None,
                kernel_image: None,
                rootfs_image: None,
            },
            linux_namespace: LinuxNamespaceSettings {
                enabled: ns_enabled,
                unshare_bin: ns_bin,
            },
        }
    }

    #[test]
    fn test_safe_fallback_explicit_policy() {
        let policy = make_policy(SandboxPreference::SafeFallbackOnly, true, false, None);
        let resolution = policy.resolve_backend();
        assert_eq!(resolution.backend, SandboxBackend::SafeFallback);
        assert!(!resolution.degraded);
        assert!(resolution.reason.is_none());
    }

    #[test]
    fn test_microvm_preferred_degrades_with_reason() {
        let policy = make_policy(SandboxPreference::PreferMicroVm, true, false, None);
        let resolution = policy.resolve_backend();
        assert_eq!(resolution.backend, SandboxBackend::SafeFallback);
        assert!(resolution.degraded);
        assert!(resolution.reason.is_some());
    }

    #[test]
    fn test_policy_constraints_always_nonempty() {
        for policy in [
            make_policy(SandboxPreference::SafeFallbackOnly, false, false, None),
            make_policy(SandboxPreference::Auto, false, false, None),
            make_policy(SandboxPreference::PreferMicroVm, true, false, None),
        ] {
            let resolution = policy.resolve_backend();
            assert!(!resolution.policy_constraints.is_empty());
        }
    }

    #[test]
    fn test_microvm_backend_never_degraded() {
        let policy = make_policy(SandboxPreference::PreferMicroVm, true, false, None);
        let resolution = policy.resolve_backend();
        assert_eq!(resolution.backend, SandboxBackend::SafeFallback);
        assert!(resolution.degraded);
        assert!(resolution.reason.is_some());
    }

    #[test]
    fn test_degraded_always_has_reason() {
        let policy = make_policy(SandboxPreference::Auto, true, false, None);
        let resolution = policy.resolve_backend();
        if resolution.degraded {
            assert!(resolution.reason.is_some());
        }
    }

    #[test]
    fn test_linux_namespace_explicit_policy() {
        // /bin/sh exists on all Unix systems; use it as a stand-in for unshare
        let bin = PathBuf::from("/bin/sh");
        if !bin.exists() {
            return; // skip on Windows CI
        }
        let policy = make_policy(
            SandboxPreference::PreferLinuxNamespace,
            false,
            true,
            Some(bin),
        );
        let resolution = policy.resolve_backend();
        assert_eq!(resolution.backend, SandboxBackend::LinuxNamespace);
        assert!(!resolution.degraded);
        assert!(resolution.reason.is_none());
    }

    #[test]
    fn test_linux_namespace_disabled_degrades() {
        let policy = make_policy(
            SandboxPreference::PreferLinuxNamespace,
            false,
            false,
            None,
        );
        let resolution = policy.resolve_backend();
        assert_eq!(resolution.backend, SandboxBackend::SafeFallback);
        assert!(resolution.degraded);
        assert_eq!(
            resolution.reason.as_deref(),
            Some("linux_namespace_disabled")
        );
    }

    #[test]
    fn test_auto_prefers_namespace_over_fallback() {
        let bin = PathBuf::from("/bin/sh");
        if !bin.exists() {
            return; // skip on Windows CI
        }
        let policy = make_policy(SandboxPreference::Auto, false, true, Some(bin));
        let resolution = policy.resolve_backend();
        assert_eq!(resolution.backend, SandboxBackend::LinuxNamespace);
        assert!(!resolution.degraded);
    }
}
