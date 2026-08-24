# Changelog

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
