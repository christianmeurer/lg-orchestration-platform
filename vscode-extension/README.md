# Lula VS Code Extension

This extension provides a small VS Code surface for running the Lula stack from inside VS Code, either locally or against a hosted remote API.

## Current capabilities

- Open a dedicated Lula webview panel.
- Start the local Rust runner from the workspace by invoking `cargo` in `rs/`.
- Run an orchestration request from the panel or command palette.
- Stop the local runner process.
- Show the current workspace root, runner status, request status, latest trace path, final output, and captured logs.

## Prerequisites

- `cargo` must be installed and available on `PATH`.
- `uv` must be installed and available on `PATH`.
- The VS Code workspace root must be the repository root so the extension can resolve `rs/`, `py/`, and `artifacts/runs/`.
- `npm` must be available locally to build the extension during development.

## Local development

1. From `vscode-extension/`, install dependencies:

   ```bash
   npm install
   ```

2. Compile the extension:

   ```bash
   npm run compile
   ```

3. Start the extension in an Extension Development Host:
   - Open `vscode-extension/` in VS Code.
   - Run the `Run Extension` debug target or press `F5`.

4. In the Extension Development Host window, open the repository root workspace before using the extension commands.

## Commands

The extension contributes four commands:

| Command | Identifier | Purpose |
| --- | --- | --- |
| Open Panel | `lgOrch.openPanel` | Opens the Lula webview panel. |
| Start Runner | `lgOrch.startRunner` | Starts the local Rust runner for the current workspace. |
| Run Request | `lgOrch.runRequest` | Prompts for, or accepts, a request and runs the orchestration flow against the local stack. |
| Stop Runner | `lgOrch.stopRunner` | Stops the local runner process started by the extension. |

## Notes

- `Start Runner` launches the runner with the development profile and the workspace root as the runner root directory.
- `Run Request` performs `uv sync` in `py/` before invoking the CLI run flow.
- The panel mirrors command execution and shows the latest trace discovered under `artifacts/runs/`.
- Set `lula.remoteApiBaseUrl` to a deployed remote API URL to route requests through the hosted stack instead of local `uv` and `cargo` execution.

## Packaging and publishing

From [`vscode-extension/`](vscode-extension/):

```bash
npm install
npm run package
```

Later Marketplace publishing uses:

```bash
npm run publish
```

Notes:
- The packaging and publishing scripts use the local `@vscode/vsce` dev dependency.
- Authenticate with your VS Marketplace publisher credentials before running publish.
