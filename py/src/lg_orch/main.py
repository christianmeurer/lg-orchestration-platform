from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lg_orch.config import load_config
from lg_orch.graph import build_graph, export_mermaid
from lg_orch.logging import configure_logging, get_logger
from lg_orch.trace import write_run_trace


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lg-orch")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("request")
    run_p.add_argument("--profile", default=None)
    run_p.add_argument("--runner-base-url", default=None)
    run_p.add_argument("--trace", action="store_true")

    sub.add_parser("export-graph")
    return p


def cli(argv: list[str] | None = None) -> int:
    configure_logging()
    log = get_logger()
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[3]

    if args.cmd == "export-graph":
        sys.stdout.write(export_mermaid())
        return 0

    cfg = load_config(repo_root=repo_root)
    if args.profile:
        import os

        os.environ["LG_PROFILE"] = str(args.profile)
        cfg = load_config(repo_root=repo_root)

    runner_base_url = args.runner_base_url or cfg.runner.base_url
    trace_enabled = bool(args.trace) or cfg.trace.enabled

    app = build_graph()
    state = {
        "request": str(args.request),
        "_repo_root": str(repo_root),
        "_runner_base_url": runner_base_url,
        "_budget_max_loops": cfg.budgets.max_loops,
        "_config_policy": {
            "network_default": cfg.policy.network_default,
            "require_approval_for_mutations": cfg.policy.require_approval_for_mutations,
        },
        "_trace_enabled": trace_enabled,
        "_trace_out_dir": cfg.trace.output_dir,
    }
    out = app.invoke(state)
    sys.stdout.write(str(out.get("final", "")) + "\n")

    log.info(
        "run_complete",
        intent=out.get("intent"),
        runner_enabled=bool(out.get("_runner_enabled", True)),
        trace_enabled=bool(out.get("_trace_enabled", False)),
        tool_results=len(list(out.get("tool_results", []))),
    )

    if bool(out.get("_trace_enabled", False)) is True:
        trace_path = write_run_trace(
            repo_root=repo_root,
            out_dir=Path(str(out.get("_trace_out_dir", "artifacts/runs"))),
            state=out,
        )
        log.info("trace_written", path=str(trace_path))
    return 0


def main(argv: list[str]) -> int:
    return cli(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
