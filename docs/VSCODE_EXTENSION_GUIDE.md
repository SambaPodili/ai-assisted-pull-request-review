# GTO Pull Request Review — VS Code Extension

Configuration and usage reference for the GTO Pull Request Review Framework extension — run the multi-agent PR review without leaving the editor.

**Version:** 0.14.3 · **Publisher:** UOB · **Engine:** vscode ^1.85.0 · **Extension ID:** UOB.gto-pr-review

---

## Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Initial setup](#initial-setup)
5. [Settings reference](#settings-reference)
6. [Commands reference](#commands-reference)
7. [Running an analysis](#running-an-analysis)
8. [Reading the results](#reading-the-results)
9. [Suppressing, false positives & explain](#suppressing-false-positives--explain)
10. [Path-scoped config (.gto.yaml)](#path-scoped-config-gtoyaml)
11. [Git hook](#git-hook)
12. [PR actions](#pr-actions)
13. [Choosing a model](#choosing-a-model)
14. [Troubleshooting & limitations](#troubleshooting--limitations)
15. [Building from source](#building-from-source)

---

## Overview

The GTO extension brings the same multi-agent review the web app runs on a submitted pull request into the editor, scoped to whatever you're currently working on. It diffs your working tree (or a branch against a base), sends the diff to the GTO backend, and surfaces the findings as a results panel, inline diagnostics, CodeLens annotations, and — where a fix is mechanical — one-click Quick Fixes.

Three review depths (presets) trade speed for coverage:

| Preset | Agents | Use for |
|---|---|---|
| `fast` | 2 — code_analysis, security | The editor loop. Fastest, catches the sharpest issues. |
| `standard` | 6 — core review + risk gate | A heavier local check before pushing. |
| `thorough` | ~22 — full depth | Same coverage as a real PR submission. Slow for an editor loop — use sparingly. |

---

## Prerequisites

- VS Code `1.85.0` or later.
- A running GTO backend reachable from your machine (defaults to `http://localhost:8080`) — ask your admin for the shared team URL, or run one locally.
- An API key for that backend.
- A git repository open in the workspace — both analyze commands operate on the current repo's diff.

---

## Installation

The extension ships as a `.vsix` package rather than through the public Marketplace.

1. Get the latest `gto-pr-review-<version>.vsix` file (built from the repo — see [Building from source](#building-from-source) — or from wherever your team distributes releases).
2. In VS Code, open the **Extensions** panel (`Cmd/Ctrl+Shift+X`).
3. Click the `···` menu at the top of the panel → **Install from VSIX...**
4. Select the downloaded file. VS Code installs it immediately — no reload usually required.

> **Upgrading from publisher `gto`:** Builds from 0.14.3 onward publish as `UOB.gto-pr-review` (previously `gto.gto-pr-review`). VS Code treats this as a different extension, so uninstall the old one first. Your `gto.*` settings and stored secrets carry over — only the extension identity changed.

---

## Initial setup

1. Point the extension at your backend: **Settings → Extensions → GTO Pull Request Review Framework → Backend Url** (or set `gto.backendUrl` directly in `settings.json`). Defaults to `http://localhost:8080`.
2. Run **GTO: Set API Key** from the Command Palette (`Cmd/Ctrl+Shift+P`) and paste your key. It's stored in VS Code's secret storage (the OS keychain) — never written to `settings.json` and never synced.
3. Open a git repository, make (or stage) some changes, and run **GTO: Analyze Changes**.

That's the minimum to get a first result. Everything below is configuration you can layer on afterward.

---

## Settings reference

All settings live under the `gto.*` namespace in `settings.json`, or via **Settings → Extensions → GTO Pull Request Review Framework**.

| Setting | Default | Description |
|---|---|---|
| `gto.backendUrl` | `http://localhost:8080` | Base URL of the backend, no trailing slash. |
| `gto.agentPreset` | `fast` | Review depth for editor-triggered analyses — `fast` / `standard` / `thorough`. |
| `gto.autoAnalyzeOnSave` | `false` | Auto-runs Analyze Changes ~1.5s after a save (uncommitted diff, no priorities prompt). Off by default — it's a real LLM-backed call on every save. |
| `gto.excludePatterns` | IDE/build noise (see below) | `.gitignore`-style globs dropped from every analysis, layered on top of your real `.gitignore`. Covers common JS/TS, Java/Kotlin, Python, .NET, Go/PHP/Ruby and Swift build output out of the box. |
| `gto.modelPreset` | `""` | Admin-configured backend model to use — set via **GTO: Select Model**, not edited by hand. No credential of your own required. |
| `gto.modelProvider` | `""` | *Advanced.* Manual override for a personal backend — `anthropic` / `openai` / `azure_openai` / `ollama` / `custom`. Ignored whenever `modelPreset` is set. |
| `gto.modelName` | `""` | *Advanced.* Model name for `gto.modelProvider`, e.g. `claude-sonnet-4-6`, `gpt-4o`, `llama3.2`. |
| `gto.modelBaseUrl` | `""` | *Advanced.* Endpoint URL — required for `azure_openai`, `ollama`, `custom`. |
| `gto.modelApiVersion` | `""` | *Advanced.* API version string, used only for `azure_openai`. |
| `gto.gitProvider` | `github` | Provider that "Post to PR" / "Approve PR" target — `github` / `bitbucket` / `bitbucket_server`. Should match the backend's own `GIT_PROVIDER`. |
| `gto.gitHookMode` | `warn` | Pre-push hook behavior — `warn` (prints the gate result, never blocks) or `block` (refuses the push on `BLOCK`). |

---

## Commands reference

All commands are reachable from the Command Palette (`Cmd/Ctrl+Shift+P`), prefixed `GTO:`.

| Command | Description |
|---|---|
| `GTO: Analyze Changes` | Reviews your working tree against `HEAD` (staged + unstaged + untracked). Also available from the status bar button. |
| `GTO: Analyze Branch...` | Quick-pick a base branch, then reviews everything your branch adds since it diverged (a three-dot diff — what a real PR would contain). |
| `GTO: Set API Key` | Stores your backend API key in secret storage. |
| `GTO: Set Model API Key` | Stores a credential for an advanced `gto.modelProvider` override, in secret storage. |
| `GTO: Select Model` | Picks an admin-configured model preset from the backend. |
| `GTO: Set Personal Git Provider Token` | Your own GitHub/Bitbucket token, used only so "Approve PR" shows as you. |
| `GTO: Install Git Hook` | Adds a local `pre-push` hook running a Fast-preset check. |
| `GTO: Uninstall Git Hook` | Removes it (only if GTO installed it). |
| `GTO: Show Last Result` | Reopens the most recent report from this session. |
| `GTO: Show Issues on This Line` | Quick-picks between findings when more than one lands on the same line. |

With more than one git repo open in the workspace, the analyze commands ask which one to use — skipped automatically when there's only one.

---

## Running an analysis

1. Run **GTO: Analyze Changes** (or **Analyze Branch...**).
2. Quick-pick the agent depth — defaults to your configured `gto.agentPreset`; press Enter to keep it.
3. Optionally type an analysis priority, e.g. *"focus on security in the payment module"*. Leave blank and press Enter to skip. The input is validated in real time against the same guardrails as the web app — this is UX-only, the backend re-validates authoritatively and this text can never change the deterministic gate decision.
4. The diff is submitted to the backend. Results open in a panel beside your editor.

> **Caching:** If nothing has changed since your last run — identical diff, depth, priorities and `.gto.yaml` — re-running shows the cached result instantly instead of re-hitting the backend.

---

## Reading the results

A run reports one gate outcome plus a ranked list of findings, surfaced in several places at once:

| Gate | Meaning |
|---|---|
| **PASS** | No blocking issues found. |
| **HOLD** | Review recommended before merging. |
| **BLOCK** | Blocking issues found. |

**Results panel** — Gate decision, risk score, ranked findings, and a "Files changed" list. Click a finding or file to jump to it. A matching suggested fix shows inline as an expandable diff with **Apply fix**. On the **Thorough** preset, unit-test coverage gaps get real, language-aware test skeletons with **Copy code** / **Create test file…**, and complex changes get a "Sequence diagrams" section (raw Mermaid source, copy-able — paste into a Mermaid live editor to view). **Copy as Markdown** copies the whole report.

**Code fixes** — Findings matched by one of 7 deterministic patterns (hardcoded secrets, weak hashes, etc.) get a high-confidence **Apply fix**. The remediation agent (Thorough preset) also proposes real before/after patches for other issues, labeled **"AI-suggested — review before applying"** since it isn't guaranteed correct the way a regex match is.

**Inline diagnostics & CodeLens** — Critical/high findings show as editor errors, medium as warnings, low as informational hints — also listed in the native Problems panel (`Cmd/Ctrl+Shift+M`). An inline "⚠ N GTO issue(s)" CodeLens sits above each flagged line; click to view (or quick-pick among several on the same line).

**Quick Fix (💡)** — On lines with a deterministic fix, the lightbulb offers "GTO fix: ...". Only appears when **Thorough** ran, since that's the only preset that includes the remediation agent. If the file changed since analysis ran, the fix is silently withheld rather than risk editing the wrong line.

**Status bar** — Shows the gate result (✓ / ⚠ / ⛔) after a run, colored for HOLD/BLOCK, in place of the idle "Analyze" label. Click it to re-analyze.

---

## Suppressing, false positives & explain

**🚫 Suppress a finding** — Stops it reappearing on future runs of the same code. Prompts for an optional reason, saved to `.gto-ignore.json` at the repo root — git-trackable, same idea as `.eslintignore`. Matching is by exact file + line, so if the code moves, the suppression won't silently follow it — it resurfaces instead, the safer failure mode. Manage existing suppressions from the "Suppressed findings" section at the bottom of the panel.

**🚩 Mark a false positive** — Feeds the same reviewer feedback loop the web app's Insights tab tracks. After enough false-positive verdicts pile up for the same repo/agent/category, GTO auto-suppresses that pattern going forward — always with a visible note in the report.

**❓ Explain** — Asks GTO why a finding was flagged and what to check before dismissing it, via the same guardrailed Q&A engine used for PR chat replies. The question sent is a fixed constant — there's no free-text input, so it can't be used to argue the model into changing a severity or the gate decision.

**New badge** — Issues not seen in your previous run on the same branch are badged **New** — tracked locally per (repo, branch); nothing is written to git for it.

---

## Path-scoped config (.gto.yaml)

Add a `.gto.yaml` at your repo root to define per-path rules — read automatically, no setting required:

```yaml
version: 1
paths:
  - match: "payments/**"
    user_instructions: "Treat any missing input validation as high severity"
  - match: "scripts/**"
    skip: true              # no agent runs on hunks matching this path
  - match: "**/*.generated.*"
    agents: []               # same as skip: true
```

`match` is a glob (a pattern with no `/` matches at any depth). `skip` / `agents: []` skip every agent on matching hunks; a non-empty `agents: [...]` is an allow-list. `user_instructions` adds path-scoped steering text, screened by the same guardrail as the priorities prompt.

> **Scope:** A rule only ever *narrows* agent selection, never widens it — it can fully skip an agent only when every hunk that agent would see is excluded. The backend applies the same file/schema for webhook-triggered and raw-API submissions.

---

## Git hook

Run **GTO: Install Git Hook** to add a local `pre-push` hook running a Fast-preset (`code_analysis` + `security`) check on whatever you're about to push. By default (`gto.gitHookMode: "warn"`) it only prints the gate result — it never blocks. Set the setting to `"block"` and reinstall to refuse a push outright on a `BLOCK` gate.

Written to `.git/hooks/pre-push` — local and untracked, per-clone. Your API key and backend URL are baked into the generated script, which is why. **GTO: Uninstall Git Hook** removes it only if GTO installed it — a hand-written hook is left alone.

> **Re-install after changes:** Re-run **GTO: Install Git Hook** after changing `gto.gitHookMode` or rotating your API key, so the hook picks up the change. It also doesn't handle a git worktree or submodule, where `.git` is a file rather than a directory.

---

## PR actions

Three buttons above the findings act on the whole report:

- **Apply all** — batch-applies every high-confidence deterministic fix in one click (shown only when at least one exists).
- **📮 Post to PR** — posts findings as grouped per-file comments plus a summary on a real PR (prompts for the PR number). Uses the backend's shared bot credential — comments appear as "GTO Bot", no setup needed.
- **✅ Approve PR** — reviewer sign-off; **never merges**, and never changes GTO's own gate decision. Requires your own personal token (**GTO: Set Personal Git Provider Token**) first, so the approval shows as you.

When available, the panel also surfaces past analyses similar to the current one (file-overlap + summary-keyword similarity) with their gate outcomes — the same data the web app's Results view shows.

---

## Choosing a model

**Shared team backend** — Run **GTO: Select Model**. It fetches the admin-configured presets from the backend (e.g. "Llama", "Qwen") and lets you pick one — no API key or URL ever needs to come from you. Picking "Default" clears the selection and reverts to the backend's own model.

**Personal backend** — Use the advanced `gto.modelProvider` / `modelName` / `modelBaseUrl` / `modelApiVersion` settings, plus **GTO: Set Model API Key** to store the credential in secret storage. These are ignored whenever `gto.modelPreset` is set.

---

## Troubleshooting & limitations

- **No result / connection errors** — check `gto.backendUrl` is correct and the backend is reachable from your machine, and that **GTO: Set API Key** has been run.
- **"❓ Explain" fails with a generic error** — the backend predates the `/report/{id}/explain-finding` endpoint; ask your admin to update it.
- **Quick Fix lightbulb missing** — only appears on the **Thorough** preset, since Fast/Standard don't run the remediation agent.
- **A fix silently didn't apply** — the target file changed since the analysis ran, so the fix was withheld rather than risk editing the wrong line. Re-run the analysis.
- **Pre-push hook not installing** — not supported in a git worktree or submodule (where `.git` is a file, not a directory).
- **Not yet built** — the priorities prompt is a single-line input box; the web app's multi-line textarea for longer guidance isn't in the extension yet.

---

## Building from source

From the `vscode-extension/` directory:

```bash
npm install
npm run compile      # one-off build -> dist/extension.js
npm run watch        # rebuild on save
```

Press **F5** in VS Code (with this folder open) to launch an Extension Development Host with the extension loaded — uses the included `.vscode/launch.json` and `.vscode/tasks.json`.

To produce an installable package without a debug session:

```bash
npm run vsix
```

Packages to `gto-pr-review-<version>.vsix` (version from `package.json`), so successive builds don't overwrite each other. Install via **Extensions panel → ··· → Install from VSIX...** — a `.vsix` is a frozen snapshot and doesn't hot-reload, so rebuild and reinstall after any source change.

---

*Source: `vscode-extension/` in the repository (`code-impact-analysis-review`). Reflects extension version 0.14.3.*
