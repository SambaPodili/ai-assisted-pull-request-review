# @gto/report-view

The single source of truth for what a GTO review result **looks like** —
shared between the VS Code extension and the IntelliJ plugin, so a finding
card, a fix diff, a QA test skeleton, or a rendered Mermaid diagram look and
behave identically no matter which IDE you're reviewing in, and a visual fix
only has to be made once.

## What's in here vs. what isn't

This package renders **markup**, never wires up **interactivity**. Concretely:

- `renderReportBody()` / `renderReportStyles()` produce the HTML/CSS for the
  whole report — top bar, issue list, fix diffs, QA scenarios, diagrams,
  suppressed list, files list. Every button in that markup carries a
  `data-*` attribute (`data-fingerprint`, `data-file`, `data-code`, …) but
  **no** `onclick` and no assumption about how a click reaches the host.
- Wiring those buttons to `postMessage` (VS Code) or a `JBCefJSQuery` bridge
  (IntelliJ) is each host's own, small, separate script — see
  `vscode-extension/src/resultsPanel.ts`'s trailing `<script>` block for the
  VS Code side. That code is inherently host-specific and deliberately
  **not** here.
- `reportToMarkdown()` is the "Copy as Markdown" / PR-comment text, also
  shared for the same reason.

## Styling contract: `--vscode-*` design tokens

The CSS in `render.ts` is written entirely against `--vscode-*` custom
properties (`var(--vscode-foreground)`, `var(--vscode-editor-background)`,
…). VS Code's webview host defines these automatically, matching the user's
real editor theme. **A non-VS-Code host (IntelliJ's JCEF) must define the
same variable names itself**, mapped from its own theme, before injecting
this package's CSS — otherwise text/borders/backgrounds with no inline
fallback render unstyled.

The full set of tokens this package's CSS currently uses (keep this list in
sync if you add a new one):

```
--vscode-button-border
--vscode-button-secondaryBackground
--vscode-button-secondaryForeground
--vscode-button-secondaryHoverBackground
--vscode-descriptionForeground
--vscode-editor-background
--vscode-editor-font-family
--vscode-editorWarning-foreground
--vscode-errorForeground
--vscode-font-family
--vscode-foreground
--vscode-gitDecoration-addedResourceForeground
--vscode-gitDecoration-deletedResourceForeground
--vscode-list-hoverBackground
--vscode-panel-border
--vscode-testing-iconFailed
--vscode-testing-iconPassed
--vscode-textCodeBlock-background
--vscode-textLink-foreground
```

The IntelliJ plugin supplies these by generating a `:root { --vscode-foo:
...; }` block from `EditorColorsManager`/`JBColor` at render time (see
`intellij-plugin`'s results tool window) — same token names, IntelliJ-theme
values.

## Consumers

- **VS Code**: `vscode-extension/src/resultsPanel.ts` imports this package
  directly from TypeScript source (`import { ... } from '../../shared/
  report-view/src'`) — esbuild bundles it into `extension.js` at build time,
  no separate artifact needed.
- **IntelliJ**: has no TypeScript runtime, so it consumes the **built**
  browser bundle instead — run `npm run build` here to produce
  `dist/report-view.iife.js` (exposes a `window.GtoReportView` global), then
  the plugin loads that file into its JCEF browser and calls
  `GtoReportView.renderReportBody(...)`/`renderReportStyles()` with the
  report JSON serialized from Kotlin.

## Types

`src/types.ts` defines a minimal, dependency-free subset of the report shape
this package's rendering actually reads (not the full `AnalysisReport`
contract used elsewhere) — intentionally decoupled from
`vscode-extension/src/apiClient.ts`'s richer types so this package never
needs the `vscode` module or anything else IDE-specific.
