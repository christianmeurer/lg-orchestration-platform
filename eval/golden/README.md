# eval/golden/

Golden files define the expected outputs used for pass-rate benchmarking. Each JSON file
corresponds to one eval task (keyed by task name, matching the task `id` prefix) and declares the
assertions that `eval/run.py` must verify against the agent's actual output.

## Assertion operators

| Operator   | Meaning                                      |
|------------|----------------------------------------------|
| `eq`       | Exact equality (`actual == value`)           |
| `lte`      | Less-than-or-equal (`actual <= value`)       |
| `gte`      | Greater-than-or-equal (`actual >= value`)    |
| `in`       | Membership (`actual in value`)               |
| `contains` | Substring or list containment                |

## Loading convention

`eval/run.py` loads golden files from `eval/golden/<task-id>.json`, where `task-id` matches the
`id` field in the corresponding `eval/tasks/*.json` file (with the numeric suffix dropped for
multi-instance tasks). Each golden file's `assertions` list is evaluated in order; all assertions
must pass for the task to be marked green.
