# GTO Pull Request Review Framework — VS Code Extension

Run the multi-agent PR review without leaving the editor.

## Commands

- **`GTO: Analyze Changes`** (Command Palette, or the status bar button) — diffs
  your working tree against `HEAD` (staged + unstaged combined), quick-picks
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

If you have more than one git repo open in the workspace, both analyze
commands ask which one to use (skipped entirely when there's only one — no
added friction for the common case).

If nothing changed since your last run (identical diff, depth, and
priorities), re-running shows the cached result instantly instead of
re-hitting the backend.

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
  findings. Click a finding to jump to that file and line.
- **Inline diagnostics** — critical/high findings show as errors, medium as
  warnings, low as informational hints, both as squiggly underlines in the
  editor and as entries in the native Problems panel (`Cmd+Shift+M` /
  `Ctrl+Shift+M`).
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
npx @vscode/vsce package --no-dependencies -o gto-pr-review.vsix
```

Then in VS Code: **Extensions panel → `...` → Install from VSIX...** and
pick the file. Re-run this command and reinstall after any source change —
an installed `.vsix` is a frozen snapshot, it doesn't hot-reload.

(`--no-dependencies` is used because this extension has no runtime
`dependencies` — everything is bundled by esbuild into `dist/extension.js`.)
