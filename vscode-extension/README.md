# GTO Pull Request Review Framework — VS Code Extension

Run the multi-agent PR review without leaving the editor.

## Commands

- **`GTO: Analyze Changes`** (Command Palette, or the status bar button) — diffs
  your working tree against `HEAD` (staged + unstaged combined, plus any
  untracked files that were never `git add`ed), quick-picks
  the agent depth (defaults to your configured preset — press Enter to keep
  it), prompts for optional analysis priorities, submits to the backend, and
  shows results in a panel beside your editor, as inline diagnostics, and
  (when the `remediation` agent ran) as Quick Fix actions.
- **`GTO: Analyze Branch...`** — quick-pick a base branch, then reviews
  everything your current branch adds since it diverged from that base (a
  three-dot diff, same semantics as what a PR against that base would contain).
- **`GTO: Set API Key`** — stores your API key in VS Code's secret storage (OS
  keychain). Never written to settings.json or synced.
- **`GTO: Show Last Result`** — reopens the most recent report from this session.
- **`GTO: Set Personal Git Provider Token`** — your own Bitbucket/GitHub token,
  used only by "✅ Approve PR" (see below) so an approval shows as you, not
  the shared bot.
- **`GTO: Install Git Hook`** / **`GTO: Uninstall Git Hook`** — installs a local
  `pre-push` hook that runs a Fast-preset check on what's about to be pushed.
  See [Git hook](#git-hook) below.

If you have more than one git repo open in the workspace, both analyze
commands ask which one to use (skipped entirely when there's only one — no
added friction for the common case).

If nothing changed since your last run (identical diff, depth, priorities, and
`.gto.yaml` content), re-running shows the cached result instantly instead of
re-hitting the backend.

## Path-scoped review config (`.gto.yaml`)

Add a `.gto.yaml` at your repo root to define per-path rules — read
automatically, no setting needed:

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

`match` is a glob (a pattern with no `/` matches at any depth). `skip`/
`agents: []` skip every agent for matching hunks; a non-empty `agents: [...]`
is an allow-list (only those agents run there). `user_instructions` adds
path-scoped steering text — scanned by the same guardrail as the priorities
prompt before it reaches an LLM, and it's request-wide once triggered (v1
doesn't hide specific files from an agent that also has in-scope work
elsewhere in the same diff — a rule only ever narrows the agent selection,
never widens it, and can fully skip an agent only when *every* hunk it would
see is excluded). The backend applies the same file/schema for
webhook-triggered and raw-API submissions.

## Analysis priorities

Both analyze commands prompt for optional free-text guidance — e.g. *"focus on
security in the payment module"*. Leave it blank and press Enter to skip. The
input box validates in real time and blocks phrasing that looks like it's
trying to override review rules rather than prioritize (mirrors the same
guardrails as the main web app — see `governance/prompt_guard.py`). This is
UX-only: the backend re-validates authoritatively regardless of what the
extension catches, and this text can never influence the deterministic gate
decision (see `core/models.py`'s `AnalysisRequest.user_instructions` for why).

## Where results show up

- **A webview panel** beside your editor — gate decision, risk score, ranked
  findings, and a "Files changed" list. Click a finding (or a file) to jump to
  it. When a suggested fix matches a finding, it shows inline as an
  expandable diff with an "Apply fix" button. Findings without a mechanical
  fix still get the remediation agent's text-level "Suggested fixes" list.
  On the **Thorough** preset, "Unit test coverage gaps" shows each missing
  scenario with a real, language-aware test skeleton, a "Copy code" button,
  and a "Create test file…" button (opens a save dialog pre-filled with a
  sensible location — co-located with the affected source file, or mirrored
  into `src/test/...` for a Maven/Gradle-style `src/main/...` layout). Also on
  **Thorough**, when the change is complex enough (real reference-impact data
  at medium+ risk), a "Sequence diagrams" section shows the raw Mermaid
  source as a labeled, copy-able block — "AI-generated — not verified
  against the real call graph." Paste it into a Mermaid live editor to view
  it; the panel doesn't render it inline. A "Copy as Markdown" button copies
  the whole report for pasting into a PR description or chat.
- **Code fixes**: findings matched by the 7 deterministic patterns
  (`agents/fix_generator.py` — hardcoded secrets, weak hashes, etc.) get a
  high-confidence Apply-fix. Beyond that, the `remediation` agent also
  proposes real before/after patches for other issues, verified against the
  actual diff before being kept — these show the same way but labeled
  **"AI-suggested — review before applying"**, since an LLM-written patch
  isn't guaranteed correct the way a regex match is.
- **Inline diagnostics** — critical/high findings show as errors, medium as
  warnings, low as informational hints, both as squiggly underlines in the
  editor and as entries in the native Problems panel (`Cmd+Shift+M` /
  `Ctrl+Shift+M`).
- **CodeLens** — an inline "⚠ N GTO issue(s)" annotation right above each
  flagged line. Click to view the finding (or quick-pick between several, if
  more than one issue lands on the same line). Skipped for findings with no
  resolvable line — a CodeLens pinned to line 1 of an unrelated file would be
  more misleading than useful.
- **Quick Fix (💡)** — on lines with a deterministic suggested fix (hardcoded
  secrets, weak hashes, etc. — see `agents/fix_generator.py`), the lightbulb
  offers "GTO fix: ...". Only appears when the **Thorough** preset ran, since
  that's the only one that includes the `remediation` agent; Fast/Standard
  intentionally match the web app's preset definitions rather than diverging
  just to unlock this in the extension. If the file changed since the
  analysis ran, the fix is silently withheld rather than editing the wrong
  line — it only applies when the target line still matches exactly.
- **Status bar** — after a run, shows the gate result (✓/⚠/⛔) instead of the
  idle "Analyze" label, colored for HOLD/BLOCK. Click it to re-analyze.

## Suppressing a finding

Click **🚫 Ignore** on any issue card to stop it reappearing on future runs
of the same code. You'll be prompted for an optional reason, then it's saved
to **`.gto-ignore.json`** at the repo root — git-trackable, so the team can
see why something was suppressed (same idea as a `.eslintignore` or `# noqa`
comment). Matching is by exact file + line, so if the code moves, the
suppression won't silently apply to the wrong line — it'll just show up
again, which is the safer failure mode. Manage existing suppressions from
the "Suppressed findings" section at the bottom of the panel.

## Marking a false positive

Click **🚩 False positive** on any issue card to record your verdict —
the same reviewer feedback loop the web app's Insights tab already tracks
(`POST /report/{id}/feedback`). After enough false-positive verdicts pile up
for the same repo/agent/category, GTO auto-suppresses that pattern on future
runs — always with a visible note in the report, never silently.

## New-since-last-run

Issues not seen in your previous run on the same branch are badged **New**.
This is tracked locally per (repo, branch) — nothing is written to git for
it, unlike suppressions.

## Explaining a finding

Click **❓ Explain** on any issue card to ask GTO why it was flagged and what
to check before dismissing it — answered by the same guardrailed Q&A engine
already used for PR chat replies. The question sent is always the same fixed
text; there's no free-text input here, so this can't be used to argue the
model into changing a severity or the gate decision (it has no authority to
do either).

## Report actions

Three buttons above the findings act on the whole report, not one issue:

- **Apply all** — batch-applies every high-confidence deterministic fix in
  one click (only shown when at least one exists).
- **📮 Post to PR** — posts the findings as grouped per-file comments plus an
  overall summary on a real PR (prompts for the PR number). Uses the
  backend's shared bot credential — no setup needed on your end, comments
  appear as "GTO Bot", same identity webhook-triggered comments already use.
- **✅ Approve PR** — reviewer sign-off on the PR, **never merges**. Requires
  your own personal token (`GTO: Set Personal Git Provider Token`) first —
  the approval must show as *you* on the PR, not the shared bot, so there's
  no fallback here. Distinct from a gate override: this never changes GTO's
  own gate decision, it's purely a side-channel "I reviewed this" action.

## Similar past PRs

When available, the panel shows past analyses similar to the current one
(file-overlap + summary-keyword similarity) with their gate outcomes — useful
context before you dig through history yourself. Same underlying data the
web app's Results view already surfaces.

## Git hook

Run **`GTO: Install Git Hook`** to add a local `pre-push` hook that runs a
Fast-preset (`code_analysis` + `security`) check on whatever you're about to
push, before it ever reaches a PR. By default (`gto.gitHookMode: "warn"`) it
only prints the gate result — it never blocks a push. Set it to `"block"`
and reinstall to refuse a push outright when the gate comes back `BLOCK`.

The hook is written to `.git/hooks/pre-push` — local and untracked by git,
same as any other git hook, so it's per-clone, not something you commit.
Your API key and backend URL are baked into the generated script for that
reason. `GTO: Uninstall Git Hook` removes it (only if GTO installed it — a
hand-written hook is left untouched). Re-run `GTO: Install Git Hook` after
changing `gto.gitHookMode` or rotating your API key to pick up the change.

Known limitation: resolves hooks under the repo's own `.git/hooks/` directly
— doesn't handle a git worktree or submodule, where `.git` is a file
pointing elsewhere.

## Setup

1. Set the backend URL (defaults to `http://localhost:8080`):
   `Settings → Extensions → GTO Pull Request Review Framework → Backend Url`
   (or edit `gto.backendUrl` in `settings.json`).
2. Run **`GTO: Set API Key`** once and paste your key.
3. Open a git repository, make some changes, run **`GTO: Analyze Changes`**.

## Settings

| Setting | Default | Description |
|---|---|---|
| `gto.backendUrl` | `http://localhost:8080` | Base URL of the backend, no trailing slash. |
| `gto.agentPreset` | `fast` | `fast` (2 agents) / `standard` (6 agents) / `thorough` (~22 agents, full-depth — slow for an editor loop, matches a full PR submission). |
| `gto.autoAnalyzeOnSave` | `false` | Automatically re-run "Analyze Changes" ~1.5s after you save a file — uncommitted diff only, no priorities prompt. **Off by default**: this makes a real LLM-backed backend call on every save, which costs time and tokens. Turn on only if that tradeoff is worth it for your workflow. |
| `gto.excludePatterns` | IDE/tooling noise + build output for JS/TS, Java/Kotlin, Python, .NET, Go/PHP/Ruby, Swift (full list in `package.json`) | `.gitignore`-flavored glob patterns dropped from every analysis, on top of what `.gitignore` already hides — a pattern with no `/` matches at any depth (e.g. `*.lock` catches nested lockfiles too). Applies to both `Analyze Changes` and `Analyze Branch...`. Note: `.NET`'s `obj/` is excluded by default but `bin/` deliberately isn't, since some projects ship real code there (npm bin scripts, CLI wrappers) — add it yourself if your repo's `bin/` is pure build output. |
| `gto.modelPreset` | `""` (backend default) | Which admin-configured model to use — set via **`GTO: Select Model`**, not edited directly. The recommended way to pick a model on a shared team backend: no credential from you at all. |
| `gto.modelProvider` | `""` | **Advanced.** Manual override for a personal backend with your own separate provider — `anthropic` / `openai` / `azure_openai` / `ollama` / `custom`. Ignored whenever `gto.modelPreset` is set. |
| `gto.modelName` | `""` | **Advanced.** Model name for `gto.modelProvider`, e.g. `claude-sonnet-4-6`, `gpt-4o`, `llama3.2`, or your own deployment name. |
| `gto.modelBaseUrl` | `""` | **Advanced.** Endpoint URL — required for `azure_openai`, `ollama`, and `custom`. |
| `gto.modelApiVersion` | `""` | **Advanced.** API version string — only used for `azure_openai`. |
| `gto.gitProvider` | `github` | Which provider "Post to PR" and "Approve PR" target — `github` / `bitbucket` / `bitbucket_server`. Should match the backend's own `GIT_PROVIDER`. |
| `gto.gitHookMode` | `warn` | Behavior of the pre-push hook installed via `GTO: Install Git Hook` — `warn` (never blocks) or `block` (refuses the push on a `BLOCK` gate). |

## Choosing a model

**On a shared team backend** (multiple people pointing the extension at the
same server): run **`GTO: Select Model`**. It fetches the admin-configured
presets from the backend (e.g. "Llama", "Qwen") and lets you pick one — no
API key or URL ever needs to come from you; the backend already has that
configured server-side (`MODEL_PRESETS` in its `.env`). Picking "Default"
clears the selection and goes back to the backend's own model.

**On a personal backend** where you want to bring your own separate
provider/endpoint, use the advanced `gto.modelProvider`/`modelName`/
`modelBaseUrl`/`modelApiVersion` settings instead, plus **`GTO: Set Model API
Key`** to store the credential in SecretStorage (never a plain setting).
These are ignored whenever `gto.modelPreset` is set.

## Not yet built

- A settings UI for the priorities text (currently a single-line input box;
  the web app's textarea supports longer, multi-line guidance).

## Development

```bash
npm install
npm run compile      # one-off build -> dist/extension.js
npm run watch        # rebuild on save
```

Press **F5** in VS Code (with this folder open) to launch an Extension
Development Host with the extension loaded — needs `.vscode/launch.json`
and `.vscode/tasks.json` (included) to work out of the box.

To produce an installable `.vsix` (the simpler way to try it — no debug
session needed):

```bash
npm run vsix
```

which packages to `gto-pr-review-<version>.vsix` (version from `package.json`), so successive builds don't overwrite each other and it's obvious which one you're installing.

Then in VS Code: **Extensions panel → `...` → Install from VSIX...** and
pick the file. Re-run this command and reinstall after any source change —
an installed `.vsix` is a frozen snapshot, it doesn't hot-reload.

(`--no-dependencies` is used because this extension has no runtime
`dependencies` — everything is bundled by esbuild into `dist/extension.js`.)
