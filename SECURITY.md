# Security policy

## Reporting a vulnerability

Please report security problems **privately** through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue for vulnerabilities.

Include what you found, how to reproduce it, and the version (`beom-workbench --version` or the app's version label).
You will get a reply as soon as possible; fixes are released from `main`.

## Supported versions

Only the latest release on `main` receives security fixes.

## Scope

In scope: the toolkit (CLI, MCP server, desktop app) and its handling of agent permissions, approvals, the ledger and
files in the target folder. The coding agents themselves (Claude Code, Codex, Antigravity) are third-party software —
report their issues to their vendors.
