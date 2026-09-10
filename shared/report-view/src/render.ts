// shared/report-view/src/render.ts
// -----------------------------------------------------------------------------
// Pure HTML/Markdown rendering for a GTO AnalysisReport. No host API calls
// (no `vscode`, no JCEF bridge) — every function here is a pure function of
// the data passed in. See README.md for the split between this file
// (what a report LOOKS like) and each host's own interactivity script (how a
// button click reaches that host).

import {
  CorrelatedIssue, CodeFix, QAScenario, MermaidDiagram, SuppressedEntry, ReportViewModel, ReportViewOpts,
} from './types';

export const GATE_META: Record<string, { label: string; color: string }> = {
  APPROVE: { label: '✓ APPROVE', color: 'var(--vscode-testing-iconPassed, #3fb950)' },
  HOLD: { label: '⚠ HOLD', color: 'var(--vscode-editorWarning-foreground, #d29922)' },
  BLOCK: { label: '⛔ BLOCK', color: 'var(--vscode-testing-iconFailed, #f85149)' },
};

export const SEVERITY_COLOR: Record<string, string> = {
  critical: 'var(--vscode-testing-iconFailed, #f85149)',
  high: 'var(--vscode-testing-iconFailed, #f85149)',
  medium: 'var(--vscode-editorWarning-foreground, #d29922)',
  low: 'var(--vscode-descriptionForeground, #9fadbf)',
};

/** Stable identity for one finding — (file, line) is enough in practice: if
 * the line moves, treating it as a "new" location (rather than matching a
 * stale suppression/seen-entry to the wrong code) is the safer failure mode.
 * This is the ONE canonical copy for anything that can import TS/JS
 * directly (vscode-extension/src/reportState.ts re-exports this rather than
 * redefining it). The IntelliJ plugin can't share code across languages, so
 * GtoReportState.kt carries its own Kotlin port — MUST stay byte-identical
 * to this formula; keep both in sync. */
export function fingerprint(filePath: string, line: number): string {
  return `${filePath}:${line || 0}`;
}

const LINE_RE = /@@ line (\d+) @@/;

/** Line number a CodeFix targets (0-based), or null if its `diff` doesn't
 * carry the `@@ line N @@` marker fix_generator.py always emits. */
export function parseFixLine(fix: CodeFix): number | null {
  const m = LINE_RE.exec(fix.diff);
  return m ? parseInt(m[1], 10) - 1 : null;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));
}

/** Cuts long text at the last word boundary before `max` chars, rather than
 * mid-word — some upstream findings (esp. LLM output cut off by a max-tokens
 * budget) can otherwise arrive already truncated mid-sentence; this at least
 * keeps what's displayed from *looking* broken. */
export function truncateAtWord(s: string, max: number): string {
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const lastSpace = cut.lastIndexOf(' ');
  const trimmed = lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut;
  return trimmed.trimEnd() + '…';
}

export const BASE_STYLE = `
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 12px 16px; }
  .dim { color: var(--vscode-descriptionForeground); }
  .err { color: var(--vscode-errorForeground); }
  h2 { margin: 0 0 4px; }
`;

/** The rest of the report's CSS — everything renderReportBody's markup
 * references beyond BASE_STYLE. Kept as one block (not per-component) since
 * it's all one stylesheet either way and splitting it buys nothing. */
export function renderReportStyles(gateColor: string): string {
  return `
    ${BASE_STYLE}
    .mermaid-wrap { overflow-x: auto; margin: 8px 0; padding: 8px; background: var(--vscode-textCodeBlock-background); border-radius: 4px; }
    .mermaid-wrap .mermaid { display: flex; justify-content: center; }
    .top-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .gate { font-size: 20px; font-weight: 700; color: ${gateColor}; }
    .meta { display: flex; gap: 16px; font-size: 12px; color: var(--vscode-descriptionForeground); margin: 6px 0 16px; }
    .issue { border-top: 1px solid var(--vscode-panel-border); padding: 10px 0; cursor: pointer; }
    .issue:hover { background: var(--vscode-list-hoverBackground); }
    .issue-title { font-weight: 500; margin-bottom: 3px; }
    .sev { font-size: 10px; font-weight: 700; text-transform: uppercase; padding: 1px 6px; border-radius: 8px;
           border: 1px solid currentColor; margin-right: 6px; }
    .loc { font-family: var(--vscode-editor-font-family, monospace); font-size: 12px; color: var(--vscode-descriptionForeground); }
    .agents { font-size: 11px; color: var(--vscode-descriptionForeground); margin-top: 2px; }
    details { margin-top: 16px; }
    summary { cursor: pointer; font-weight: 500; }
    .files { font-family: var(--vscode-editor-font-family, monospace); font-size: 12px; margin: 6px 0 0 0; padding-left: 18px; }
    .file-item { cursor: pointer; padding: 1px 0; }
    .file-item:hover { text-decoration: underline; color: var(--vscode-textLink-foreground); }
    .errs { color: var(--vscode-errorForeground); font-size: 12px; margin-top: 10px; }
    button.gto-btn { font-family: var(--vscode-font-family); font-size: 12px; padding: 3px 10px; border-radius: 3px;
      border: 1px solid var(--vscode-button-border, transparent); background: var(--vscode-button-secondaryBackground);
      color: var(--vscode-button-secondaryForeground); cursor: pointer; }
    button.gto-btn:hover { background: var(--vscode-button-secondaryHoverBackground); }
    .fix { margin: 8px 0 0; padding: 8px 10px; background: var(--vscode-textCodeBlock-background); border-radius: 4px; cursor: default; }
    .fix summary { font-size: 12px; }
    .ai-tag { font-size: 10px; font-weight: 700; text-transform: uppercase; padding: 1px 6px; border-radius: 8px;
      border: 1px solid var(--vscode-editorWarning-foreground, #d29922); color: var(--vscode-editorWarning-foreground, #d29922); margin-left: 4px; }
    .diff { font-family: var(--vscode-editor-font-family, monospace); font-size: 11.5px; white-space: pre-wrap;
      word-break: break-word; margin: 6px 0; padding: 6px 8px; background: var(--vscode-editor-background);
      border-radius: 3px; line-height: 1.5; }
    .diff-add { color: var(--vscode-gitDecoration-addedResourceForeground, #3fb950); }
    .diff-del { color: var(--vscode-gitDecoration-deletedResourceForeground, #f85149); }
    .diff-ctx { color: var(--vscode-descriptionForeground); }
    .apply-btn[data-status="applied"] { color: var(--vscode-testing-iconPassed, #3fb950); }
    .apply-btn[data-status="stale"], .apply-btn[data-status="error"] { color: var(--vscode-errorForeground); }
    .suggestions { font-size: 12.5px; margin: 4px 0 0; padding-left: 18px; line-height: 1.6; }
    .scenario { border-top: 1px solid var(--vscode-panel-border); padding: 10px 0; }
    .scenario-type { font-size: 11px; color: var(--vscode-descriptionForeground); text-transform: uppercase; margin-right: 6px; }
    .scenario .file-item { display: inline-block; margin-right: 10px; }
    .new-tag { font-size: 10px; font-weight: 700; text-transform: uppercase; padding: 1px 6px; border-radius: 8px;
      border: 1px solid var(--vscode-textLink-foreground); color: var(--vscode-textLink-foreground); margin-left: 4px; }
    .suppress-btn { background: none; border: none; color: var(--vscode-descriptionForeground); cursor: pointer;
      font-size: 11px; padding: 0; float: right; }
    .suppress-btn:hover { color: var(--vscode-errorForeground); text-decoration: underline; }
    .fp-btn { background: none; border: none; color: var(--vscode-descriptionForeground); cursor: pointer;
      font-size: 11px; padding: 0; float: right; margin-right: 12px; }
    .fp-btn:hover { color: var(--vscode-editorWarning-foreground); text-decoration: underline; }
    .fp-btn:disabled { cursor: default; text-decoration: none; }
    .explain-btn { background: none; border: none; color: var(--vscode-descriptionForeground); cursor: pointer;
      font-size: 11px; padding: 0; float: right; margin-right: 12px; }
    .explain-btn:hover { color: var(--vscode-textLink-foreground); text-decoration: underline; }
    .explain-btn:disabled { cursor: default; text-decoration: none; }
    .suppressed-row { border-top: 1px solid var(--vscode-panel-border); padding: 6px 0; font-size: 12px;
      display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .suppressed-row .info { color: var(--vscode-descriptionForeground); }
    .path-review-banner { font-size: 12px; color: var(--vscode-descriptionForeground);
      background: var(--vscode-textCodeBlock-background); padding: 5px 10px; border-radius: 4px;
      margin: 8px 0 0; }
    .path-review-banner code { font-family: var(--vscode-editor-font-family, monospace); }
    #similarPrs summary { cursor: pointer; font-size: 12px; color: var(--vscode-descriptionForeground); margin: 8px 0 4px; }
    .similar-row { font-size: 12px; padding: 3px 0; display: flex; gap: 8px; align-items: baseline; }
    .sim-pct { font-variant-numeric: tabular-nums; color: var(--vscode-textLink-foreground); min-width: 34px; }
  `;
}

/** Makes .gto.yaml's effect visible — without this, path-scoped rules only
 * ever show up as "fewer findings than expected," with nothing telling the
 * user a config file was even read. */
export function pathReviewBannerHtml(r: ReportViewModel): string {
  const s = r.path_review_summary;
  if (!s || (!s.agents_excluded?.length && !s.steering_applied)) return '';
  const parts: string[] = [];
  if (s.agents_excluded?.length) {
    parts.push(`skipped ${s.agents_excluded.length} agent(s) (${s.agents_excluded.map(escapeHtml).join(', ')})`);
  }
  if (s.steering_applied) parts.push('applied path-scoped priorities');
  return `<p class="path-review-banner">📄 <code>.gto.yaml</code> — ${parts.join('; ')}</p>`;
}

export function findFixForIssue(issue: CorrelatedIssue, fixes: CodeFix[]): CodeFix | undefined {
  if (!issue.file_path) return undefined;
  const sameFile = fixes.filter((f) => f.file_path === issue.file_path);
  if (!sameFile.length) return undefined;
  const exact = sameFile.find((f) => {
    const line = parseFixLine(f);
    return line !== null && line + 1 === issue.line;
  });
  return exact ?? sameFile[0];
}

export function renderDiffLines(diff: string): string {
  return diff
    .split('\n')
    .map((l) => {
      const esc = escapeHtml(l);
      if (l.startsWith('+') && !l.startsWith('+++')) return `<span class="diff-add">${esc}</span>`;
      if (l.startsWith('-') && !l.startsWith('---')) return `<span class="diff-del">${esc}</span>`;
      return `<span class="diff-ctx">${esc}</span>`;
    })
    .join('\n');
}

export function fixHtml(fix: CodeFix, file: string): string {
  const line0 = parseFixLine(fix);
  if (line0 === null) return '';
  // Deterministic fixes (agents/fix_generator.py, regex-matched) are always
  // "high" confidence. "low" marks an LLM-proposed patch — plausible but not
  // guaranteed correct the way a mechanical regex match is; label it so a
  // reviewer knows to actually read it before clicking Apply.
  const aiTag = fix.confidence === 'low' ? ' <span class="ai-tag">AI-suggested — review before applying</span>' : '';
  return `<details class="fix">
    <summary>💡 Suggested fix: ${escapeHtml(fix.title)}${aiTag}</summary>
    ${fix.explanation ? `<p class="dim" style="margin:6px 0;">${escapeHtml(fix.explanation)}</p>` : ''}
    <pre class="diff">${renderDiffLines(fix.diff)}</pre>
    <button class="gto-btn apply-btn" data-file="${escapeHtml(file)}" data-line0="${line0}">Apply fix</button>
  </details>`;
}

/** The remediation agent's text-level fix descriptions — not tied to a
 * specific top_issue 1:1, so shown as its own list rather than merged into
 * issue cards (unlike code_fixes, which do match a specific issue/line). */
export function fixSuggestionsHtml(suggestions: string[]): string {
  if (!suggestions.length) return '';
  return `<h2 style="margin-top:16px;font-size:14px;">Suggested fixes (${suggestions.length})</h2>
    <ul class="suggestions">${suggestions.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ul>`;
}

export function scenarioHtml(s: QAScenario): string {
  const color = SEVERITY_COLOR[(s.priority || 'medium').toLowerCase()] ?? SEVERITY_COLOR.low;
  const filesHtml = s.affected_files?.length
    ? `<div class="loc">${s.affected_files
        .map((f) => `<span class="file-item" data-file="${escapeHtml(f)}">${escapeHtml(f)}</span>`)
        .join('')}</div>`
    : '';
  const firstFile = s.affected_files?.[0] ?? '';
  const skeletonHtml = s.test_skeleton
    ? `<details class="fix">
        <summary>🧪 Test skeleton${s.test_skeleton_filename ? ` — ${escapeHtml(s.test_skeleton_filename)}` : ''}</summary>
        <pre class="diff">${escapeHtml(s.test_skeleton)}</pre>
        <button class="gto-btn copy-skeleton-btn" data-id="${escapeHtml(s.id)}" data-code="${escapeHtml(s.test_skeleton)}">Copy code</button>
        ${
          s.test_skeleton_filename
            ? `<button class="gto-btn create-test-btn" data-affected="${escapeHtml(firstFile)}" data-filename="${escapeHtml(s.test_skeleton_filename)}" data-code="${escapeHtml(s.test_skeleton)}" style="margin-left:6px;">Create test file…</button>`
            : ''
        }
      </details>`
    : '';
  return `<div class="scenario">
    <div class="issue-title">
      <span class="sev" style="color:${color}">${escapeHtml(s.priority || 'medium')}</span>
      <span class="scenario-type">${escapeHtml(s.type || '')}</span>${escapeHtml(s.title)}
    </div>
    ${s.description ? `<p class="dim" style="margin:4px 0;">${escapeHtml(s.description)}</p>` : ''}
    ${filesHtml}
    ${skeletonHtml}
  </div>`;
}

export function qaScenariosHtml(scenarios: QAScenario[]): string {
  if (!scenarios.length) return '';
  return `<h2 style="margin-top:16px;font-size:14px;">Unit test coverage gaps (${scenarios.length})</h2>
    ${scenarios.map(scenarioHtml).join('')}`;
}

/** Renders each diagram inline via a bundled mermaid.js the HOST resolves
 * (loaded as `mermaidUri` — a webview-local resource on VS Code, an
 * equivalent JCEF-resolvable resource URI on IntelliJ), alongside the raw
 * source in a <details> for copy/paste elsewhere. Falls back to raw-source-
 * only when mermaidUri is undefined — never a broken page. */
export function diagramsHtml(diagrams: MermaidDiagram[], mermaidUri?: string): string {
  if (!diagrams.length) return '';
  return `<h2 style="margin-top:16px;font-size:14px;">Sequence diagrams (${diagrams.length})</h2>
    ${diagrams
      .map(
        (d, i) => `<div>
          ${mermaidUri ? `<div class="mermaid-wrap"><div class="mermaid">${escapeHtml(d.mermaid_source)}</div></div>` : ''}
          <details class="fix">
            <summary>📈 ${escapeHtml(d.diagram_type || 'sequenceDiagram')}
              <span class="ai-tag">AI-generated — not verified against the real call graph</span>
            </summary>
            ${d.note ? `<p class="dim" style="margin:6px 0;">${escapeHtml(d.note)}</p>` : ''}
            <pre class="diff">${escapeHtml(d.mermaid_source)}</pre>
            <button class="gto-btn copy-skeleton-btn" data-id="diagram-${i}" data-code="${escapeHtml(d.mermaid_source)}">Copy code</button>
          </details>
        </div>`
      )
      .join('')}`;
}

export function issueHtml(it: CorrelatedIssue, fix: CodeFix | undefined, isNew: boolean): string {
  const color = SEVERITY_COLOR[it.severity?.toLowerCase()] ?? SEVERITY_COLOR.low;
  const fp = fingerprint(it.file_path, it.line);
  const clickable = it.file_path ? ` data-file="${escapeHtml(it.file_path)}" data-line="${it.line || 1}"` : '';
  const newTag = isNew ? ' <span class="new-tag">New</span>' : '';
  const suppressBtn = `<button class="suppress-btn" data-fingerprint="${escapeHtml(fp)}" data-file="${escapeHtml(it.file_path)}" data-line="${it.line || 0}" data-title="${escapeHtml(it.title)}" title="Suppress this finding — stops it reappearing on future runs">🚫 Ignore</button>`;
  const fpBtn = `<button class="fp-btn" data-fingerprint="${escapeHtml(fp)}" data-agent="${escapeHtml(it.agents?.[0] ?? '')}" data-category="${escapeHtml(it.categories?.[0] ?? '')}" data-file="${escapeHtml(it.file_path)}" title="Mark as a false positive — after enough of these on this repo, GTO auto-suppresses this pattern on future runs (see the Insights tab in the web app)">🚩 False positive</button>`;
  const explainBtn = `<button class="explain-btn" data-fingerprint="${escapeHtml(fp)}" data-agent="${escapeHtml(it.agents?.[0] ?? '')}" data-category="${escapeHtml(it.categories?.[0] ?? '')}" data-file="${escapeHtml(it.file_path)}" data-title="${escapeHtml(it.title)}" title="Ask GTO to explain why this was flagged">❓ Explain</button>`;
  return `<div class="issue" data-fingerprint="${escapeHtml(fp)}"${clickable}>
    ${suppressBtn}
    ${fpBtn}
    ${explainBtn}
    <div class="issue-title">
      <span class="sev" style="color:${color}">${escapeHtml(it.severity || 'info')}</span>${escapeHtml(truncateAtWord(it.title, 240))}${newTag}
    </div>
    ${it.file_path ? `<div class="loc">${escapeHtml(it.file_path)}${it.line ? ':' + it.line : ''}</div>` : ''}
    ${it.agents?.length ? `<div class="agents">${it.agents.map(escapeHtml).join(', ')}</div>` : ''}
    ${fix ? fixHtml(fix, it.file_path) : ''}
    <div class="explain-slot" data-fingerprint="${escapeHtml(fp)}"></div>
  </div>`;
}

export function suppressedListHtml(suppressed: SuppressedEntry[]): string {
  if (!suppressed.length) return '';
  return `<details><summary>Suppressed findings (${suppressed.length})</summary>
    ${suppressed
      .map(
        (s) => `<div class="suppressed-row" data-fingerprint="${escapeHtml(s.fingerprint)}">
          <span class="info">${escapeHtml(s.title || s.file_path)}${s.reason ? ' — ' + escapeHtml(s.reason) : ''}</span>
          <button class="gto-btn unsuppress-btn" data-fingerprint="${escapeHtml(s.fingerprint)}">Unsuppress</button>
        </div>`
      )
      .join('')}
  </details>`;
}

/** Everything between <body> and the host's own interactivity <script> —
 * the top bar, meta line, issues, fixes, diagrams, QA scenarios, suppressed
 * list, files list, and errors. Buttons carry data-* attributes only; the
 * calling host appends its own script (see README.md) to make them do
 * anything. `mermaidUri`, when given, both flips diagramsHtml() into
 * rendering an inline .mermaid block AND is used here for the actual
 * `<script src>` tag that loads the mermaid runtime — omit it (undefined)
 * to render as before mermaid existed: raw copyable source only. */
export function renderReportBody(r: ReportViewModel, opts: ReportViewOpts, mermaidUri?: string): string {
  const gate = GATE_META[r.gate_decision] ?? { label: r.gate_decision, color: 'var(--vscode-foreground)' };
  const score = r.risk?.risk_score ?? 0;
  const issues = r.top_issues ?? [];
  const fixes = r.remediation?.code_fixes ?? [];
  const highConfidenceFixes = fixes.filter((f) => f.confidence === 'high');

  const issuesHtml = issues.length
    ? issues.map((it) => issueHtml(it, findFixForIssue(it, fixes), opts.newFingerprints.has(fingerprint(it.file_path, it.line)))).join('')
    : `<p class="dim">No issues found — looks clean.</p>`;

  const suppressedHtml = suppressedListHtml(opts.suppressed);

  const filesHtml = r.files_changed_list?.length
    ? `<details><summary>Files changed (${r.files_changed_list.length})</summary>
         <ul class="files">${r.files_changed_list
           .map((f) => `<li class="file-item" data-file="${escapeHtml(f)}">${escapeHtml(f)}</li>`)
           .join('')}</ul>
       </details>`
    : '';

  return `<div class="top-row">
      <div class="gate">${gate.label}</div>
      <div>
        ${
          highConfidenceFixes.length > 0
            ? `<button class="gto-btn" id="applyAll" style="margin-right:6px;">Apply all (${highConfidenceFixes.length})</button>`
            : ''
        }
        <button class="gto-btn" id="copyMd">Copy as Markdown</button>
        <button class="gto-btn" id="postToPr" style="margin-left:6px;">📮 Post to PR</button>
        <button class="gto-btn" id="approvePr" style="margin-left:6px;">✅ Approve PR</button>
      </div>
    </div>
    <div class="meta">
      <span>Risk score: ${score}/100</span>
      <span>${r.files_changed} file${r.files_changed === 1 ? '' : 's'} changed</span>
      <span>${r.duration_s?.toFixed?.(1) ?? '—'}s</span>
    </div>
    ${r.risk?.rationale ? `<p class="dim">${escapeHtml(r.risk.rationale)}</p>` : ''}
    ${pathReviewBannerHtml(r)}
    <div id="similarPrs"></div>
    <h2 style="margin-top:16px;font-size:14px;">Top issues (${issues.length})</h2>
    ${issuesHtml}
    ${fixSuggestionsHtml(r.remediation?.fix_suggestions ?? [])}
    ${diagramsHtml(r.remediation?.diagrams ?? [], mermaidUri)}
    ${qaScenariosHtml(r.qa_scenarios?.scenarios ?? [])}
    ${suppressedHtml}
    ${filesHtml}
    ${r.errors?.length ? `<div class="errs">${r.errors.map(escapeHtml).join('<br>')}</div>` : ''}
    ${mermaidUri ? `<script src="${mermaidUri}"></script>` : ''}`;
}

/** "Copy as Markdown" / PR-comment text — shared for the same reason as the
 * HTML above: one report, one textual representation, regardless of host. */
export function reportToMarkdown(r: ReportViewModel): string {
  const score = r.risk?.risk_score ?? 0;
  const issues = r.top_issues ?? [];
  const lines: string[] = [];

  lines.push(`# GTO Review — ${r.gate_decision}`);
  lines.push('');
  lines.push(`Risk score: ${score}/100 · ${r.files_changed} file${r.files_changed === 1 ? '' : 's'} changed · ${r.duration_s?.toFixed?.(1) ?? '—'}s`);
  if (r.risk?.rationale) {
    lines.push('');
    lines.push(r.risk.rationale);
  }
  const prs = r.path_review_summary;
  if (prs && (prs.agents_excluded?.length || prs.steering_applied)) {
    lines.push('');
    const bits: string[] = [];
    if (prs.agents_excluded?.length) bits.push(`skipped agent(s): ${prs.agents_excluded.join(', ')}`);
    if (prs.steering_applied) bits.push('applied path-scoped priorities');
    lines.push(`\`.gto.yaml\` — ${bits.join('; ')}`);
  }
  lines.push('');
  lines.push(`## Top issues (${issues.length})`);
  for (const it of issues) {
    lines.push('');
    lines.push(`### [${(it.severity || 'info').toUpperCase()}] ${it.title}`);
    if (it.file_path) lines.push(`- File: \`${it.file_path}${it.line ? ':' + it.line : ''}\``);
    if (it.agents?.length) lines.push(`- Agents: ${it.agents.join(', ')}`);
  }
  const suggestions = r.remediation?.fix_suggestions ?? [];
  if (suggestions.length) {
    lines.push('');
    lines.push(`## Suggested fixes (${suggestions.length})`);
    for (const s of suggestions) lines.push(`- ${s}`);
  }
  const diagrams = r.remediation?.diagrams ?? [];
  if (diagrams.length) {
    lines.push('');
    lines.push(`## Sequence diagrams (${diagrams.length})`);
    for (const d of diagrams) {
      lines.push('');
      lines.push('AI-generated — not verified against the real call graph.');
      lines.push('```mermaid');
      lines.push(d.mermaid_source);
      lines.push('```');
    }
  }
  const scenarios = r.qa_scenarios?.scenarios ?? [];
  if (scenarios.length) {
    lines.push('');
    lines.push(`## Unit test coverage gaps (${scenarios.length})`);
    for (const s of scenarios) {
      lines.push('');
      lines.push(`### [${(s.priority || 'medium').toUpperCase()}] ${s.title} (${s.type})`);
      if (s.description) lines.push(s.description);
      if (s.affected_files?.length) lines.push(`- Files: ${s.affected_files.map((f) => `\`${f}\``).join(', ')}`);
      if (s.test_skeleton) {
        lines.push('');
        if (s.test_skeleton_filename) lines.push(`**${s.test_skeleton_filename}**`);
        lines.push('```');
        lines.push(s.test_skeleton);
        lines.push('```');
      }
    }
  }
  if (r.files_changed_list?.length) {
    lines.push('');
    lines.push(`## Files changed (${r.files_changed_list.length})`);
    for (const f of r.files_changed_list) lines.push(`- \`${f}\``);
  }
  if (r.errors?.length) {
    lines.push('');
    lines.push('## Errors');
    for (const e of r.errors) lines.push(`- ${e}`);
  }
  return lines.join('\n');
}
