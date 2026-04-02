from __future__ import annotations

from lg_orch.glean import (
    DEFAULT_GUIDELINES,
    GleanAuditor,
    Guideline,
    GuidelineViolation,
)


def _auditor_with_defaults() -> GleanAuditor:
    auditor = GleanAuditor()
    for g in DEFAULT_GUIDELINES:
        auditor.add_guideline(g)
    return auditor


def test_pre_execution_blocks_force_push() -> None:
    auditor = _auditor_with_defaults()
    blocking = auditor.check_pre_execution("bash", {"command": "git push origin main --force"})
    assert len(blocking) == 1
    assert blocking[0].guideline_id == "no-force-push"
    assert blocking[0].severity == "block"


def test_pre_execution_blocks_force_push_short_flag() -> None:
    auditor = _auditor_with_defaults()
    blocking = auditor.check_pre_execution("bash", {"command": "git push origin master -f"})
    assert len(blocking) == 1
    assert blocking[0].guideline_id == "no-force-push"


def test_pre_execution_allows_normal_git_push() -> None:
    auditor = _auditor_with_defaults()
    blocking = auditor.check_pre_execution("bash", {"command": "git push origin feature-branch"})
    assert blocking == []


def test_pre_execution_allows_normal_operations() -> None:
    auditor = _auditor_with_defaults()
    blocking = auditor.check_pre_execution("read_file", {"path": "/tmp/foo.txt"})
    assert blocking == []


def test_pre_execution_blocks_rm_rf_root() -> None:
    auditor = _auditor_with_defaults()
    blocking = auditor.check_pre_execution("bash", {"command": "rm -rf /"})
    assert len(blocking) == 1
    assert blocking[0].guideline_id == "no-rm-rf-root"
    assert blocking[0].severity == "block"


def test_post_execution_warns_on_secrets() -> None:
    auditor = _auditor_with_defaults()
    violations = auditor.check_post_execution(
        "run_command",
        "Output: api_key=sk-abc123xyz456789 some other text",
    )
    assert len(violations) == 1
    assert violations[0].guideline_id == "no-secret-in-stdout"
    assert violations[0].severity == "warning"
    assert violations[0].tool_name == "run_command"


def test_post_execution_no_violation_on_clean_output() -> None:
    auditor = _auditor_with_defaults()
    violations = auditor.check_post_execution("run_command", "Build succeeded in 2.3s")
    assert violations == []


def test_record_evidence_and_summary() -> None:
    auditor = _auditor_with_defaults()
    auditor.record_evidence("read_file", "read", "Read /tmp/config.yaml")
    auditor.record_evidence("write_file", "write", "Wrote /tmp/output.txt")
    summary = auditor.summary()
    assert summary["evidence_entries"] == 2
    assert summary["guidelines_checked"] == len(DEFAULT_GUIDELINES)
    assert summary["violations"] == 0
    assert summary["blocking_violations"] == 0
    assert summary["compliant"] is True


def test_summary_not_compliant_after_blocking_violation() -> None:
    auditor = _auditor_with_defaults()
    auditor.check_pre_execution("bash", {"command": "git push --force origin main"})
    summary = auditor.summary()
    assert summary["blocking_violations"] >= 1
    assert summary["compliant"] is False


def test_summary_compliant_with_only_warnings() -> None:
    auditor = _auditor_with_defaults()
    auditor.check_post_execution("bash", "password=hunter2x is visible in logs")
    summary = auditor.summary()
    assert summary["violations"] >= 1
    assert summary["blocking_violations"] == 0
    assert summary["compliant"] is True


def test_custom_guideline() -> None:
    auditor = GleanAuditor()
    auditor.add_guideline(
        Guideline(
            id="no-drop-table",
            description="Prevent DROP TABLE statements.",
            check="pre",
            pattern=r"(?i)drop\s+table",
            severity="error",
        )
    )
    blocking = auditor.check_pre_execution("sql", {"query": "DROP TABLE users"})
    # severity is "error", not "block", so blocking list should be empty
    assert blocking == []
    summary = auditor.summary()
    assert summary["violations"] == 1
    assert summary["blocking_violations"] == 0


def test_violation_dataclass_fields() -> None:
    v = GuidelineViolation(
        guideline_id="test-id",
        tool_name="bash",
        detail="some detail",
        severity="block",
    )
    assert v.guideline_id == "test-id"
    assert v.tool_name == "bash"
    assert v.severity == "block"
