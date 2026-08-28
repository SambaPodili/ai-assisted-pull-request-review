# Changelog

## 0.14.0

A senior-review batch — seven features aimed at cutting manual reviewer/developer
effort, several of which reuse backend capability that already existed but wasn't
reachable from the plugin.

- **❓ Explain this finding** — a button on every issue card asks GTO to explain
  why a finding was flagged and what to check before dismissing it. Reuses the
  same guardrailed Q&A engine already built for PR chat replies — the question
  is a fixed, never-user-typed constant, so this introduces no new free-text
  surface.
- **Apply all** — a report-level button batch-applies every high-confidence
  deterministic fix in one click, instead of one at a time.
- **Similar past PRs** — the results panel now shows past analyses similar to
  this one (file-overlap + summary-keyword similarity) and their outcomes —
  the same feature the web app's Results view already had, now also here.
- **📮 Post to PR** — post findings as grouped per-file PR comments directly
  from the panel, using the backend's shared bot credential (same identity
  webhook-triggered comments already post under — no personal token needed).
- **✅ Approve PR** — a reviewer can approve a PR (never merges) without
  logging into Bitbucket/GitHub separately. Unlike Post to PR, this requires
  your own personal token (`GTO: Set Personal Git Provider Token`) so the
  approval shows as *you* on the PR, not the shared bot — matters for
  audit/compliance. Backend-gated behind a new dedicated `pr:approve`
  permission, distinct from gate override.
- **`GTO: Install Git Hook`** — a local pre-push hook that runs a Fast-preset
  check on what's about to be pushed. Warn-only by default
  (`gto.gitHookMode`); can be set to `block` to refuse a push on a
  BLOCK-severity finding. `GTO: Uninstall Git Hook` removes it (only if GTO
  installed it).
- **Backend**: incremental re-analysis v1 — when a PR gets pushed to again,
  the backend now checks whether the push actually added any net new code
  (a rebase, a merge commit, a whitespace-only commit) before re-running the
  full pipeline. A trivial push reuses the prior result instead of paying
  full LLM cost again; any push with real content still gets a full
  re-analysis, unchanged from today. Required populating real PR identity
  (head/base SHA) onto every webhook-triggered report, which was silently
  empty before this.

## 0.13.2

- **Sequence diagrams** — when the `remediation` agent generates a Mermaid
  sequence diagram for a complex change (Thorough preset only, and only when
  the change has real reference-impact data at medium+ risk — this backend
  capability existed already but was previously only ever posted to PR
  comments, never shown locally), it now shows in the results panel as a
  labeled, copy-able code block — "AI-generated — not verified against the
  real call graph", same framing as an AI-suggested code fix. Raw Mermaid
  source only (paste into a Mermaid live editor to view) — VS Code's webview
  has no Mermaid runtime bundled, and that's a bigger step than this earns
  until there's real demand for inline rendering. Included in "Copy as
  Markdown" too.

## 0.13.1

- Network-failure error messages now show the real cause instead of Node's
  generic `fetch failed` — e.g. `connect ECONNREFUSED 10.0.0.5:8080` instead
  of a dead end. Node's `fetch()` collapses DNS failures, connection refused,
  timeouts, and TLS errors into one opaque message and buries the actual
  reason on `err.cause`, which was previously dropped silently on both
  `GTO: Analyze Changes`/`Analyze Branch...` and `GTO: Select Model`.

## 0.13.0

- **CodeLens inline annotation** — findings now show as an inline "⚠ N GTO
  issue(s)" annotation right above the flagged line, not just as squiggles/the
  results panel. Click to view (or, for multiple issues on one line,
  quick-pick between) the finding's full detail. Skipped for findings with no
  resolvable line (a real case for some LLM-sourced findings) — a CodeLens
  pinned to line 1 of an unrelated file would be more misleading than useful.
- **Backend**: SARIF 2.1.0 export — `GET /report/{id}/sarif` — for GitHub code
  scanning, SARIF viewers, or SIEM ingestion. Rule ids are built dynamically
  per-report from each finding's CWE/category label.
- **Backend**: one-click compliance report per PR — `GET
  /report/{id}/compliance-report` (requires `audit:read`) — gate rationale,
  human override history, full findings list, and suppression notes as a
  single Markdown document. v1 is Markdown only (no PDF library in this repo
  yet) and pulls override history from the existing override store, not a
  full audit-log query (that read path doesn't exist yet either) — both
  limits are stated in the document's own footer.
- **Mark as false positive** — a "🚩 False positive" action on each issue card
  in the results panel, using the same feedback loop the web app already has
  (`POST /report/{id}/feedback`). After enough false-positive verdicts on the
  same repo/agent/category, GTO auto-suppresses that pattern on future runs —
  always visibly noted, never silent.
- **Backend**: team-wide default `.gto.yaml` — set `TEAM_GTO_CONFIG_REPO` (+
  optional `TEAM_GTO_CONFIG_REF`) to a shared repo, and its `.gto.yaml` is
  merged into every webhook-triggered analysis alongside the target repo's
  own file. Merged as a union, never a replacement — a team default can only
  add restriction, the same narrows-never-widens rule a repo's own file
  already follows; it can never loosen scrutiny a repo has set for itself.
  Opt-in — unset by default, changes nothing.

## 0.12.1

- Cosmetic: Bitbucket Server/Data Center's `ssh://git@host:port/PROJ/repo.git`
  remote format now normalizes to a clean `https://host/PROJ/repo` display
  string, matching how GitHub/Bitbucket Cloud remotes already display —
  previously shown as-is (`ssh://...`). Display only, never used for auth;
  local `Analyze Changes`/`Analyze Branch` never touch Bitbucket credentials
  at all — they only read your already-cloned local repo.

## 0.12.0

- **`GTO: Select Model`** — the correct fix for a shared multi-user backend
  (v0.11.0's model-selection settings assumed a personal backend and required
  each user to bring their own credential, which doesn't fit a team server
  where the admin has already configured shared model access). This command
  fetches admin-defined presets (e.g. "Llama", "Qwen") from a new backend
  endpoint (`GET /api/v1/model-presets`, `config/settings.py`'s
  `MODEL_PRESETS`) and lets you pick one — no API key or URL ever needs to
  come from the extension; the backend already has it server-side. New
  `gto.modelPreset` setting stores the choice. The v0.11.0 settings
  (`gto.modelProvider`/`modelName`/`modelBaseUrl`/`modelApiVersion` +
  `GTO: Set Model API Key`) remain available as an advanced manual override
  for a personal backend with a separate provider, but are now clearly
  secondary — ignored whenever `gto.modelPreset` is set.

## 0.11.0

- **Model selection** — new `gto.modelProvider`/`modelName`/`modelBaseUrl`/
  `modelApiVersion` settings and a `GTO: Set Model API Key` command, mirroring
  the web app's Configure → AI Model panel (`llm_config` on the request).
  Entirely opt-in: leaving `gto.modelProvider` empty changes nothing.
- **Backend**: Bitbucket Server/Data Center (self-hosted) support — diff
  fetching (`ingestion/git_client.py`) and webhook parsing
  (`ingestion/webhook_parser.py`) previously only understood Bitbucket
  Cloud's API/payload shape; PR-triggered analysis and chat replies would
  silently never fire on Server. Set `GIT_PROVIDER=bitbucket_server`,
  `BITBUCKET_API_URL` to your server root, `BITBUCKET_WORKSPACE` as the
  project-key fallback. Also wired the existing `GIT_SSL_NO_VERIFY` setting
  into these REST calls (previously only used for git-clone), for a
  corporate server behind a self-signed/internal-CA cert.

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
