from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvalTask:
    id: str
    request: str
    expected_intent: str


def load_tasks(tasks_dir: Path) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    for path in sorted(tasks_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks.append(
            EvalTask(
                id=str(data["id"]),
                request=str(data["request"]),
                expected_intent=str(data["expected_intent"]),
            )
        )
    return tasks


def main() -> None:
    tasks = load_tasks(Path(__file__).parent / "tasks")
    if not tasks:
        raise SystemExit("no tasks")
    for t in tasks:
        if not t.id or not t.request or not t.expected_intent:
            raise SystemExit(f"invalid task: {t}")
    print(f"ok: loaded {len(tasks)} task(s)")


if __name__ == "__main__":
    main()

