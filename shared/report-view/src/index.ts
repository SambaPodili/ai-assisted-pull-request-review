// shared/report-view/src/index.ts
// -----------------------------------------------------------------------------
// Public API. See README.md for the shared/host-specific split this package
// draws, and for the --vscode-* CSS token contract every host must satisfy.

export * from './types';
export {
  GATE_META,
  SEVERITY_COLOR,
  BASE_STYLE,
  fingerprint,
  parseFixLine,
  escapeHtml,
  truncateAtWord,
  renderReportStyles,
  renderReportBody,
  reportToMarkdown,
  pathReviewBannerHtml,
  findFixForIssue,
  renderDiffLines,
  fixHtml,
  fixSuggestionsHtml,
  scenarioHtml,
  qaScenariosHtml,
  diagramsHtml,
  issueHtml,
  suppressedListHtml,
} from './render';

import { GATE_META } from './render';

/** `{label, color}` for a gate decision, with the same fallback every
 * renderer needs — one place so renderReportBody's <div class="gate"> and
 * renderReportStyles' `.gate { color: ... }` rule can't drift apart. */
export function resolveGate(gateDecision: string): { label: string; color: string } {
  return GATE_META[gateDecision] ?? { label: gateDecision, color: 'var(--vscode-foreground)' };
}
