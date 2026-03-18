# Lula agent collaboration: current state and SOTA 2026 direction

## Current state in this repository

Lula already has a strong staged orchestration pipeline, but it is still mostly a **single-run specialist pipeline**, not a fully collaborative multi-agent system.

The current main runtime is built around the node chain documented in [`py/src/lg_orch/graph.py`](../py/src/lg_orch/graph.py): router, planner, executor, verifier, and reporter.

### What is already strong

- [`py/src/lg_orch/nodes/router.py`](../py/src/lg_orch/nodes/router.py) is a real routing agent. It decides lane, context scope, cache affinity, and recovery posture.
- [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py) is a real planning agent. It emits a bounded structured plan with steps, verification calls, rollback guidance, and recovery metadata.
- [`py/src/lg_orch/nodes/executor.py`](../py/src/lg_orch/nodes/executor.py) is deterministic. This is a design strength: tool execution is not delegated back to a free-form LLM.
- [`py/src/lg_orch/nodes/verifier.py`](../py/src/lg_orch/nodes/verifier.py) already behaves like a recovery critic. It classifies failure types, emits recovery packets, and feeds the next loop.
- [`py/src/lg_orch/memory.py`](../py/src/lg_orch/memory.py) already gives the system a collaboration substrate through stable-prefix context, working-set context, compression provenance, and loop summaries.

### What is missing or only partial

- There is **no explicit coder agent** today. Patch intent is still primarily produced by the planner, and the executor only dispatches tools. Lula has planner and verifier specialization, but not a dedicated code-synthesis specialist.
- There is **no true supervisor or scheduler agent** above the graph. The experimental multi-repository path in [`py/src/lg_orch/meta_graph.py`](../py/src/lg_orch/meta_graph.py) is still a placeholder and does not yet provide dependency-aware delegation, artifact routing, or conflict control.
- There is **no shared handoff contract** beyond state fields. Recovery packets are good, but there is not yet a uniform cross-agent artifact envelope for plans, diffs, critiques, and acceptance evidence.
- There is **no file-ownership or branch-isolation protocol** for multiple agents operating concurrently.

## Practical interpretation

Lula today is best understood as:

1. a **router agent**,
2. a **planner agent**,
3. a **deterministic tool executor**,
4. a **verifier / recovery critic**,
5. and a **reporter**.

That is already a serious architecture. The next gain does not come from adding many free-form agents. It comes from turning this pipeline into a **contract-first collaborative system**.

## SOTA 2026 agent topology for Lula

The most effective 2026 design for Lula is not “many agents talking freely.” It is a **small set of specialized agents with strict handoff contracts**.

### 1. Supervisor / topology agent

Keep [`py/src/lg_orch/nodes/router.py`](../py/src/lg_orch/nodes/router.py) focused on more than model choice.

Its future role should be:

- choose the lane,
- choose the collaboration topology,
- decide whether the run is single-path or multi-branch,
- decide whether a coder, critic, or retrieval-heavy pass is needed,
- and assign risk level for approval and verification depth.

This agent should schedule *who works next*, not just *which model to call next*.

### 2. Planner agent

Keep [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py) as the contract author.

Its job should be to emit:

- file-scoped tasks,
- acceptance criteria,
- verification requirements,
- retry budget,
- and handoff packets for downstream specialists.

The planner should not try to be the coder, the verifier, and the scheduler at once.

### 3. Explicit coder agent

Add a dedicated coder node between planner and executor.

That agent should:

- consume a planner step,
- stay within declared file scope,
- produce patch proposals or patch-ready payloads,
- attach rationale and touched-symbol metadata,
- and return unfinished work as a structured request rather than improvising outside scope.

This is the largest missing specialist role in the current Lula graph.

### 4. Verifier / critic agent

Keep [`py/src/lg_orch/nodes/verifier.py`](../py/src/lg_orch/nodes/verifier.py) as a first-class critic, but deepen the contract.

Its output should remain machine-readable and should expand to include:

- exact failing contract,
- failure fingerprint,
- evidence snippets,
- likely repair class,
- and whether the next hop is planner, coder, or context builder.

The verifier should be the agent that decides **what kind of repair loop is justified**.

### 5. Memory broker

The collaboration layer should rely on [`py/src/lg_orch/memory.py`](../py/src/lg_orch/memory.py), not on raw transcript replay.

The memory broker pattern should maintain:

- stable repo facts,
- loop-local working evidence,
- episodic failures and successful repairs,
- procedural playbooks,
- and compressed handoff packets for each specialist.

This is how agents stay efficient instead of re-reading the same world state.

### 6. Governance / approval agent

Lula already has the right enforcement foundation in the Rust runner. The next step is to treat approval as an explicit collaborative role rather than only a tool error condition.

That governance layer should own:

- approval state,
- risky mutation review,
- resume semantics,
- actor identity,
- and audit-grade decision records.

## Collaboration rules that actually work

### Contract-first handoffs

Every agent handoff should contain:

- objective,
- file scope,
- inspected evidence,
- constraints,
- success criteria,
- and next expected action.

This is the same pattern that now exists in a lighter form through recovery packets in [`py/src/lg_orch/nodes/verifier.py`](../py/src/lg_orch/nodes/verifier.py).

### Bounded authority

- router decides topology,
- planner decides task graph,
- coder proposes code,
- executor runs deterministic tools,
- verifier decides repair class,
- governance decides risky mutations.

Do not let one agent silently absorb three roles.

### Artifact envelopes instead of chat transcripts

Agents should exchange structured artifacts:

- route decision,
- task contract,
- patch proposal,
- verification report,
- recovery packet,
- loop summary.

This keeps collaboration auditable and cheap.

### File leases and branch isolation

If Lula later enables parallel branches, agents must not share mutable file scope blindly.

The correct model is:

- per-agent file leases,
- or per-agent git worktrees,
- followed by deterministic merge / reconcile steps.

That is the missing operational half of [`py/src/lg_orch/meta_graph.py`](../py/src/lg_orch/meta_graph.py).

### Recovery packets as shared memory

The current recovery loop is one of Lula's strongest starting points. Recovery packets should become the main collaboration primitive for failed branches, not an afterthought.

## Recommended near-term Lula implementation order

### 1. Add an explicit coder node

Add a coder node between planner and executor so the planner stops carrying patch synthesis implicitly.

Primary files:

- [`py/src/lg_orch/graph.py`](../py/src/lg_orch/graph.py)
- [`py/src/lg_orch/state.py`](../py/src/lg_orch/state.py)
- [`py/src/lg_orch/nodes/planner.py`](../py/src/lg_orch/nodes/planner.py)
- new `py/src/lg_orch/nodes/coder.py`

### 2. Introduce a handoff schema

Define a structured artifact for agent-to-agent collaboration.

Minimum fields:

- producer,
- consumer,
- objective,
- file_scope,
- evidence,
- constraints,
- acceptance_checks,
- retry_budget,
- provenance.

### 3. Upgrade the meta graph into a scheduler

Evolve [`py/src/lg_orch/meta_graph.py`](../py/src/lg_orch/meta_graph.py) from placeholder fan-out into a dependency-aware scheduler with explicit completion and failure edges.

### 4. Separate critique from synthesis more aggressively

Use stronger synthesis for planner or coder, and smaller faster critique for verifier triage where possible. Lula already has the lane concepts to support this.

### 5. Keep execution deterministic

Do not collapse executor responsibility back into the LLM stack. Lula's separation between LLM reasoning and Rust execution is one of its best architectural choices.

### 6. Pair collaborative agents with governed autonomy

The collaboration plan should not be implemented in isolation from operator control.

Lula already has approval enforcement primitives in the Rust runner, and the current repository now includes the first governed execution loop across the API and client surfaces. The practical implication is:

- planner, coder, verifier, and router collaboration should produce clearer specialist boundaries,
- while the control plane now makes risky mutations pausable, resumable, and auditable and should be refined further rather than introduced from scratch.

That means the correct near-term pairing is:

1. **explicit collaboration contracts** between specialists,
2. **approval-backed suspend/resume control** for risky steps,
3. **durable audit artifacts** shared across the API, SPA, and VS Code extension.

This pairing is stronger than either direction on its own:

- collaboration without governance still leaves risky autonomy weakly controlled,
- governance without explicit specialists still governs a planner-heavy monolith rather than a disciplined multi-role system.

## Bottom line

The current Lula graph already contains the seeds of an effective collaborative agent system. The most valuable next move is not “add many agents.” It is:

1. add one explicit coder specialist,
2. formalize handoffs,
3. promote recovery packets into a general collaboration contract,
4. pair that collaboration model with governed-autonomy controls for approval, suspend/resume, and auditability,
5. and only then grow toward dependency-aware multi-agent scheduling.

That path is both safer and more likely to produce real 2026-grade performance than a premature free-form multi-agent design.
