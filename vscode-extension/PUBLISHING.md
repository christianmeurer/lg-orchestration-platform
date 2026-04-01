# Publishing the Lula VS Code Extension

## Prerequisites

1. Create a Personal Access Token (PAT) at https://dev.azure.com
   - Organization: your Azure DevOps org linked to the VS Code Marketplace publisher
   - Scope: Marketplace > Manage
   
2. Add the PAT as a GitHub repository secret:
   - Go to Settings > Secrets > Actions
   - Create `VSCE_PAT` with the token value

3. For Open VSX (optional):
   - Create a token at https://open-vsx.org/user-settings/tokens
   - Add as `OVSX_PAT` repository secret

## Publishing

Publishing is automated via GitHub Actions on tag push:

```bash
git tag vscode-v0.2.0
git push origin vscode-v0.2.0
```

This triggers `.github/workflows/vscode-publish.yml` which:
1. Builds the extension with esbuild
2. Packages as VSIX
3. Publishes to VS Code Marketplace (if VSCE_PAT is set)
4. Publishes to Open VSX (if OVSX_PAT is set)

## Manual Publishing

```bash
cd vscode-extension
npm run compile
npx vsce package
npx vsce publish -p $VSCE_PAT
```
