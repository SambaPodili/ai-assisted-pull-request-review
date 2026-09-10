# GTO Pull Request Review — IntelliJ Plugin

Configuration and usage reference for the GTO Pull Request Review Framework
IntelliJ plugin — run the multi-agent PR review without leaving the IDE.
IntelliJ counterpart to the GTO VS Code extension: same backend, same
review agents, same results rendering.

**Version:** 0.2.1 · **Vendor:** UOB · **Platform:** IntelliJ IDEA (Community or Ultimate) 2022.3+ · **Plugin ID:** com.uob.gto.pr-review

---

## Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Initial setup](#initial-setup)
5. [Settings reference](#settings-reference)
6. [Actions reference](#actions-reference)
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

The GTO plugin brings the same multi-agent review the web app runs on a
submitted pull request into the IDE, scoped to whatever you're currently
working on. It diffs your working tree (or a branch against a base) via the
`git` CLI, sends the diff to the GTO backend, and surfaces the findings in
a dedicated tool window, as inline diagnostics, as an always-visible Code
Vision annotation, and — where a fix is mechanical — one-click Quick Fixes.

Three review depths (presets) trade speed for coverage:

| Preset | Agents | Use for |
|---|---|---|
| `fast` | 2 — code_analysis, security | The editor loop. Fastest, catches the sharpest issues. |
| `standard` | 6 — core review + risk gate | A heavier local check before pushing. |
| `thorough` | ~22 — full depth | Same coverage as a real PR submission. Slow for an editor loop — use sparingly. |

---

## Prerequisites

- IntelliJ IDEA (Community or Ultimate) `2022.3` or later, with JCEF enabled — bundled by default in all modern JetBrains IDE distributions, so this is normally a non-issue.
- A running GTO backend reachable from your machine (defaults to `http://localhost:8080`) — ask your admin for the shared team URL, or run one locally. **This is the same backend the VS Code extension and web app use** — nothing IntelliJ-specific to stand up.
- An API key for that backend.
- A git repository open as the project — both analyze actions operate on the current repo's diff via the `git` CLI directly (no VCS plugin dependency).

---

## Installation

The plugin ships as a ZIP built from source rather than through the JetBrains Marketplace.

1. Get the latest `gto-pr-review-intellij-<version>.zip` (built from the repo — see [Building from source](#building-from-source) — or from wherever your team distributes releases; ask for it the same way you'd ask for the VS Code `.vsix`).
2. In IntelliJ IDEA: **Settings → Plugins → ⚙️ (gear icon, top of the list) → Install Plugin from Disk...**
3. Select the ZIP. Restart the IDE when prompted.

> **Updates are manual for now.** There's no auto-update channel yet — a new version means re-sharing the ZIP and repeating the install steps. Uninstall the old version first if you hit a conflict (**Settings → Plugins → GTO Pull Request Review Framework → Uninstall**).

---

## Initial setup

1. Open **Settings → Tools → GTO Review**.
2. Under **Backend**, set your **Backend URL** (defaults to `http://localhost:8080`) and paste your **API key**. The key is stored in the OS keychain (via IntelliJ's PasswordSafe) — never written to the plain settings file, never synced.
3. Click **OK** to save.
4. Open a git repository, make (or stage) some changes, and run **GTO: Analyze Changes** (Tools menu, or `Ctrl+Alt+Shift+G`).

That's the minimum to get a first result. Everything below is configuration you can layer on afterward.

---

## Settings reference

All settings live on one page: **Settings → Tools → GTO Review**.

| Field (group) | Default | Description |
|---|---|---|
| Backend URL *(Backend)* | `http://localhost:8080` | Base URL of the backend, no trailing slash. |
| API key *(Backend)* | — | Stored via PasswordSafe. Leave blank when re-opening Settings to keep the current key — it's never shown back to you. |
| Provider *(Git provider)* | `github` | Which provider "Post to PR"/"Approve PR" target — `github` / `bitbucket` / `bitbucket_server`. Should match the backend's own configured provider. |
| Personal token *(Git provider)* | — | Your own GitHub/Bitbucket token, used only so **Approve PR** shows as you, not the shared bot. Stored via PasswordSafe. |
| Pre-push hook *(Git provider)* | `warn` | `warn` (prints the gate result, never blocks) or `block` (refuses the push on a `BLOCK` gate). |
| Agent preset *(Review)* | `fast` | Review depth for editor-triggered analyses. |
| Auto-analyze on save *(Review)* | off | Auto-runs Analyze Changes ~1.5s after a save (uncommitted diff, no priorities prompt). Off by default — it's a real LLM-backed call on every save. |
| Server model preset *(Model)* | *(blank)* | Admin-configured backend model to use — set via **GTO: Select Model**, not typed by hand. No credential of your own required. Takes priority over the manual fields below when non-blank. |
| Manual provider / model name / base URL *(Model)* | *(blank)* | *Advanced.* Manual override for a personal backend — only consulted when Server model preset is blank. |
| Model API key *(Model)* | — | *Advanced.* Only consulted when Server model preset is blank. Stored via PasswordSafe. |
| Connected repos *(Review context)* | *(empty)* | One dependent repo name/slug per line — the dependency agent's blast-radius baseline. |
| Connected repo local paths *(Review context)* | *(empty)* | One local filesystem path per line, to dependent repos already checked out on this machine. Grepped for real call-sites of this diff's changed symbols and sent as cross-repo references — this is genuinely *cheaper* to do from the IDE than from the web app, since a local checkout has filesystem access a browser doesn't. |
| Functional spec paths *(Review context)* | *(empty)* | One path per line to `.md`/`.txt`/`.docx`/`.pdf` requirement documents to trace the change against — enables the Functional Validation agent. |

> **Not yet exposed here:** the exclude-patterns list (which files are dropped from every diff before analysis — IDE noise, build output, lockfiles, etc.) uses a built-in default list and isn't editable from the Settings page yet, unlike the VS Code extension's `gto.excludePatterns`.

---

## Actions reference

All actions are reachable from the **Tools → GTO** menu, or via **Find Action** (`Cmd/Ctrl+Shift+A` — type "GTO" to filter).

| Action | Description |
|---|---|
| `GTO: Analyze Changes` | Reviews your working tree against `HEAD` (staged + unstaged + untracked). `Ctrl+Alt+Shift+G` by default. |
| `GTO: Analyze Branch...` | Pick a base branch, then reviews everything your branch adds since it diverged (a three-dot diff — what a real PR would contain). |
| `GTO: Show Last Result` | Reopens the most recent report from this session. |
| `GTO: Set API Key` | Stores your backend API key via PasswordSafe — a quick-access alternative to the Settings page. |
| `GTO: Set Model API Key` | Stores a credential for an advanced manual model override, via PasswordSafe. |
| `GTO: Select Model` | Fetches admin-configured model presets from the backend and lets you pick one. |
| `GTO: Set Personal Git Provider Token` | Your own GitHub/Bitbucket token, used only so "Approve PR" shows as you. |
| `GTO: Install Git Hook` | Adds a local `pre-push` hook running a Fast-preset check. |
| `GTO: Uninstall Git Hook` | Removes it (only if GTO installed it). |

---

## Running an analysis

1. Run **GTO: Analyze Changes** (or **Analyze Branch...**).
2. Pick the agent depth from the popup — defaults to your configured preset; the current default is listed first.
3. Optionally type an analysis priority, e.g. *"focus on security in the payment module"*. Leave blank to skip. The input is validated as you type against the same guardrails as the web app — this is UX-only, the backend re-validates authoritatively and this text can never change the deterministic gate decision.
4. The diff is submitted to the backend. The **GTO Review** tool window (right-hand sidebar by default) opens with the result.

> **Caching:** If nothing has changed since your last run — identical diff, depth, priorities, `.gto.yaml`, and review-context settings — re-running shows the cached result instantly instead of re-hitting the backend.

---

## Reading the results

A run reports one gate outcome plus a ranked list of findings, surfaced in several places at once:

| Gate | Meaning |
|---|---|
| **APPROVE** | No blocking issues found. |
| **HOLD** | Review recommended before merging. |
| **BLOCK** | Blocking issues found. |

**GTO Review tool window** — Gate decision, risk score, ranked findings, and a "Files changed" list. Click a finding or file to jump to it. A matching suggested fix shows inline as an expandable diff with **Apply fix**. On the **Thorough** preset, unit-test coverage gaps get real, language-aware test skeletons with **Copy code** / **Create test file…**, and complex changes get a "Sequence diagrams" section (raw Mermaid source, copy-able — paste into a Mermaid live editor to view). **Copy as Markdown** copies the whole report. When available, the panel also lists past analyses similar to the current one (file-overlap + summary-keyword similarity) with their gate outcomes.

**Code fixes** — Findings matched by one of several deterministic patterns (hardcoded secrets, weak hashes, etc.) get a high-confidence **Apply fix**. The remediation agent (Thorough preset) also proposes real before/after patches for other issues, labeled **"AI-suggested — review before applying"** since it isn't guaranteed correct the way a regex match is.

**Inline diagnostics** — Findings show as editor squiggles, severity-graded, and are also listed in the native **Problems** tool window.

**Code Vision ("⚠ N GTO issue(s)")** — An always-visible inline annotation above each flagged line — IntelliJ's own equivalent of a CodeLens. Click it to see the finding directly (or choose among several, if more than one lands on the same line).

**Quick Fix (💡)** — On lines with a deterministic fix, the lightbulb (`Alt+Enter`) offers "GTO fix: ...". Only appears when **Thorough** ran, since that's the only preset that includes the remediation agent. If the file changed since analysis ran, the fix is silently withheld rather than risk editing the wrong line.

**Status bar** — Shows the gate result (✓ / ⚠ / ⛔) after a run, in place of the idle "▶ GTO Review" label. Click it to re-analyze.

---

## Suppressing, false positives & explain

**🚫 Ignore a finding** — Stops it reappearing on future runs of the same code. Prompts for an optional reason, saved to `.gto-ignore.json` at the repo root — git-trackable, same idea as `.eslintignore`, and **shared with the VS Code extension** (same file, same fingerprint format). Matching is by exact file + line, so if the code moves, the suppression won't silently follow it — it resurfaces instead, the safer failure mode. Manage existing suppressions from the "Suppressed findings" section at the bottom of the panel.

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

> **Scope:** A rule only ever *narrows* agent selection, never widens it. Same file, same schema as the VS Code extension and the backend's own webhook-triggered path.

---

## Git hook

Run **GTO: Install Git Hook** to add a local `pre-push` hook running a Fast-preset (`code_analysis` + `security`) check on whatever you're about to push. By default (**Pre-push hook: warn**) it only prints the gate result — it never blocks. Set the Settings field to **block** and reinstall to refuse a push outright on a `BLOCK` gate.

Written to `.git/hooks/pre-push` — local and untracked, per-clone — and is the **exact same generated bash script** the VS Code extension installs, byte for byte (it's plain shell, IDE-agnostic). Your API key and backend URL are baked into the generated script, which is why. **GTO: Uninstall Git Hook** removes it only if GTO installed it — a hand-written hook is left alone.

> **Re-install after changes:** Re-run **GTO: Install Git Hook** after changing the hook mode or rotating your API key, so the hook picks up the change. It also doesn't handle a git worktree or submodule, where `.git` is a file rather than a directory.

---

## PR actions

Three buttons above the findings act on the whole report:

- **Apply all** — batch-applies every high-confidence deterministic fix in one click (shown only when at least one exists).
- **📮 Post to PR** — posts findings as grouped per-file comments plus a summary on a real PR (prompts for the PR number). Uses the backend's shared bot credential — comments appear as "GTO Bot", no setup needed.
- **✅ Approve PR** — reviewer sign-off; **never merges**, and never changes GTO's own gate decision. Requires your own personal token (**GTO: Set Personal Git Provider Token**, or the Settings page) first, so the approval shows as you.

---

## Choosing a model

**Shared team backend** — Run **GTO: Select Model**. It fetches the admin-configured presets from the backend (e.g. "Llama", "Qwen") and shows a popup to pick one — no API key or URL ever needs to come from you. Picking "Default" clears the selection and reverts to the backend's own model.

**Personal backend** — Use the advanced Manual provider / model name / base URL fields on the Settings page, plus **GTO: Set Model API Key** to store the credential via PasswordSafe. These are ignored whenever Server model preset is set.

---

## Troubleshooting & limitations

- **No result / connection errors** — check the Backend URL is correct and the backend is reachable from your machine, and that an API key is set in Settings.
- **"❓ Explain" fails with a generic error** — the backend predates the `/report/{id}/explain-finding` endpoint; ask your admin to update it.
- **Quick Fix lightbulb missing** — only appears on the **Thorough** preset, since Fast/Standard don't run the remediation agent.
- **A fix silently didn't apply** — the target file changed since the analysis ran, so the fix was withheld rather than risk editing the wrong line. Re-run the analysis.
- **Pre-push hook not installing** — not supported in a git worktree or submodule (where `.git` is a file, not a directory).
- **Exclude patterns aren't editable from Settings yet** — see the note in [Settings reference](#settings-reference).
- **No auto-update** — install is a manual ZIP; a new version means re-installing by hand (see [Installation](#installation)).
- **The priorities prompt is a single-line input**, same limitation the VS Code extension has — the web app's multi-line textarea for longer guidance isn't in either editor plugin yet.

---

## Building from source

From the `intellij-plugin/` directory:

```bash
gradle compileKotlin   # one-off compile check
gradle runIde          # launches a sandboxed IDE with the plugin loaded — nothing touches your real IDE config
```

No Gradle wrapper is committed yet — see `intellij-plugin/README.md` for why and the exact command to generate one. Requires a JDK 17 for the Gradle daemon itself (separate from IntelliJ Platform's own bundled runtime).

To produce an installable package:

```bash
gradle buildPlugin
```

Packages to `build/distributions/gto-pr-review-intellij-<version>.zip` (version from `gradle.properties`). Install via **Settings → Plugins → ⚙️ → Install Plugin from Disk...** — a ZIP is a frozen snapshot and doesn't hot-reload, so rebuild and reinstall after any source change.

The plugin depends on `shared/report-view` (the same report-rendering code the VS Code extension uses) — run `npm run build` there first if `gradle buildPlugin` reports that bundle as missing.

---

*Source: `intellij-plugin/` in the repository. Reflects plugin version 0.2.0.*
