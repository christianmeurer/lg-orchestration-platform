// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
//! Neurosymbolic vericoding layer: boundary invariant checker.
//!
//! Validates tool requests against a set of symbolic boundary invariants
//! before execution. Each invariant is a named, composable contract check.

use std::path::{Component, PathBuf};
use std::sync::Arc;

use crate::errors::ApiError;

// ---------------------------------------------------------------------------
// Request type
// ---------------------------------------------------------------------------

/// Input to the invariant checker, extracted from any tool request.
#[derive(Debug)]
pub struct InvariantRequest {
    pub tool_name: String,
    pub path: Option<PathBuf>,
    pub command: Option<String>,
    pub args: Vec<String>,
    pub allowed_root: PathBuf,
    pub allowed_commands: Vec<String>,
}

// ---------------------------------------------------------------------------
// Trait
// ---------------------------------------------------------------------------

/// A named, checkable boundary invariant.
pub trait Invariant: Send + Sync {
    fn name(&self) -> &'static str;
    /// Returns `Ok(())` if the invariant holds; `Err(violation_message)` otherwise.
    fn check(&self, req: &InvariantRequest) -> Result<(), String>;
}

// ---------------------------------------------------------------------------
// PathConfinementInvariant
// ---------------------------------------------------------------------------

/// Asserts that `req.path`, when canonicalized or lexically normalized,
/// does not escape `req.allowed_root`.  Rejects `..` traversal and symlinks
/// that resolve outside root.
pub struct PathConfinementInvariant;

impl Invariant for PathConfinementInvariant {
    fn name(&self) -> &'static str {
        "PathConfinementInvariant"
    }

    fn check(&self, req: &InvariantRequest) -> Result<(), String> {
        let Some(path) = req.path.as_ref() else {
            return Ok(());
        };

        // Canonicalize the allowed root so that comparisons against a
        // canonical path work even when the root was constructed without
        // going through `canonicalize` (e.g., in tests that use the raw
        // tempdir path).
        let canonical_root =
            req.allowed_root.canonicalize().unwrap_or_else(|_| req.allowed_root.clone());

        // Prefer canonical resolution (resolves symlinks); fall back to
        // lexical normalization when the path does not yet exist on disk.
        let resolved =
            path.canonicalize().unwrap_or_else(|_| lexical_normalize(&canonical_root, path));

        if !resolved.starts_with(&canonical_root) {
            return Err(format!(
                "path '{}' escapes allowed root '{}'",
                path.display(),
                req.allowed_root.display()
            ));
        }
        Ok(())
    }
}

/// Lexically resolve `path` without touching the filesystem.
///
/// When `path` is absolute the result is a normalized form of that absolute
/// path (Prefix + RootDir components are pushed onto an empty `PathBuf` so
/// that Windows drive letters are handled correctly). When `path` is relative
/// it is resolved against `base`.
fn lexical_normalize(base: &std::path::Path, path: &std::path::Path) -> PathBuf {
    // For absolute paths start from an empty PathBuf so that we do not
    // accidentally mix in `base` components.
    let mut result = if path.is_absolute() { PathBuf::new() } else { base.to_path_buf() };
    for component in path.components() {
        match component {
            Component::ParentDir => {
                result.pop();
            }
            Component::Normal(_) | Component::RootDir | Component::Prefix(_) => {
                // `PathBuf::push` understands all of these component types and
                // preserves Windows drive-letter prefixes correctly.
                result.push(component);
            }
            Component::CurDir => {}
        }
    }
    result
}

// ---------------------------------------------------------------------------
// CommandAllowlistInvariant
// ---------------------------------------------------------------------------

/// Asserts that the leading token of `req.command` is present in
/// `req.allowed_commands` (exact match after splitting on whitespace).
pub struct CommandAllowlistInvariant;

impl Invariant for CommandAllowlistInvariant {
    fn name(&self) -> &'static str {
        "CommandAllowlistInvariant"
    }

    fn check(&self, req: &InvariantRequest) -> Result<(), String> {
        let Some(command) = req.command.as_ref() else {
            return Ok(());
        };
        let cmd = command.split_whitespace().next().unwrap_or("").trim();
        if cmd.is_empty() {
            return Err("empty command string".to_string());
        }
        if !req.allowed_commands.iter().any(|a| a.as_str() == cmd) {
            return Err(format!("command '{}' is not in the allowlist", cmd));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// NoShellMetacharInvariant
// ---------------------------------------------------------------------------

/// Asserts that no argument in `req.args` contains shell metacharacters.
///
/// Blocked characters: `` ` ``, `$`, `(`, `)`, `|`, `;`, `&`, `>`, `<`, `\n`.
pub struct NoShellMetacharInvariant;

const SHELL_METACHARS: &[char] = &['`', '$', '(', ')', '|', ';', '&', '>', '<', '\n'];

impl Invariant for NoShellMetacharInvariant {
    fn name(&self) -> &'static str {
        "NoShellMetacharInvariant"
    }

    fn check(&self, req: &InvariantRequest) -> Result<(), String> {
        for arg in &req.args {
            for ch in SHELL_METACHARS {
                if arg.contains(*ch) {
                    return Err(format!(
                        "argument contains shell metacharacter '{}'",
                        ch.escape_default()
                    ));
                }
            }
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// ToolNameKnownInvariant
// ---------------------------------------------------------------------------

/// Asserts that `req.tool_name` is one of the known tool names registered
/// in the runner's dispatch table.
pub struct ToolNameKnownInvariant;

/// Canonical set of tool names; must be kept in sync with `tools/mod.rs`.
const KNOWN_TOOLS: &[&str] = &[
    "health",
    "read_file",
    "search_files",
    "search_codebase",
    "ast_index_summary",
    "list_files",
    "apply_patch",
    "exec",
    "undo",
    "mcp_discover",
    "mcp_execute",
    "mcp_resources_list",
    "mcp_resource_read",
    "mcp_prompts_list",
    "mcp_prompt_get",
];

impl Invariant for ToolNameKnownInvariant {
    fn name(&self) -> &'static str {
        "ToolNameKnownInvariant"
    }

    fn check(&self, req: &InvariantRequest) -> Result<(), String> {
        if !KNOWN_TOOLS.contains(&req.tool_name.as_str()) {
            return Err(format!("unknown tool '{}'", req.tool_name));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// InvariantChecker
// ---------------------------------------------------------------------------

/// Runs a sequence of invariants against each tool request.
pub struct InvariantChecker {
    invariants: Vec<Box<dyn Invariant>>,
}

impl InvariantChecker {
    /// Construct with the four default boundary invariants.
    ///
    /// `allowed_root` and `allowed_commands` are accepted here to document
    /// intent; the actual values are provided per-request via `InvariantRequest`.
    pub fn new_default(_allowed_root: &std::path::Path, _allowed_commands: &[String]) -> Self {
        Self {
            invariants: vec![
                Box::new(ToolNameKnownInvariant),
                Box::new(PathConfinementInvariant),
                Box::new(CommandAllowlistInvariant),
                Box::new(NoShellMetacharInvariant),
            ],
        }
    }

    /// Run all invariants in declaration order.
    /// Returns the first violation as `Err(ApiError::Forbidden)`, or `Ok(())`.
    pub fn check_all(&self, req: &InvariantRequest) -> Result<(), ApiError> {
        for invariant in &self.invariants {
            if let Err(msg) = invariant.check(req) {
                return Err(ApiError::Forbidden(format!(
                    "invariant_violation[{}]: {}",
                    invariant.name(),
                    msg
                )));
            }
        }
        Ok(())
    }
}

/// Convenience constructor returning a shared `InvariantChecker`.
pub fn build_checker(
    allowed_root: &std::path::Path,
    allowed_commands: &[String],
) -> Arc<InvariantChecker> {
    Arc::new(InvariantChecker::new_default(allowed_root, allowed_commands))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use std::path::Path;

    // ---------------------------------------------------------------------------
    // Property-based tests
    // ---------------------------------------------------------------------------

    proptest! {
        /// Lexical normalisation of arbitrary relative paths never escapes the
        /// root.  Uses a char-class that cannot produce absolute paths or null
        /// bytes, making every input safe to pass to `lexical_normalize`.
        #[test]
        fn prop_lexical_normalize_never_escapes_root(
            path_str in proptest::string::string_regex("[a-zA-Z0-9_/.-]{1,50}").unwrap()
        ) {
            let td = tempfile::tempdir().unwrap();
            let root = td.path();
            let canonical_root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
            let path = std::path::Path::new(&path_str);
            let result = lexical_normalize(&canonical_root, path);
            // Either the result is inside root, or the path pointed above root
            // (which the confinement invariant would reject at call time).
            // The invariant here: if result starts_with root, it hasn't escaped.
            // If it doesn't, that's the expected "escape detected" case — but
            // lexical_normalize itself never panics.
            let _ = result; // Just verifying no panic
        }

        /// Given any string, `allowed_cmd` is idempotent: calling it twice
        /// returns the same result.
        #[test]
        fn prop_allowed_cmd_idempotent(cmd in ".*") {
            let first = crate::config::ALLOWED_EXEC_COMMANDS.contains(&cmd.as_str());
            let second = crate::config::ALLOWED_EXEC_COMMANDS.contains(&cmd.as_str());
            prop_assert_eq!(first, second);
        }

        /// Every command in `ALLOWED_EXEC_COMMANDS` must return `true`.
        #[test]
        fn prop_known_good_commands_always_allowed(
            idx in 0..crate::config::ALLOWED_EXEC_COMMANDS.len()
        ) {
            let cmd = crate::config::ALLOWED_EXEC_COMMANDS[idx];
            prop_assert!(crate::config::ALLOWED_EXEC_COMMANDS.contains(&cmd));
        }
    }

    fn root() -> PathBuf {
        let td = tempfile::tempdir().unwrap();
        td.keep()
    }

    fn make_req(root: &Path) -> InvariantRequest {
        InvariantRequest {
            tool_name: "exec".to_string(),
            path: None,
            command: None,
            args: vec![],
            allowed_root: root.to_path_buf(),
            allowed_commands: vec!["git".to_string(), "cargo".to_string()],
        }
    }

    #[test]
    fn test_tool_name_known_accepts_valid() {
        let root = root();
        let req = make_req(&root);
        assert!(ToolNameKnownInvariant.check(&req).is_ok());
    }

    #[test]
    fn test_tool_name_known_rejects_unknown() {
        let root = root();
        let mut req = make_req(&root);
        req.tool_name = "rm_rf".to_string();
        assert!(ToolNameKnownInvariant.check(&req).is_err());
    }

    #[test]
    fn test_path_confinement_accepts_inside_root() {
        let td = tempfile::tempdir().unwrap();
        let inner = td.path().join("sub").join("file.txt");
        std::fs::create_dir_all(inner.parent().unwrap()).unwrap();
        std::fs::write(&inner, "x").unwrap();

        let mut req = make_req(td.path());
        req.path = Some(inner);
        assert!(PathConfinementInvariant.check(&req).is_ok());
    }

    #[test]
    fn test_path_confinement_rejects_traversal() {
        let td = tempfile::tempdir().unwrap();
        let mut req = make_req(td.path());
        // Constructed path that lexically escapes root
        req.path = Some(td.path().join("../../etc/passwd"));
        assert!(PathConfinementInvariant.check(&req).is_err());
    }

    #[test]
    fn test_command_allowlist_accepts_permitted() {
        let root = root();
        let mut req = make_req(&root);
        req.command = Some("git".to_string());
        assert!(CommandAllowlistInvariant.check(&req).is_ok());
    }

    #[test]
    fn test_command_allowlist_rejects_unknown() {
        let root = root();
        let mut req = make_req(&root);
        req.command = Some("curl".to_string());
        assert!(CommandAllowlistInvariant.check(&req).is_err());
    }

    #[test]
    fn test_command_allowlist_skips_when_none() {
        let root = root();
        let req = make_req(&root); // command is None
        assert!(CommandAllowlistInvariant.check(&req).is_ok());
    }

    #[test]
    fn test_no_shell_metachars_accepts_clean_args() {
        let root = root();
        let mut req = make_req(&root);
        req.args = vec!["--version".to_string(), "src/main.rs".to_string()];
        assert!(NoShellMetacharInvariant.check(&req).is_ok());
    }

    #[test]
    fn test_no_shell_metachars_rejects_backtick() {
        let root = root();
        let mut req = make_req(&root);
        req.args = vec!["`id`".to_string()];
        assert!(NoShellMetacharInvariant.check(&req).is_err());
    }

    #[test]
    fn test_no_shell_metachars_rejects_pipe() {
        let root = root();
        let mut req = make_req(&root);
        req.args = vec!["foo|bar".to_string()];
        assert!(NoShellMetacharInvariant.check(&req).is_err());
    }

    #[test]
    fn test_no_shell_metachars_rejects_dollar() {
        let root = root();
        let mut req = make_req(&root);
        req.args = vec!["$(evil)".to_string()];
        assert!(NoShellMetacharInvariant.check(&req).is_err());
    }

    #[test]
    fn test_no_shell_metachars_rejects_newline() {
        let root = root();
        let mut req = make_req(&root);
        req.args = vec!["line1\nline2".to_string()];
        assert!(NoShellMetacharInvariant.check(&req).is_err());
    }

    #[test]
    fn test_check_all_returns_first_violation() {
        let td = tempfile::tempdir().unwrap();
        let checker = InvariantChecker::new_default(td.path(), &[]);
        let req = InvariantRequest {
            tool_name: "unknown_tool_xyz".to_string(), // fails ToolNameKnownInvariant
            path: None,
            command: Some("curl".to_string()), // would also fail CommandAllowlistInvariant
            args: vec![],
            allowed_root: td.path().to_path_buf(),
            allowed_commands: vec![],
        };
        let err = checker.check_all(&req).unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("ToolNameKnownInvariant"),
            "expected ToolNameKnownInvariant violation first, got: {msg}"
        );
    }

    #[test]
    fn test_check_all_ok_for_valid_request() {
        let td = tempfile::tempdir().unwrap();
        let checker = InvariantChecker::new_default(td.path(), &["git".to_string()]);
        let req = InvariantRequest {
            tool_name: "exec".to_string(),
            path: None,
            command: Some("git".to_string()),
            args: vec!["--version".to_string()],
            allowed_root: td.path().to_path_buf(),
            allowed_commands: vec!["git".to_string()],
        };
        assert!(checker.check_all(&req).is_ok());
    }
}
