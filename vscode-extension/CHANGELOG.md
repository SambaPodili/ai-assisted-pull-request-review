# Changelog

## 0.10.1

- **Backend**: `path_review_summary` on the report — a safe, display-only
  record of what `.gto.yaml` actually did for a run (agents skipped,
  whether steering text was applied). Previously this only ever showed up
  as a server log line; now it's visible.
- Results panel shows a small `.gto.yaml` banner (e.g. "skipped 2 agent(s)
  (security, code_analysis)") whenever path-scoped rules affected the run —
  previously invisible locally, the only symptom was fewer findings than
  expected with no explanation. Included in Copy as Markdown too.

## 0.10.0

- **Path-scoped review config** — a `.gto.yaml` at the repo root lets a repo
  define per-path rules: skip specific agents (or all of them) for matching
  paths (`skip: true`, `agents: []`, or an allow-list `agents: [...]`), and
  add path-scoped steering text (`user_instructions`) that only applies when
  the diff touches that path. Read automatically from the workspace root —
  same file/schema the backend now understands for webhook-triggered and
  raw-API submissions too. Free text is scanned by the same prompt-injection
  guard as the priorities prompt before it ever reaches an LLM. A rule only
  ever narrows the configured agent selection, never widens it.

## 0.9.0

- **Suppress/ignore a finding** — a "🚫 Ignore" action on each issue card,
  persisted to a git-trackable `.gto-ignore.json` at the repo root (with an
  optional reason) so it stops reappearing in the panel, Problems panel, and
  diagnostics on future runs. Matched by exact file+line, so a moved finding
  reappears rather than silently suppressing the wrong line. Manageable from
  a "Suppressed findings" section in the panel.
- **Delta view** — issues not seen in your previous run on the same branch
  are badged **New**, tracked locally per (repo, branch) in workspace state
  (not git — this one's just local bookkeeping, unlike suppressions).
- **Create test file from a scenario** — "Unit test coverage gaps" scenarios
  now also offer "Create test file…", which opens a save dialog pre-filled
  with a sensible location (co-located with the affected file, or mirrored
  into `src/test/...` for Maven/Gradle-style layouts) instead of copy/paste
  only.
- Added `.gto-ignore.json` to the default `gto.excludePatterns` — editing
  your suppression list no longer counts as a "changed file" to re-analyze.

## 0.8.0

- **Backend**: the `remediation` agent now generates real, actual before/after
  code fixes for issues beyond the 7 deterministic patterns — not just text
  suggestions. Each is verified against the actual diff content before being
  kept (rejected if the claimed line doesn't match exactly, guarding against
  a hallucinated fix silently corrupting a file on Apply). These show up
  automatically in the panel's existing "Apply fix" UI, labeled
  **"AI-suggested — review before applying"** to distinguish them from the
  high-confidence deterministic fixes.
- **Backend**: fixed `qa_scenarios` generating bogus scenarios (and a
  meaningless fallback test skeleton) for non-code files — a log or data file
  whose *content* happened to contain words like "auth" or "migration" was
  being treated as if it needed a unit test. Scenarios are now dropped when
  none of their affected files resolve to a real recognized language.

## 0.7.1

- `npm run vsix` now packages to `gto-pr-review-<version>.vsix` instead of a
  fixed filename, so successive builds don't overwrite each other.
- Renamed the "Missing test scenarios" panel section to "Unit test coverage
  gaps".

## 0.7.0

- Results panel now surfaces two things the backend already produced but the
  extension never displayed:
  - **Suggested fixes** — the remediation agent's text-level fix
    descriptions (below Top Issues), for findings that don't have a
    deterministic `code_fixes` patch.
  - **Missing test scenarios** (Thorough preset) — each scenario from the
    `qa_scenarios` agent, with a real language-aware test skeleton
    (Arrange/Act/Assert, matched to the actual changed function) shown as an
    expandable code block with a "Copy code" button.
  - Both are included in "Copy as Markdown" too.

## 0.6.0

- Results panel improvements:
  - Long issue titles are truncated at a word boundary (with `…`) instead of
    being left to display a raw mid-word cut, which can happen when an
    upstream LLM response hit its output-token budget.
  - The "Files changed" list is now clickable, jumping to each file like the
    issue cards above it.
  - When the `remediation` agent ran (Thorough preset) and a suggested fix
    matches an issue's file/line, it now shows inline as an expandable diff
    with an "Apply fix" button — same staleness-checked apply logic as the
    editor's Quick Fix lightbulb, just reachable from the panel too.
  - "Copy as Markdown" button — copies the whole report (gate, issues,
    files changed) to the clipboard for pasting into a PR description or chat.

## 0.5.0

- New `gto.excludePatterns` setting — `.gitignore`-flavored glob patterns
  dropped from every analysis, on top of what `.gitignore` already hides.
  Applies to both `Analyze Changes` and `Analyze Branch...`. Defaults cover
  IDE/tooling noise (`.vscode/**`, `.claude/**`, `.idea/**`, `.trunk/**`) plus
  build/dependency output across languages: JS/TS (`node_modules`, lock
  files), Java/Kotlin (`target`, `.gradle`, `*.class`), Python (`__pycache__`,
  `venv`, `.pytest_cache`, `.mypy_cache`, `egg-info`), .NET (`obj` — not
  `bin`, since some projects ship real code there), Go/PHP/Ruby (`vendor`),
  and Swift (`.build`, `DerivedData`).

## 0.4.0

- `GTO: Analyze Changes` now includes untracked files (never `git add`ed),
  not just staged/unstaged changes to files git already tracks — new files
  no longer need to be staged just to be analyzed. Untracked files are
  diffed via `git diff --no-index` against `/dev/null`, which never touches
  the index (no silent `git add`).

## 0.3.1

- Added `LICENSE` (all rights reserved) and a marketplace icon
  (`media/icon.png`) — clears both `vsce package` warnings. The icon reuses
  the same UOB navy/red mark as the web app's sidebar
  (`frontend/src/components/Sidebar.jsx`), adapted to a square glyph.

## 0.3.0

- Quick Fix (💡) actions from `remediation.code_fixes` — deterministic
  suggested fixes, applied via exact before/after line match (withheld if the
  file changed since analysis). Only available when the Thorough preset ran.
- Runtime agent-depth quick-pick on both analyze commands, defaulting to
  the configured `gto.agentPreset` setting (Enter keeps the default).
- Skips redundant re-runs — an unchanged diff/depth/priorities combo
  redisplays the cached result instead of re-hitting the backend.
- Status bar now shows the last gate result (✓/⚠/⛔, colored for HOLD/BLOCK)
  instead of a static label.
- Multi-root workspaces: prompts which repo to analyze when more than one is
  open; silent (no added friction) when there's only one.

## 0.2.0

- `GTO: Analyze Branch...` — quick-pick a base branch, three-dot diff against it.
- Optional "analysis priorities" free-text prompt on both analyze commands,
  with real-time guardrail validation (ported from the web app).
- Inline diagnostics (squiggly underlines + Problems panel entries), shown
  alongside the results panel, not instead of it.
- `gto.autoAnalyzeOnSave` setting (default **off**) — re-run "Analyze Changes"
  automatically ~1.5s after a save, debounced, skipped while an analysis is
  already in flight.
- Added `repository` field to package.json.

## 0.1.0

- Initial lean v1: `GTO: Analyze Changes` (uncommitted diff, Fast preset by
  default), `GTO: Set API Key`, `GTO: Show Last Result`. Results shown in a
  webview panel; click a finding to jump to file:line.
