# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# HIGH FIX 4: Allowlist of binaries permitted as test commands to prevent
# command injection from malicious repository package.json scripts.
_ALLOWED_TEST_COMMANDS = {"pytest", "python", "uv", "npm", "yarn", "cargo", "make", "go"}


def _validate_test_command(cmd: str) -> bool:
    """Validate that the test command starts with an allowed binary."""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts:
        return False
    binary = os.path.basename(parts[0])
    return binary in _ALLOWED_TEST_COMMANDS


def detect_test_runner(root_dir: str | Path) -> str:
    """Detect the appropriate test runner based on project files present in root_dir."""
    root = Path(root_dir)
    if (root / "Cargo.toml").exists():
        return "cargo test --all"
    if (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text())
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                return "npm test"
        except (json.JSONDecodeError, OSError):
            pass
        return "npm test"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (
        (root / "pyproject.toml").exists()
        or (root / "pytest.ini").exists()
        or (root / "setup.cfg").exists()
    ):
        return "python -m pytest"
    if (root / "Makefile").exists():
        return "make test"
    return "python -m pytest"


_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")
_ERROR_RE = re.compile(r"(\d+)\s+error")
_FAILED_LINE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)

_OUTPUT_TRUNCATE_CHARS = 4000


@dataclass
class TestSuiteResult:
    __test__ = False  # prevent pytest from collecting this as a test class

    run_id: str
    repo_path: str
    passed: int
    failed: int
    errors: int
    failed_tests: list[str]
    output: str
    timestamp: float


@dataclass
class HealingJob:
    job_id: str
    repo_path: str
    failing_tests: list[str]
    priority: int
    created_at: float
    status: Literal["queued", "running", "healed", "failed", "skipped"]


class HealingLoop:
    """Continuous monitoring loop.

    Polls a repo's test suite on a configurable interval.
    On failure, creates a HealingJob and triggers graph execution.
    Tracks healing history.
    """

    def __init__(
        self,
        repo_path: str,
        poll_interval_seconds: float = 60.0,
        max_concurrent_jobs: int = 2,
        graph_runner: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        test_runner: str | None = None,
    ) -> None:
        self._repo_path = repo_path
        self._poll_interval = poll_interval_seconds
        self._max_concurrent_jobs = max_concurrent_jobs
        self._graph_runner = graph_runner
        self.test_runner_cmd: str = (
            test_runner if test_runner is not None else detect_test_runner(repo_path)
        )
        self._job_history: list[HealingJob] = []
        self._pending_jobs: list[HealingJob] = []
        self._lock = asyncio.Lock()

    async def poll_once(self) -> TestSuiteResult:
        """Run pytest in repo_path subprocess; return TestSuiteResult."""
        run_id = uuid.uuid4().hex
        timestamp = time.time()

        # HIGH FIX 4: Validate test command against allowlist before executing
        if not _validate_test_command(self.test_runner_cmd):
            logging.warning("healing_loop_blocked_unsafe_test_command: %s", self.test_runner_cmd)
            return TestSuiteResult(
                run_id=run_id,
                repo_path=self._repo_path,
                passed=0,
                failed=0,
                errors=1,
                failed_tests=[],
                output=f"Blocked unsafe test command: {self.test_runner_cmd}",
                timestamp=timestamp,
            )

        try:
            cmd_parts = shlex.split(self.test_runner_cmd)
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                cwd=self._repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout_bytes, _ = await proc.communicate()
            raw_output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            exit_code = proc.returncode if proc.returncode is not None else 0
        except Exception as exc:
            raw_output = str(exc)
            exit_code = 2

        output = raw_output[:_OUTPUT_TRUNCATE_CHARS]

        passed = 0
        failed = 0
        errors = 0

        passed_match = _PASSED_RE.search(raw_output)
        if passed_match:
            passed = int(passed_match.group(1))

        failed_match = _FAILED_RE.search(raw_output)
        if failed_match:
            failed = int(failed_match.group(1))

        error_match = _ERROR_RE.search(raw_output)
        if error_match:
            errors = int(error_match.group(1))

        # exit code 2 means internal pytest error; count as error
        if exit_code == 2 and errors == 0 and failed == 0:
            errors = 1

        failed_tests = _FAILED_LINE_RE.findall(raw_output)

        return TestSuiteResult(
            run_id=run_id,
            repo_path=self._repo_path,
            passed=passed,
            failed=failed,
            errors=errors,
            failed_tests=failed_tests,
            output=output,
            timestamp=timestamp,
        )

    async def run_until_cancelled(self) -> None:
        """Main loop: poll, detect failures, enqueue HealingJobs, dispatch graph_runner."""
        try:
            while True:
                result = await self.poll_once()

                if result.failed > 0:
                    job = HealingJob(
                        job_id=uuid.uuid4().hex,
                        repo_path=self._repo_path,
                        failing_tests=list(result.failed_tests),
                        priority=1,
                        created_at=time.time(),
                        status="queued",
                    )
                    async with self._lock:
                        self._pending_jobs.append(job)
                        self._job_history.append(job)

                await self._dispatch_pending_jobs()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            return

    async def _dispatch_pending_jobs(self) -> None:
        async with self._lock:
            batch = self._pending_jobs[: self._max_concurrent_jobs]
            self._pending_jobs = self._pending_jobs[self._max_concurrent_jobs :]
            for job in batch:
                job.status = "running"

        if not batch:
            return

        async with asyncio.TaskGroup() as tg:
            for job in batch:
                tg.create_task(self._run_job(job))

    async def _run_job(self, job: HealingJob) -> None:
        if self._graph_runner is None:
            job.status = "skipped"
            return
        try:
            result = await self._graph_runner(
                {
                    "task": "Fix failing tests",
                    "healing_context": {
                        "job_id": job.job_id,
                        "failing_tests": list(job.failing_tests),
                        "failure_class": "test_failure",
                        "repo_path": job.repo_path,
                    },
                    "repo_path": job.repo_path,
                    "healing_job_id": job.job_id,
                }
            )
            # Check if healing actually succeeded via verification
            verification = result.get("verification", {}) if isinstance(result, dict) else {}
            if isinstance(verification, dict) and verification.get("ok", False):
                job.status = "healed"
            else:
                job.status = "failed"
                logging.warning(
                    "Healing job %s completed but verification did not pass",
                    job.job_id,
                )
        except Exception:
            job.status = "failed"

    def get_job_history(self) -> list[HealingJob]:
        return list(self._job_history)

    def get_pending_jobs(self) -> list[HealingJob]:
        return list(self._pending_jobs)
