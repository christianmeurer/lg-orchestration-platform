// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fs,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, RwLock,
    },
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::Context;
use globset::GlobSet;
use rusqlite::{params, Connection};
use serde::Serialize;
use sha2::{Digest, Sha256};
use tree_sitter::{Language, Node, Parser};

const INDEX_REFRESH_INTERVAL_MS: u64 = 1_250;
const MAX_INDEX_FILE_BYTES: u64 = 768 * 1024;
const MAX_SEMANTIC_BODY_CHARS: usize = 60_000;
const SNAPSHOT_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct StructuralSnapshot {
    pub schema_version: u32,
    pub version: u64,
    pub generated_at_ms: u64,
    pub files_indexed: usize,
    pub symbols_total: usize,
    pub files: Vec<StructuralFileSummary>,
}

impl Default for StructuralSnapshot {
    fn default() -> Self {
        Self {
            schema_version: SNAPSHOT_SCHEMA_VERSION,
            version: 0,
            generated_at_ms: 0,
            files_indexed: 0,
            symbols_total: 0,
            files: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct StructuralFileSummary {
    pub path: String,
    pub language: String,
    pub bytes: usize,
    pub symbols: Vec<StructuralSymbol>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct StructuralSymbol {
    pub kind: String,
    pub name: String,
    pub start_line: u32,
    pub end_line: u32,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SemanticSearchHit {
    pub path: String,
    pub language: String,
    pub symbols: Vec<String>,
    pub snippet: String,
    pub score: f64,
}

#[derive(Clone)]
pub struct IndexingService {
    inner: Arc<IndexingServiceInner>,
}

struct IndexingServiceInner {
    root_dir: PathBuf,
    allow_read: GlobSet,
    snapshot: RwLock<StructuralSnapshot>,
    version: AtomicU64,
    started: AtomicBool,
    semantic_db_path: PathBuf,
}

impl IndexingService {
    pub fn new(root_dir: PathBuf, allow_read: GlobSet) -> anyhow::Result<Self> {
        let semantic_db_path = semantic_db_path_for_root(&root_dir);
        if let Some(parent) = semantic_db_path.parent() {
            fs::create_dir_all(parent).with_context(|| {
                format!("failed to create semantic index directory: {}", parent.display())
            })?;
        }
        let inner = Arc::new(IndexingServiceInner {
            root_dir,
            allow_read,
            snapshot: RwLock::new(StructuralSnapshot::default()),
            version: AtomicU64::new(0),
            started: AtomicBool::new(false),
            semantic_db_path,
        });
        Ok(Self { inner })
    }

    pub fn ensure_started(&self) {
        if self
            .inner
            .started
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return;
        }

        let inner = Arc::clone(&self.inner);
        let spawn = thread::Builder::new().name("lg-runner-indexer".to_string()).spawn(move || {
            if let Err(err) = run_indexer_loop(inner) {
                tracing::error!(error = %err, "indexer_loop_terminated");
            }
        });

        if let Err(err) = spawn {
            self.inner.started.store(false, Ordering::Release);
            tracing::error!(error = %err, "indexer_thread_spawn_failed");
        }
    }

    pub fn snapshot(&self) -> StructuralSnapshot {
        if let Ok(guard) = self.inner.snapshot.read() {
            return guard.clone();
        }
        StructuralSnapshot::default()
    }

    pub fn current_version(&self) -> u64 {
        self.inner.version.load(Ordering::Acquire)
    }

    pub fn semantic_search(
        &self,
        query: &str,
        limit: usize,
        path_prefix: Option<&str>,
    ) -> anyhow::Result<Vec<SemanticSearchHit>> {
        let fts_query = build_fts_query(query);
        if fts_query.is_empty() {
            return Ok(Vec::new());
        }
        let normalized_prefix =
            path_prefix.map(str::trim).filter(|prefix| !prefix.is_empty()).map(normalize_rel_path);
        let limit_i64 = i64::try_from(limit.clamp(1, 50)).unwrap_or(50);

        let conn = Connection::open(&self.inner.semantic_db_path)
            .with_context(|| "failed to open semantic sqlite database")?;
        initialize_connection(&conn)?;

        let mut hits: Vec<SemanticSearchHit> = Vec::new();
        if let Some(prefix) = normalized_prefix {
            let like_prefix = format!("{}%", prefix);
            let mut stmt = conn.prepare(
                "SELECT path, language, symbols,
                        snippet(code_fts, 3, '[', ']', ' … ', 20) AS snippet,
                        bm25(code_fts) AS score
                 FROM code_fts
                 WHERE code_fts MATCH ?1
                   AND path LIKE ?2
                 ORDER BY score ASC, path ASC
                 LIMIT ?3",
            )?;
            let rows =
                stmt.query_map(params![fts_query, like_prefix, limit_i64], map_row_to_hit)?;
            for row in rows {
                hits.push(row?);
            }
        } else {
            let mut stmt = conn.prepare(
                "SELECT path, language, symbols,
                        snippet(code_fts, 3, '[', ']', ' … ', 20) AS snippet,
                        bm25(code_fts) AS score
                 FROM code_fts
                 WHERE code_fts MATCH ?1
                 ORDER BY score ASC, path ASC
                 LIMIT ?2",
            )?;
            let rows = stmt.query_map(params![fts_query, limit_i64], map_row_to_hit)?;
            for row in rows {
                hits.push(row?);
            }
        }
        Ok(hits)
    }

    #[cfg(test)]
    pub fn wait_for_version_at_least(&self, min_version: u64, timeout: Duration) -> bool {
        let start = std::time::Instant::now();
        while start.elapsed() <= timeout {
            if self.current_version() >= min_version {
                return true;
            }
            thread::sleep(Duration::from_millis(20));
        }
        false
    }
}

fn run_indexer_loop(inner: Arc<IndexingServiceInner>) -> anyhow::Result<()> {
    let mut runtime = RuntimeState::new(&inner.semantic_db_path)?;
    loop {
        if let Err(err) = runtime.refresh(&inner) {
            tracing::warn!(error = %err, "index_refresh_failed");
        }
        thread::sleep(Duration::from_millis(INDEX_REFRESH_INTERVAL_MS));
    }
}

struct RuntimeState {
    fingerprints: HashMap<String, FileFingerprint>,
    files: BTreeMap<String, StructuralFileSummary>,
    conn: Connection,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct FileFingerprint {
    bytes: u64,
    modified_ns: u128,
}

impl FileFingerprint {
    fn from_path(path: &Path) -> Option<Self> {
        let metadata = fs::metadata(path).ok()?;
        let modified_ns = metadata
            .modified()
            .ok()
            .and_then(|ts| ts.duration_since(UNIX_EPOCH).ok())
            .map(|dur| dur.as_nanos())
            .unwrap_or(0);
        Some(Self { bytes: metadata.len(), modified_ns })
    }
}

impl RuntimeState {
    fn new(db_path: &Path) -> anyhow::Result<Self> {
        let conn = Connection::open(db_path)
            .with_context(|| format!("failed to open semantic sqlite db: {}", db_path.display()))?;
        initialize_connection(&conn)?;
        Ok(Self { fingerprints: HashMap::new(), files: BTreeMap::new(), conn })
    }

    fn refresh(&mut self, inner: &IndexingServiceInner) -> anyhow::Result<()> {
        let candidates = collect_candidate_files(&inner.root_dir, &inner.allow_read);
        let mut seen: BTreeSet<String> = BTreeSet::new();
        let mut changed = false;

        for rel_path in candidates {
            let full_path = inner.root_dir.join(&rel_path);
            seen.insert(rel_path.clone());

            let Some(fingerprint) = FileFingerprint::from_path(&full_path) else {
                continue;
            };
            if self.fingerprints.get(&rel_path).copied() == Some(fingerprint) {
                continue;
            }

            match parse_indexed_file(&full_path, &rel_path) {
                Ok(parsed) => {
                    self.fingerprints.insert(rel_path.clone(), fingerprint);
                    self.files.insert(rel_path.clone(), parsed.summary.clone());
                    upsert_semantic_row(&self.conn, &parsed.summary, &parsed.semantic_body)?;
                    changed = true;
                }
                Err(err) => {
                    tracing::debug!(path = %rel_path, error = %err, "index_parse_failed");
                }
            }
        }

        let existing_paths: Vec<String> = self.fingerprints.keys().cloned().collect();
        for old_path in existing_paths {
            if seen.contains(&old_path) {
                continue;
            }
            self.fingerprints.remove(&old_path);
            self.files.remove(&old_path);
            delete_semantic_row(&self.conn, &old_path)?;
            changed = true;
        }

        if changed || inner.version.load(Ordering::Acquire) == 0 {
            let next_version = inner.version.fetch_add(1, Ordering::AcqRel) + 1;
            let files: Vec<StructuralFileSummary> = self.files.values().cloned().collect();
            let snapshot = snapshot_from_files(next_version, files);
            if let Ok(mut guard) = inner.snapshot.write() {
                *guard = snapshot;
            }
        }
        Ok(())
    }
}

#[derive(Debug)]
struct ParsedIndexedFile {
    summary: StructuralFileSummary,
    semantic_body: String,
}

fn parse_indexed_file(full_path: &Path, rel_path: &str) -> anyhow::Result<ParsedIndexedFile> {
    let language_kind =
        LanguageKind::from_rel_path(rel_path).context("unsupported file extension")?;
    let raw = fs::read(full_path)
        .with_context(|| format!("failed to read source file: {}", full_path.display()))?;
    let source = String::from_utf8_lossy(&raw).to_string();
    let symbols = extract_symbols(&source, language_kind)?;
    let summary = StructuralFileSummary {
        path: rel_path.to_string(),
        language: language_kind.label().to_string(),
        bytes: raw.len(),
        symbols,
    };
    let semantic_body = build_semantic_body(&source, &summary.symbols);
    Ok(ParsedIndexedFile { summary, semantic_body })
}

fn snapshot_from_files(version: u64, mut files: Vec<StructuralFileSummary>) -> StructuralSnapshot {
    files.sort_by(|a, b| a.path.cmp(&b.path));
    let symbols_total = files.iter().map(|entry| entry.symbols.len()).sum();
    StructuralSnapshot {
        schema_version: SNAPSHOT_SCHEMA_VERSION,
        version,
        generated_at_ms: now_ms(),
        files_indexed: files.len(),
        symbols_total,
        files,
    }
}

fn is_excluded_path(path: &std::path::Path) -> bool {
    path.components().any(|c| {
        matches!(
            c.as_os_str().to_str().unwrap_or(""),
            ".venv" | ".git" | "node_modules" | "target" | "__pycache__" | ".hypothesis"
        )
    })
}

fn collect_candidate_files(root_dir: &Path, allow_read: &GlobSet) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for entry in walkdir::WalkDir::new(root_dir).into_iter().filter_map(Result::ok) {
        let full_path = entry.path();
        if !full_path.is_file() {
            continue;
        }
        if is_excluded_path(full_path) {
            continue;
        }
        let Ok(rel) = full_path.strip_prefix(root_dir) else {
            continue;
        };
        let rel_str = normalize_rel_path(&rel.to_string_lossy());
        if !allow_read.is_match(&rel_str) {
            continue;
        }
        if LanguageKind::from_rel_path(&rel_str).is_none() {
            continue;
        }
        let Ok(meta) = fs::metadata(full_path) else {
            continue;
        };
        if meta.len() > MAX_INDEX_FILE_BYTES {
            continue;
        }
        out.push(rel_str);
    }
    out.sort();
    out.dedup();
    out
}

#[derive(Clone, Copy, Debug)]
enum LanguageKind {
    Rust,
    Python,
}

impl LanguageKind {
    fn from_rel_path(path: &str) -> Option<Self> {
        if path.ends_with(".rs") {
            return Some(Self::Rust);
        }
        if path.ends_with(".py") {
            return Some(Self::Python);
        }
        None
    }

    fn label(self) -> &'static str {
        match self {
            Self::Rust => "rust",
            Self::Python => "python",
        }
    }

    fn language(self) -> Language {
        match self {
            Self::Rust => tree_sitter_rust::LANGUAGE.into(),
            Self::Python => tree_sitter_python::LANGUAGE.into(),
        }
    }
}

fn extract_symbols(
    source: &str,
    language_kind: LanguageKind,
) -> anyhow::Result<Vec<StructuralSymbol>> {
    let mut parser = Parser::new();
    let language = language_kind.language();
    parser
        .set_language(&language)
        .map_err(|err| anyhow::anyhow!("failed to set language parser: {}", err))?;

    let Some(tree) = parser.parse(source, None) else {
        return Ok(Vec::new());
    };

    let mut out: Vec<StructuralSymbol> = Vec::new();
    collect_symbols(tree.root_node(), source.as_bytes(), language_kind, &mut out);
    out.sort();
    out.dedup();
    Ok(out)
}

fn collect_symbols(
    node: Node<'_>,
    source_bytes: &[u8],
    language_kind: LanguageKind,
    out: &mut Vec<StructuralSymbol>,
) {
    if let Some(kind) = symbol_kind_for_node(language_kind, node.kind()) {
        let name = node
            .child_by_field_name("name")
            .and_then(|name_node| name_node.utf8_text(source_bytes).ok())
            .map(str::trim)
            .unwrap_or_default()
            .to_string();
        if !name.is_empty() {
            out.push(StructuralSymbol {
                kind: kind.to_string(),
                name,
                start_line: to_line(node.start_position().row),
                end_line: to_line(node.end_position().row),
            });
        }
    }

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        collect_symbols(child, source_bytes, language_kind, out);
    }
}

fn symbol_kind_for_node(language_kind: LanguageKind, node_kind: &str) -> Option<&'static str> {
    match language_kind {
        LanguageKind::Rust => match node_kind {
            "function_item" => Some("function"),
            "struct_item" => Some("struct"),
            "enum_item" => Some("enum"),
            "trait_item" => Some("trait"),
            "mod_item" => Some("module"),
            "const_item" => Some("const"),
            "type_item" => Some("type"),
            _ => None,
        },
        LanguageKind::Python => match node_kind {
            "function_definition" => Some("function"),
            "async_function_definition" => Some("async_function"),
            "class_definition" => Some("class"),
            _ => None,
        },
    }
}

fn initialize_connection(conn: &Connection) -> anyhow::Result<()> {
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
            path UNINDEXED,
            language UNINDEXED,
            symbols,
            body,
            tokenize='unicode61'
         );",
    )
    .with_context(|| "failed to initialize semantic sqlite schema")?;
    Ok(())
}

fn upsert_semantic_row(
    conn: &Connection,
    summary: &StructuralFileSummary,
    semantic_body: &str,
) -> anyhow::Result<()> {
    let symbol_blob = summary
        .symbols
        .iter()
        .map(|symbol| format!("{} {}", symbol.kind, symbol.name))
        .collect::<Vec<String>>()
        .join("\n");
    conn.execute("DELETE FROM code_fts WHERE path = ?1", params![summary.path.as_str()])?;
    conn.execute(
        "INSERT INTO code_fts(path, language, symbols, body) VALUES(?1, ?2, ?3, ?4)",
        params![summary.path.as_str(), summary.language.as_str(), symbol_blob, semantic_body],
    )?;
    Ok(())
}

fn delete_semantic_row(conn: &Connection, rel_path: &str) -> anyhow::Result<()> {
    conn.execute("DELETE FROM code_fts WHERE path = ?1", params![rel_path])?;
    Ok(())
}

fn build_semantic_body(source: &str, symbols: &[StructuralSymbol]) -> String {
    let mut body = String::new();
    for symbol in symbols {
        body.push_str(&symbol.name);
        body.push('\n');
    }
    body.push_str(&truncate_chars(source, MAX_SEMANTIC_BODY_CHARS));
    body
}

fn truncate_chars(input: &str, max_chars: usize) -> String {
    input.chars().take(max_chars).collect()
}

fn build_fts_query(query: &str) -> String {
    query
        .split_whitespace()
        .map(normalize_fts_token)
        .filter(|token| !token.is_empty())
        .map(|token| format!("{}*", token))
        .collect::<Vec<String>>()
        .join(" AND ")
}

fn normalize_fts_token(raw: &str) -> String {
    raw.chars().filter(|ch| ch.is_alphanumeric() || *ch == '_').collect::<String>().to_lowercase()
}

fn map_row_to_hit(row: &rusqlite::Row<'_>) -> rusqlite::Result<SemanticSearchHit> {
    let symbols_blob: String = row.get(2)?;
    let symbols = symbols_blob
        .split('\n')
        .filter_map(|line| line.split_whitespace().last())
        .map(str::to_string)
        .take(8)
        .collect::<Vec<String>>();
    Ok(SemanticSearchHit {
        path: row.get(0)?,
        language: row.get(1)?,
        symbols,
        snippet: row.get(3)?,
        score: row.get(4)?,
    })
}

fn semantic_db_path_for_root(root_dir: &Path) -> PathBuf {
    // Use SHA-256 (first 8 bytes) for a stable, version-independent path derivation.
    // DefaultHasher is NOT stable across Rust versions — it would orphan the index after upgrades.
    let digest = Sha256::digest(root_dir.to_string_lossy().as_bytes());
    let h: String = digest[..8].iter().map(|b| format!("{b:02x}")).collect();
    std::env::temp_dir().join("lg-runner").join(format!("semantic-index-{h}.sqlite3"))
}

fn normalize_rel_path(path: &str) -> String {
    path.replace('\\', "/")
}

fn to_line(row: usize) -> u32 {
    u32::try_from(row.saturating_add(1)).unwrap_or(u32::MAX)
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_else(|_| Duration::from_millis(0))
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn allow_all() -> GlobSet {
        let mut builder = globset::GlobSetBuilder::new();
        builder.add(globset::Glob::new("**").unwrap());
        builder.build().unwrap()
    }

    #[test]
    fn test_snapshot_deterministic_between_reads() {
        let td = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(td.path().join("py")).unwrap();
        std::fs::create_dir_all(td.path().join("rs")).unwrap();
        std::fs::write(td.path().join("py/app.py"), "class A:\n    pass\n").unwrap();
        std::fs::write(td.path().join("rs/lib.rs"), "pub fn alpha() -> i32 { 1 }\n").unwrap();

        let service = IndexingService::new(td.path().canonicalize().unwrap(), allow_all()).unwrap();
        service.ensure_started();
        assert!(service.wait_for_version_at_least(1, Duration::from_secs(3)));

        let first = service.snapshot();
        let second = service.snapshot();

        assert_eq!(first.schema_version, SNAPSHOT_SCHEMA_VERSION);
        assert_eq!(first.files, second.files);
        assert!(first.files.iter().any(|entry| entry.path.ends_with("py/app.py")));
        assert!(first.files.iter().any(|entry| entry.path.ends_with("rs/lib.rs")));
    }

    #[test]
    fn test_incremental_update_changes_snapshot_version() {
        let td = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(td.path().join("rs")).unwrap();
        let file_path = td.path().join("rs/main.rs");
        std::fs::write(&file_path, "fn alpha() -> i32 { 1 }\n").unwrap();

        let service = IndexingService::new(td.path().canonicalize().unwrap(), allow_all()).unwrap();
        service.ensure_started();
        assert!(service.wait_for_version_at_least(1, Duration::from_secs(3)));

        let before = service.snapshot();
        std::fs::write(&file_path, "fn alpha() -> i32 { 1 }\nfn beta() -> i32 { 2 }\n").unwrap();
        assert!(service.wait_for_version_at_least(before.version + 1, Duration::from_secs(3)));

        let after = service.snapshot();
        let entry = after.files.iter().find(|f| f.path.ends_with("rs/main.rs")).unwrap();
        assert!(entry.symbols.iter().any(|symbol| symbol.name == "beta"));
    }

    #[test]
    fn test_semantic_search_returns_local_hits() {
        let td = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(td.path().join("py")).unwrap();
        std::fs::write(
            td.path().join("py/search_target.py"),
            "def orchestrate_memory_context():\n    return 'ok'\n",
        )
        .unwrap();

        let service = IndexingService::new(td.path().canonicalize().unwrap(), allow_all()).unwrap();
        service.ensure_started();
        assert!(service.wait_for_version_at_least(1, Duration::from_secs(3)));

        let hits = service.semantic_search("orchestrate memory context", 5, Some("py/")).unwrap();
        assert!(!hits.is_empty());
        assert!(hits.iter().any(|hit| hit.path.ends_with("py/search_target.py")));
    }
}
