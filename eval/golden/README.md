# eval/golden/

Golden files define the expected outputs used for pass-rate benchmarking. Each JSON file
corresponds to one eval task (keyed by task name, matching the task `id` prefix) and declares the
assertions that `eval/run.py` must verify against the agent's actual output.

## File naming convention

Each golden file is named `<base-task-id>.json` where the base ID is the task `id` with any
trailing numeric instance suffix removed (e.g. task `test-repair-001` maps to `test-repair.json`).

## `_comment` convention

Every golden file should include a top-level `"_comment"` string explaining what the scenario
tests. This is documentation for future maintainers and is ignored by the assertion runner.

```json
{
  "_comment": "What this scenario validates and any known constraints.",
  "task": "my-task",
  "assertions": [...]
}
```

## Task file formats

Task files in `eval/tasks/` support two formats:

### Single-task format
A plain JSON object with `id`, `request`, and `expected_intent` at the top level:

```json
{
  "id": "canary-001",
  "request": "Summarize the repository.",
  "expected_intent": "analysis"
}
```

### Multi-task format
A top-level `{"tasks": [...]}` wrapper containing an array of individual task objects. Each inner
task must have its own `id` and `request`. Fields absent from inner tasks receive the same defaults
as single-task files (`expected_acceptance_ok: true`, `budget_max_loops: 1`, etc.):

```json
{
  "description": "Batch of repair benchmarks.",
  "schema_version": 1,
  "tasks": [
    {"id": "repair-001", "request": "Fix the off-by-one bug in memory.py.", "expected_intent": "code_change"},
    {"id": "repair-002", "request": "Fix the null-safety issue in reporter.py.", "expected_intent": "code_change"}
  ]
}
```

`load_tasks()` in `eval/run.py` handles both formats transparently.

## Assertion operators

| Operator   | Meaning                                        |
|------------|------------------------------------------------|
| `eq`       | Exact equality (`actual == value`)             |
| `ne`       | Inequality (`actual != value`)                 |
| `lte`      | Less-than-or-equal (`actual <= value`, numeric)|
| `gte`      | Greater-than-or-equal (`actual >= value`, numeric)|
| `in`       | Membership (`actual in value`, scalar in list) |
| `contains` | Containment (`value in actual`, list/string)   |

Both `"path"` and `"field"` are accepted as the assertion key name (legacy files may use `"field"`).

## Valid assertion targets (graph output fields)

The following fields are emitted by the graph and are safe to assert against:

| Path | Type | Notes |
|------|------|-------|
| `intent` | string | One of: `code_change`, `analysis`, `research`, `question`, `refactor`, `debug` |
| `route.lane` | string | One of: `interactive`, `deep_planning`, `recovery` |
| `halt_reason` | string | `""` (normal), `"max_loops_exhausted"`, `"accepted"` |
| `final` | string | Non-empty when `require_final: true` and graph completed |
| `status` | string | `"suspended"` when pending approval |
| `pending_approval` | boolean | `true` when graph halted awaiting operator action |
| `verification.ok` | boolean | Whether the verifier passed |
| `verification.acceptance_ok` | boolean | Whether all acceptance criteria were met |
| `verification.failure_class` | string | Failure classification (empty string when ok) |
| `loop_count` | integer | Number of healing loops executed |
| `acceptance_ok` | boolean | Alias from `verification.acceptance_ok` used in score_task |

**Do not** assert on `verifier_status`, `patch_applied`, `tests_passed`, or
`post_apply_pytest_pass` — these fields are not emitted by the current graph.

## Loading convention

`eval/run.py` loads golden files from `eval/golden/<task-id>.json`, where `task-id` matches the
`id` field in the corresponding `eval/tasks/*.json` file (with the numeric suffix dropped for
multi-instance tasks). Each golden file's `assertions` list is evaluated in order; all assertions
must pass for the task to be marked green.

---

## Wave D — Extended Eval Capabilities

### `--swe-bench PATH` flag and JSONL format

Pass a SWE-bench JSONL file (one task object per line) with `--swe-bench`:

```bash
python eval/run.py --swe-bench path/to/swe_bench_lite.jsonl --swe-bench-limit 50
```

Each line must be a JSON object with at least `instance_id`, `problem_statement`, and `patch`
fields. The loader maps these to Lula's internal task format (`id`, `request`, `expected_intent`).
`--swe-bench-limit N` caps the number of tasks loaded (useful for fast iteration). The SWE-bench
loader and the existing `--task` path are mutually exclusive per invocation.

### `resolved_rate` metric in summary output

The eval summary table now includes a `resolved_rate` column alongside `pass@k`:

```
benchmark            tasks   resolved   pass@1   resolved_rate
real_world_repair    10      4          0.60     0.40
swe_bench_lite       50      18         0.52     0.36
```

`resolved_rate = resolved / total` where a task is "resolved" if all golden assertions pass **and**
`verification.acceptance_ok` is `true`. Nightly CI enforces a minimum `resolved_rate` of `0.30`
on `real_world_repair.json`; the job fails if the threshold is not met.

### `--dry-run` flag for task list preview

```bash
python eval/run.py --task eval/tasks/real_world_repair.json --dry-run
```

Prints the resolved task list (IDs, requests, golden file paths, fixture paths) to stdout without
invoking the LangGraph graph. Use this to verify loader output and confirm fixture availability
before committing to a full eval run.

### Benchmark class grouping in `pass@k` tables

Tasks that carry a `"class"` field in their task definition (e.g. `"class": "repair"`) are grouped
in the `pass@k` summary table so per-class pass rates are visible alongside the aggregate:

```
class       tasks   pass@1
repair      6       0.67
analysis    3       1.00
refactor    1       0.00
(all)       10      0.70
```

The `"class"` field is optional; tasks without it are grouped under `"(unclassified)"` in the
grouped view.
