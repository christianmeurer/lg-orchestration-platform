"""Tests for Wave 12 — streaming completeness in coder and reporter nodes."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_stream(*tokens: str) -> AsyncGenerator[str, None]:
    """Return an async generator that yields each token then stops."""
    async def _gen() -> AsyncGenerator[str, None]:
        for token in tokens:
            yield token

    return _gen()


def _base_state(*, run_id: str | None = "run-abc123") -> dict[str, Any]:
    """Minimal state with a run_id and planner model slot configured."""
    state: dict[str, Any] = {
        "request": "fix the bug",
        "plan": {
            "steps": [
                {
                    "id": "step-1",
                    "description": "patch the handler",
                    "expected_outcome": "tests pass",
                    "files_touched": ["src/handler.py"],
                    "handoff": {
                        "producer": "planner",
                        "consumer": "coder",
                        "objective": "Fix the handler",
                        "file_scope": ["src/handler.py"],
                        "evidence": [],
                        "constraints": [],
                        "acceptance_checks": [],
                        "retry_budget": 1,
                        "provenance": [],
                    },
                }
            ]
        },
        "_models": {
            "planner": {
                "provider": "openai_compatible",
                "model": "gpt-4o-mini",
                "temperature": 0.2,
            }
        },
        "_model_provider_runtime": {
            "openai_compatible": {
                "api_key": "sk-test",
                "base_url": "https://api.openai.com/v1",
                "timeout_s": 30,
            }
        },
    }
    if run_id is not None:
        state["run_id"] = run_id
    return state


# ---------------------------------------------------------------------------
# coder node streaming tests
# ---------------------------------------------------------------------------


class TestCoderStreaming:
    """coder node emits llm_chunk events during LLM synthesis."""

    def test_emits_three_chunks_with_node_coder(self) -> None:
        """Three-chunk mock stream must produce exactly three push_run_event calls."""
        state = _base_state(run_id="run-coder-01")

        tokens = ("Hello", " world", "!")

        async def _fake_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            for t in tokens:
                yield t

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _fake_stream

        with (
            patch("lg_orch.nodes.coder.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.coder.push_run_event") as mock_push,
        ):
            from lg_orch.nodes.coder import coder

            result = coder(state)

        assert mock_push.call_count == 3
        calls = mock_push.call_args_list
        for idx, token in enumerate(tokens):
            assert calls[idx] == call(
                "run-coder-01",
                {"type": "llm_chunk", "node": "coder", "delta": token},
            )

        # State must still be well-formed
        assert "active_handoff" in result

    def test_no_push_event_when_run_id_is_none(self) -> None:
        """When run_id is absent, no push_run_event calls are made."""
        state = _base_state(run_id=None)

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "patch guidance"
        mock_client.chat_completion.return_value = mock_response

        with (
            patch("lg_orch.nodes.coder.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.coder.push_run_event") as mock_push,
        ):
            from lg_orch.nodes.coder import coder

            result = coder(state)

        mock_push.assert_not_called()
        assert "active_handoff" in result

    def test_chunks_accumulate_into_full_text(self) -> None:
        """_stream_llm_with_events joins chunks into the full InferenceResponse.text."""
        tokens = ("line one\n", "line two\n", "line three")
        expected_full = "line one\nline two\nline three"

        async def _fake_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            for t in tokens:
                yield t

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _fake_stream

        with patch("lg_orch.nodes.coder.push_run_event"):
            from lg_orch.nodes.coder import _stream_llm_with_events

            response = _stream_llm_with_events(
                mock_client,
                model="gpt-4o-mini",
                system_prompt="sys",
                user_prompt="user",
                temperature=0.2,
                max_tokens=100,
                run_id="run-coder-02",
                node="coder",
            )

        assert response.text == expected_full

    def test_at_least_three_chunks_emitted(self) -> None:
        """Minimum 3 chunks must each produce an individual push_run_event call."""
        state = _base_state(run_id="run-coder-03")
        tokens = ("A", "B", "C", "D")

        async def _fake_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            for t in tokens:
                yield t

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _fake_stream

        with (
            patch("lg_orch.nodes.coder.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.coder.push_run_event") as mock_push,
        ):
            from lg_orch.nodes.coder import coder

            coder(state)

        assert mock_push.call_count >= 3

    def test_fallback_to_sync_when_streaming_raises(self) -> None:
        """When streaming raises, falls back to chat_completion (no events emitted from stream)."""
        state = _base_state(run_id="run-coder-04")

        async def _bad_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            raise RuntimeError("stream_error")
            yield  # make it a generator

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _bad_stream
        mock_response = MagicMock()
        mock_response.text = "fallback text"
        mock_client.chat_completion.return_value = mock_response

        with (
            patch("lg_orch.nodes.coder.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.coder.push_run_event") as mock_push,
        ):
            from lg_orch.nodes.coder import coder

            result = coder(state)

        # Fallback to sync: push_run_event never called
        mock_push.assert_not_called()
        mock_client.chat_completion.assert_called_once()
        assert "active_handoff" in result


# ---------------------------------------------------------------------------
# reporter node streaming tests
# ---------------------------------------------------------------------------


class TestReporterStreaming:
    """reporter node emits llm_chunk events during LLM synthesis."""

    def _reporter_state(self, *, run_id: str | None = "run-rep-01") -> dict[str, Any]:
        state: dict[str, Any] = {
            "request": "explain what changed",
            "tool_results": [
                {"tool": "run_tests", "stdout": "all tests pass", "stderr": "", "ok": True}
            ],
            "intent": "code_change",
            "repo_context": {"repo_root": "/repo", "top_level": ["src", "tests"]},
            "_models": {
                "planner": {
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "temperature": 0.3,
                }
            },
            "_model_provider_runtime": {
                "openai_compatible": {
                    "api_key": "sk-test",
                    "base_url": "https://api.openai.com/v1",
                    "timeout_s": 30,
                }
            },
        }
        if run_id is not None:
            state["run_id"] = run_id
        return state

    def test_emits_three_chunks_with_node_reporter(self) -> None:
        """Three-chunk mock stream must produce exactly three push_run_event calls."""
        state = self._reporter_state(run_id="run-rep-01")
        tokens = ("The fix", " applied", " successfully.")

        async def _fake_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            for t in tokens:
                yield t

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _fake_stream

        with (
            patch("lg_orch.nodes.reporter.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.reporter.push_run_event") as mock_push,
        ):
            from lg_orch.nodes.reporter import reporter

            result = reporter(state)

        assert mock_push.call_count == 3
        calls = mock_push.call_args_list
        for idx, token in enumerate(tokens):
            assert calls[idx] == call(
                "run-rep-01",
                {"type": "llm_chunk", "node": "reporter", "delta": token},
            )

        assert isinstance(result.get("final"), str)
        assert result["final"]

    def test_no_push_event_when_run_id_is_none(self) -> None:
        """When run_id is absent, no push_run_event calls are made."""
        state = self._reporter_state(run_id=None)

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "final report"
        mock_client.chat_completion.return_value = mock_response

        with (
            patch("lg_orch.nodes.reporter.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.reporter.push_run_event") as mock_push,
        ):
            from lg_orch.nodes.reporter import reporter

            result = reporter(state)

        mock_push.assert_not_called()
        assert result["final"] == "final report"

    def test_chunks_accumulate_into_full_text_for_final(self) -> None:
        """Accumulated chunks form the full `final` output stored in state."""
        state = self._reporter_state(run_id="run-rep-02")
        tokens = ("Part one. ", "Part two. ", "Part three.")
        expected = "Part one. Part two. Part three."

        async def _fake_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            for t in tokens:
                yield t

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _fake_stream

        with (
            patch("lg_orch.nodes.reporter.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.reporter.push_run_event"),
        ):
            from lg_orch.nodes.reporter import reporter

            result = reporter(state)

        assert result["final"] == expected

    def test_at_least_three_chunks_emitted(self) -> None:
        """Minimum 3 chunks each produce an individual push_run_event call."""
        state = self._reporter_state(run_id="run-rep-03")
        tokens = ("X", "Y", "Z", "W")

        async def _fake_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            for t in tokens:
                yield t

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _fake_stream

        with (
            patch("lg_orch.nodes.reporter.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.reporter.push_run_event") as mock_push,
        ):
            from lg_orch.nodes.reporter import reporter

            reporter(state)

        assert mock_push.call_count >= 3

    def test_fallback_to_sync_when_streaming_raises(self) -> None:
        """When streaming raises, falls back to chat_completion."""
        state = self._reporter_state(run_id="run-rep-04")

        async def _bad_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            raise RuntimeError("stream_error")
            yield

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _bad_stream
        mock_response = MagicMock()
        mock_response.text = "fallback report"
        mock_client.chat_completion.return_value = mock_response

        with (
            patch("lg_orch.nodes.reporter.InferenceClient", return_value=mock_client),
            patch("lg_orch.nodes.reporter.push_run_event") as mock_push,
        ):
            from lg_orch.nodes.reporter import reporter

            result = reporter(state)

        mock_push.assert_not_called()
        mock_client.chat_completion.assert_called_once()
        assert result["final"] == "fallback report"

    def test_structured_summary_used_when_no_model_configured(self) -> None:
        """When provider is 'local', structured summary is used without LLM call."""
        state = self._reporter_state(run_id="run-rep-05")
        state["_models"] = {"planner": {"provider": "local", "model": ""}}

        with patch("lg_orch.nodes.reporter.push_run_event") as mock_push:
            from lg_orch.nodes.reporter import reporter

            result = reporter(state)

        mock_push.assert_not_called()
        assert "intent" in result["final"]


# ---------------------------------------------------------------------------
# _stream_llm_with_events unit tests (shared helper)
# ---------------------------------------------------------------------------


class TestStreamLlmWithEvents:
    """Direct unit tests for the _stream_llm_with_events helper in coder."""

    def test_returns_inference_response_with_joined_text(self) -> None:
        tokens = ("foo", "bar", "baz")

        async def _fake_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            for t in tokens:
                yield t

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _fake_stream

        with patch("lg_orch.nodes.coder.push_run_event"):
            from lg_orch.nodes.coder import _stream_llm_with_events

            resp = _stream_llm_with_events(
                mock_client,
                model="m",
                system_prompt="s",
                user_prompt="u",
                temperature=0.0,
                max_tokens=100,
                run_id="run-x",
                node="coder",
            )

        assert resp.text == "foobarbaz"
        assert resp.model == "m"

    def test_emits_correct_event_payload_per_chunk(self) -> None:
        tokens = ("T1", "T2", "T3")

        async def _fake_stream(**kwargs: Any) -> AsyncGenerator[str, None]:  # type: ignore[override]
            for t in tokens:
                yield t

        mock_client = MagicMock()
        mock_client.chat_completion_stream = _fake_stream

        with patch("lg_orch.nodes.coder.push_run_event") as mock_push:
            from lg_orch.nodes.coder import _stream_llm_with_events

            _stream_llm_with_events(
                mock_client,
                model="m",
                system_prompt="s",
                user_prompt="u",
                temperature=0.0,
                max_tokens=100,
                run_id="run-y",
                node="coder",
            )

        assert mock_push.call_count == 3
        for idx, token in enumerate(tokens):
            assert mock_push.call_args_list[idx] == call(
                "run-y",
                {"type": "llm_chunk", "node": "coder", "delta": token},
            )
