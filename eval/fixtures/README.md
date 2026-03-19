# eval/fixtures/

Fixture directories provide deterministic, static file sets that eval tasks reference as their
starting repository state. Each subdirectory represents one scenario and is named to match the
corresponding task ID prefix (e.g. `canary/`, `test-repair/`, `real-world-repair/`,
`approval-flow/`).

## Path convention

A task JSON references its fixture via the implicit convention:

    eval/fixtures/<scenario-slug>/

The eval runner resolves the fixture root relative to the repository root. Tasks that need a
specific file pass `target_file` as a path relative to the fixture root.

## Adding a new fixture

1. Create `eval/fixtures/<your-scenario>/` with a `README.md` describing the scenario.
2. Populate source files under `src/` and test files under `tests/` as needed.
3. Keep fixtures minimal and self-contained — no external dependencies.
4. Register a matching task JSON under `eval/tasks/` and a golden file under `eval/golden/`.
