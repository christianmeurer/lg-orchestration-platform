// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
use std::sync::LazyLock;

use regex::Regex;

use crate::envelope::Diagnostic;

// ---------------------------------------------------------------------------
// Compiled-once regex statics (Wave 9 perf: avoid re-compiling per call)
// ---------------------------------------------------------------------------

/// Rust compiler error/warning header: `error[E0432]: unresolved import`
static RE_RUST: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(error|warning)(?:\[(?P<code>[A-Z]\d{4}|[a-z]\d{3})\])?:\s*(?P<message>.+)$")
        .expect("static regex")
});

/// Rust compiler location line: ` --> src/main.rs:10:5`
static RE_RUST_AT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\s*-->\s*(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)\s*$").expect("static regex")
});

/// GCC / Clippy / generic `file:line:col: severity: message` format
static RE_GCC_LIKE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^(?P<file>[^:\n\r]+):(?P<line>\d+):(?P<col>\d+):\s*(?:(?P<sev>error|warning|note):\s*)?(?:(?P<code>[-A-Za-z0-9_]+):\s*)?(?P<message>.+)$",
    )
    .expect("static regex")
});

/// Trailing bracket code: `[clippy::manual_map]`
static RE_BRACKET_CODE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[(?P<code>[A-Za-z0-9_:.\-]+)\]\s*$").expect("static regex"));

/// Python traceback file/line: `  File "app.py", line 7, in <module>`
static RE_PYTHON: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"^\s*File\s+"(?P<file>[^"]+)",\s+line\s+(?P<line>\d+).*$"#).expect("static regex")
});

fn fnv1a_64(text: &str) -> u64 {
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in text.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

fn diagnostic_fingerprint(
    file: &str,
    line: Option<u32>,
    column: Option<u32>,
    code: Option<&str>,
    message: &str,
) -> String {
    let normalized = format!(
        "{}|{}|{}|{}|{}",
        file.trim().to_lowercase(),
        line.map_or_else(String::new, |v| v.to_string()),
        column.map_or_else(String::new, |v| v.to_string()),
        code.unwrap_or_default().trim().to_lowercase(),
        message.trim().to_lowercase(),
    );
    format!("{:016x}", fnv1a_64(&normalized))
}

fn parse_u32(v: Option<&str>) -> Option<u32> {
    v.and_then(|s| s.parse::<u32>().ok())
}

fn normalize_file(raw: &str) -> String {
    raw.trim_matches('"').trim().to_string()
}

pub fn parse_structured_diagnostics(stderr: &str) -> Vec<Diagnostic> {
    let mut out: Vec<Diagnostic> = Vec::new();

    let lines: Vec<&str> = stderr.lines().collect();
    let mut i = 0usize;
    while i < lines.len() {
        let line = lines[i].trim_end();

        if let Some(caps) = RE_GCC_LIKE.captures(line) {
            let file = normalize_file(caps.name("file").map_or("", |m| m.as_str()));
            let line_no = parse_u32(caps.name("line").map(|m| m.as_str()));
            let col_no = parse_u32(caps.name("col").map(|m| m.as_str()));
            let mut code = caps.name("code").map(|m| m.as_str().trim().to_string());
            let mut message = caps.name("message").map_or("", |m| m.as_str()).trim().to_string();

            if code.is_none() {
                if let Some(bc) = RE_BRACKET_CODE.captures(&message) {
                    code = bc.name("code").map(|m| m.as_str().to_string());
                    if let Some(mat) = bc.get(0) {
                        message = message[..mat.start()].trim_end().to_string();
                    }
                }
            }

            if !file.is_empty() && !message.is_empty() {
                let fingerprint =
                    diagnostic_fingerprint(&file, line_no, col_no, code.as_deref(), &message);
                out.push(Diagnostic {
                    file: file.clone(),
                    line: line_no,
                    column: col_no,
                    code: code.clone(),
                    fingerprint: Some(fingerprint),
                    message,
                });
                i += 1;
                continue;
            }
        }

        if let Some(caps) = RE_RUST.captures(line) {
            let code = caps.name("code").map(|m| m.as_str().to_string());
            let message = caps.name("message").map_or("", |m| m.as_str()).trim().to_string();

            let mut file = String::new();
            let mut line_no: Option<u32> = None;
            let mut col_no: Option<u32> = None;
            if let Some(next_line) = lines.get(i + 1).copied() {
                if let Some(at_caps) = RE_RUST_AT.captures(next_line.trim_end()) {
                    file = normalize_file(at_caps.name("file").map_or("", |m| m.as_str()));
                    line_no = parse_u32(at_caps.name("line").map(|m| m.as_str()));
                    col_no = parse_u32(at_caps.name("col").map(|m| m.as_str()));
                }
            }

            let fingerprint =
                diagnostic_fingerprint(&file, line_no, col_no, code.as_deref(), &message);
            out.push(Diagnostic {
                file: file.clone(),
                line: line_no,
                column: col_no,
                code: code.clone(),
                fingerprint: Some(fingerprint),
                message,
            });
            i += 1;
            continue;
        }

        if let Some(caps) = RE_PYTHON.captures(line) {
            let file = normalize_file(caps.name("file").map_or("", |m| m.as_str()));
            let line_no = parse_u32(caps.name("line").map(|m| m.as_str()));
            let mut message = String::new();
            if let Some(next_line) = lines.get(i + 1).copied() {
                let trimmed = next_line.trim();
                if !trimmed.is_empty() && !trimmed.starts_with('^') {
                    message = trimmed.to_string();
                }
            }
            if message.is_empty() {
                if let Some(last) = lines.last() {
                    let t = last.trim();
                    if !t.is_empty() {
                        message = t.to_string();
                    }
                }
            }
            let fingerprint = diagnostic_fingerprint(&file, line_no, None, None, &message);
            out.push(Diagnostic {
                file: file.clone(),
                line: line_no,
                column: None,
                code: None,
                fingerprint: Some(fingerprint),
                message,
            });
            i += 1;
            continue;
        }

        i += 1;
    }

    out
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::*;

    // ---------------------------------------------------------------------------
    // Property-based tests
    // ---------------------------------------------------------------------------

    proptest! {
        /// `parse_structured_diagnostics` must never panic on arbitrary input.
        /// This is a stability / robustness property — no matter what bytes
        /// the caller feeds in, the function must return (possibly empty) output.
        #[test]
        fn prop_parse_structured_diagnostics_never_panics(
            input in proptest::string::string_regex("[[:print:]\n\r]{0,300}").unwrap()
        ) {
            let result = parse_structured_diagnostics(&input);
            // Result can be any Vec<Diagnostic> — we only assert no panic and
            // that the returned diagnostics have non-empty file/message fields.
            for diag in &result {
                prop_assert!(!diag.file.is_empty() || !diag.message.is_empty());
            }
        }

        /// The FNV-1a fingerprint of two identical strings must be equal.
        #[test]
        fn prop_fnv1a_fingerprint_deterministic(
            s in proptest::string::string_regex("[[:print:]]{0,100}").unwrap()
        ) {
            let h1 = fnv1a_64(&s);
            let h2 = fnv1a_64(&s);
            prop_assert_eq!(h1, h2);
        }
    }

    #[test]
    fn parses_rust_error_with_location() {
        let stderr = "error[E0432]: unresolved import `crate::missing`\n --> src/main.rs:10:5\n";
        let diags = parse_structured_diagnostics(stderr);
        assert_eq!(diags.len(), 1);
        assert_eq!(diags[0].file, "src/main.rs");
        assert_eq!(diags[0].line, Some(10));
        assert_eq!(diags[0].column, Some(5));
        assert_eq!(diags[0].code.as_deref(), Some("E0432"));
        assert!(diags[0].message.contains("unresolved import"));
    }

    #[test]
    fn parses_clippy_like_format() {
        let stderr = "src/lib.rs:42:13: warning: this can be simplified [clippy::manual_map]";
        let diags = parse_structured_diagnostics(stderr);
        assert_eq!(diags.len(), 1);
        assert_eq!(diags[0].file, "src/lib.rs");
        assert_eq!(diags[0].line, Some(42));
        assert_eq!(diags[0].column, Some(13));
        assert_eq!(diags[0].code.as_deref(), Some("clippy::manual_map"));
    }

    #[test]
    fn parses_python_traceback_file_line() {
        let stderr = "Traceback (most recent call last):\n  File \"app.py\", line 7, in <module>\n    raise RuntimeError('x')\nRuntimeError: x\n";
        let diags = parse_structured_diagnostics(stderr);
        assert_eq!(diags.len(), 1);
        assert_eq!(diags[0].file, "app.py");
        assert_eq!(diags[0].line, Some(7));
        assert!(diags[0].message.contains("raise RuntimeError"));
    }

    #[test]
    fn parses_gcc_style_error() {
        let stderr = "src/main.c:12:3: error: unknown type name 'foo'";
        let diags = parse_structured_diagnostics(stderr);
        assert_eq!(diags.len(), 1);
        assert_eq!(diags[0].file, "src/main.c");
        assert_eq!(diags[0].line, Some(12));
        assert_eq!(diags[0].column, Some(3));
        assert!(diags[0].message.contains("unknown type name"));
    }

    #[test]
    fn ignores_unstructured_stderr() {
        let diags = parse_structured_diagnostics("just failed");
        assert!(diags.is_empty());
    }
}
