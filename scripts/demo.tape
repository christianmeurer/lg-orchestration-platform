# Lula Agent Demo — VHS tape script
# Generate with: vhs scripts/demo.tape
# Requires: https://github.com/charmbracelet/vhs

Output docs/demo.gif

Set Width 120
Set Height 32
Set FontSize 14
Set Theme "Dracula"
Set TypingSpeed 40ms

Sleep 500ms

Type "# Lula — autonomous coding agent demo"
Enter
Sleep 800ms

Type "cd py && uv run python -m lg_orch run --task 'Fix the off-by-one error in pagination logic' --repo ../demo-app"
Enter
Sleep 1500ms

# Orchestrator startup
Type ""
Sleep 300ms
Output docs/demo.gif

# Simulated orchestrator output
Type ""
Sleep 200ms

# -- Ingest phase --
SetTypingSpeed 0
Type "▶ [ingest] Loading repository ../demo-app"
Enter
Sleep 400ms
Type "  repo: demo-app  files: 47  lang: python  size: 128 KB"
Enter
Sleep 300ms
Type "  SCIP index: 312 symbols resolved"
Enter
Sleep 500ms

# -- Router phase --
Type "▶ [router] Classifying task..."
Enter
Sleep 400ms
Type "  route: interactive  confidence: 0.91  model: gpt-4o"
Enter
Sleep 500ms

# -- Planner phase --
Type "▶ [planner] Generating execution plan"
Enter
Sleep 600ms
Type "  ┌─────────────────────────────────────────────────────────┐"
Enter
Type "  │ Step 1: read_file  src/pagination.py                    │"
Enter
Type "  │ Step 2: exec       uv run pytest tests/test_paginate.py │"
Enter
Type "  │ Step 3: apply_patch src/pagination.py (fix off-by-one)  │"
Enter
Type "  │ Step 4: exec       uv run pytest tests/test_paginate.py │"
Enter
Type "  └─────────────────────────────────────────────────────────┘"
Enter
Sleep 800ms

# -- Executor phase --
Type "▶ [executor] Dispatching to Rust runner (sandbox: LinuxNamespace)"
Enter
Sleep 400ms
Type "  → read_file src/pagination.py ... 200 OK (1.2 ms)"
Enter
Sleep 300ms
Type "  → exec uv run pytest tests/test_paginate.py"
Enter
Sleep 600ms
Type "    FAILED tests/test_paginate.py::test_last_page - assert 11 == 10"
Enter
Type "    1 failed, 7 passed in 0.43s"
Enter
Sleep 500ms
Type "  → apply_patch src/pagination.py"
Enter
Sleep 300ms
Type "    ⚠ ApprovalRequired (428) — mutation requires human approval"
Enter
Sleep 800ms

# -- Approval gate --
Type "▶ [approval] Awaiting operator approval..."
Enter
Sleep 1200ms
Type "  ✓ Approved by operator@lula (HMAC token validated, nonce: a3f8c2)"
Enter
Sleep 500ms
Type "  → apply_patch src/pagination.py ... 200 OK"
Enter
Sleep 300ms
Type "    @@ -42,1 +42,1 @@"
Enter
Type "    -    end = min(offset + limit, total_items + 1)"
Enter
Type "    +    end = min(offset + limit, total_items)"
Enter
Sleep 500ms
Type "  → exec uv run pytest tests/test_paginate.py"
Enter
Sleep 600ms
Type "    8 passed in 0.39s"
Enter
Sleep 500ms

# -- Verifier phase --
Type "▶ [verifier] Checking acceptance criteria"
Enter
Sleep 500ms
Type "  tests_pass: true  patch_size: 1 hunk  invariants: OK"
Enter
Type "  verdict: PASS"
Enter
Sleep 600ms

# -- Reporter phase --
Type "▶ [reporter] Run complete"
Enter
Sleep 300ms
Type "  ┌────────────────────────────────────┐"
Enter
Type "  │ run_id:   r-20260323-ab12cd34      │"
Enter
Type "  │ status:   success                   │"
Enter
Type "  │ loops:    1 / 3                     │"
Enter
Type "  │ tools:    4 calls (1 mutation)      │"
Enter
Type "  │ sandbox:  LinuxNamespace             │"
Enter
Type "  │ duration: 3.7s                      │"
Enter
Type "  └────────────────────────────────────┘"
Enter
Sleep 1500ms

SetTypingSpeed 40ms
Type ""
Enter
Sleep 500ms
Type "# Done. Patch applied and verified."
Enter
Sleep 2000ms
