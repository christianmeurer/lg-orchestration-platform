# SOTA 2026 Plan: Enterprise Agentic Behavior

This document outlines the next steps and strategic roadmap to transform the LG Orchestration Platform into a truly production-grade enterprise solution with advanced agentic behavior, comparable to tools like Roo Code, Claude Code, and Aider.

## 1. Advanced Agentic Loop & Reflection

Currently, the orchestration graph (`py/src/lg_orch/graph.py`) is primarily a linear pipeline (`planner -> executor -> verifier -> reporter`). To achieve true autonomy, we must implement an iterative agentic loop.

- **Iterative Refinement:** Add a conditional edge from `verifier` back to `planner` or `context_builder`. If verification (linting, testing) fails, the agent must analyze the `stderr`/failure report, reflect on its mistakes, and generate a new plan or patch to fix the issue.
- **Self-Correction & Bounded Autonomy:** Implement strict bounds on these loops (`max_loops` budget in `policy_gate`) to prevent infinite looping.
- **Dynamic Re-planning:** Allow the agent to discard its current plan and re-plan if it discovers that the initial context was insufficient or incorrect after executing a tool (e.g., reading a file reveals a completely different architecture than expected).

## 2. Context Management & Repository Mapping

Agentic coding tools like Roo Code excel because they understand the repository at a holistic level without blowing up the context window.

- **Repository Map Generation:** Integrate tools (like `ctags` or tree-sitter based AST parsing) to generate a concise, structural map of the repository. This map should be injected into the `context_builder` so the agent knows what files exist and what symbols they contain without reading the entire codebase.
- **Semantic Search (RAG):** Implement a local vector store (e.g., SQLite FTS or a lightweight embedded vector DB) to allow the agent to semantic-search the codebase. Provide a `search_codebase` tool.
- **Context Pruning:** Implement token counting and automatic context pruning. As the conversation and tool execution history grows, older or less relevant tool outputs (like large file reads) should be summarized or dropped.

## 3. Tool Extensibility via MCP (Model Context Protocol)

To be a top-tier enterprise solution, the platform must integrate seamlessly with internal company systems (Jira, Confluence, internal APIs) without hardcoding every tool.

- **MCP Integration:** Adopt the Model Context Protocol (MCP). The Rust runner should act as an MCP client, discovering and executing tools provided by MCP servers.
- **Dynamic Tool Discovery:** The `planner` should be aware of dynamically available MCP tools (e.g., `query_jira`, `read_confluence`, `github_pr_create`) and utilize them in its plans.

## 4. Enhanced Human-in-the-Loop (HITL) & Streaming

Enterprise adoption requires strict oversight and excellent user experience.

- **Interactive Streaming:** The CLI (or future UI) must stream the agent's thought process, tool execution progress, and verifier outputs in real-time. Instead of waiting for a batch to finish, users should see the agent "working."
- **Approval Gates:** Integrate a human approval step before destructive operations. The `apply_patch` tool in the Rust runner should trigger a prompt for the user if the `approval_required` policy is active for the requested paths.
- **Conversational Refinement:** Allow the user to interject during the `verifier` or `planner` stages to guide the agent (e.g., "Actually, don't use the `requests` library, use `httpx`").

## 5. Web Interactions & Visual Validation

- **Sandboxed Browser Tools:** Add Playwright/Puppeteer tools to the Rust runner to allow the agent to read web documentation, search the internet for solutions (StackOverflow, GitHub issues), and visually validate UI changes for web projects.

## 6. Durable Execution & Time-Travel Debugging

- **Persistent Checkpointing:** Upgrade the LangGraph checkpointing to use a durable store (Postgres or SQLite).
- **Resumability & "Undo":** Because every tool interaction and state transition is checkpointed, provide an `undo` command that rolls back the agent's state (and filesystem changes via inverse patches) to a previous graph node.

## Roadmap Summary

**Phase 1: Agentic Loop & Reflection**
- Implement cyclic graph (Verifier -> Planner).
- Add `max_loops` policy enforcement.

**Phase 2: Context & Repo Mapping**
- Build `generate_repo_map` and `semantic_search` tools.
- Implement token-aware context window management.

**Phase 3: MCP & Extensibility**
- Add Model Context Protocol support to the Rust runner.
- Dynamically inject MCP tools into the agent's prompt.

**Phase 4: Streaming & UI**
- Build real-time CLI streaming.
- Implement explicit Human-in-the-Loop approval breaks.