# Lula VS Code Extension

Submit coding tasks to the [Lula](https://lula.eiv.eng.br) autonomous coding agent directly from VS Code.

## Features

- **Run Task** (`Ctrl+Shift+P` -> `Lula: Run Task`) -- Submit a natural-language coding task
- **Show Recent Runs** -- Open the Lula web console to view recent runs
- **Open Console** -- Open the Lula web console in the browser
- **Configure** -- Set your server URL and API token

## Setup

1. Install the extension
2. Run `Lula: Configure` to set your API token
3. Run `Lula: Run Task` to submit a task

## Configuration

| Setting | Default | Description |
|---|---|---|
| `lula.serverUrl` | `https://lula.eiv.eng.br` | Lula server URL |

The API token is stored securely in VS Code SecretStorage (not in plaintext settings).

## Sidebar

The extension adds a **Lula** sidebar with an **Orchestrator Runs** tree view that lists recent runs from the server. Click a run to open its detail panel.

## Commands

| Command | Identifier | Purpose |
|---|---|---|
| Run Task | `lula.runTask` | Submit a coding task to the Lula agent |
| Show Recent Runs | `lula.showRuns` | Open the Lula web console |
| Open Web Console | `lula.openConsole` | Open the Lula web console in the browser |
| Configure | `lula.configure` | Set API token and server URL |
| Refresh Runs | `orchestrator.refreshRuns` | Refresh the sidebar run list |
| Open Run Panel | `orchestrator.openRun` | Open a run detail panel |
| New Run | `orchestrator.newRun` | Start a new run (delegates to Run Task) |

## Local Development

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

## Packaging

```bash
npm install
npm run package
```
