// vscode-extension/src/resultsPanel.ts
// -----------------------------------------------------------------------------
// Single-instance webview panel that renders an AnalysisReport. Styled with
// VS Code's own CSS custom properties (--vscode-*) so it matches whatever
// theme the user has, light or dark, with zero extra theming code.

import * as vscode from 'vscode';
import { AnalysisReport, CorrelatedIssue } from './apiClient';

const GATE_META: Record<string, { label: string; color: string }> = {
  APPROVE: { label: '✓ APPROVE', color: 'var(--vscode-testing-iconPassed, #3fb950)' },
  HOLD: { label: '⚠ HOLD', color: 'var(--vscode-editorWarning-foreground, #d29922)' },
  BLOCK: { label: '⛔ BLOCK', color: 'var(--vscode-testing-iconFailed, #f85149)' },
};

const SEVERITY_COLOR: Record<string, string> = {
  critical: 'var(--vscode-testing-iconFailed, #f85149)',
  high: 'var(--vscode-testing-iconFailed, #f85149)',
  medium: 'var(--vscode-editorWarning-foreground, #d29922)',
  low: 'var(--vscode-descriptionForeground, #9fadbf)',
};

export class ResultsPanel {
  private static current: ResultsPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private readonly repoRoot: string;

  private constructor(panel: vscode.WebviewPanel, repoRoot: string) {
    this.panel = panel;
    this.repoRoot = repoRoot;
    this.panel.onDidDispose(() => {
      ResultsPanel.current = undefined;
    });
    this.panel.webview.onDidReceiveMessage(async (msg) => {
      if (msg?.command === 'openFinding' && typeof msg.file === 'string') {
        await openFinding(this.repoRoot, msg.file, Number(msg.line) || 1);
      }
    });
  }

  static showLoading(repoRoot: string): ResultsPanel {
    const p = ResultsPanel.getOrCreate(repoRoot);
    p.panel.webview.html = renderShell('<p class="dim">Submitting to GTO backend…</p>');
    return p;
  }

  static setStatus(text: string): void {
    if (!ResultsPanel.current) return;
    ResultsPanel.current.panel.webview.html = renderShell(`<p class="dim">${escapeHtml(text)}</p>`);
  }

  static showError(message: string, repoRoot: string): void {
    const p = ResultsPanel.getOrCreate(repoRoot);
    p.panel.webview.html = renderShell(`<p class="err">${escapeHtml(message)}</p>`);
  }

  static showReport(report: AnalysisReport, repoRoot: string): void {
    const p = ResultsPanel.getOrCreate(repoRoot);
    p.panel.webview.html = renderReport(report);
  }

  private static getOrCreate(repoRoot: string): ResultsPanel {
    if (ResultsPanel.current) {
      ResultsPanel.current.panel.reveal(vscode.ViewColumn.Beside, true);
      return ResultsPanel.current;
    }
    const panel = vscode.window.createWebviewPanel(
      'gtoResults',
      'GTO Review',
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      { enableScripts: true, retainContextWhenHidden: true }
    );
    ResultsPanel.current = new ResultsPanel(panel, repoRoot);
    return ResultsPanel.current;
  }
}

async function openFinding(repoRoot: string, file: string, line: number): Promise<void> {
  try {
    const uri = vscode.Uri.joinPath(vscode.Uri.file(repoRoot), file);
    const doc = await vscode.workspace.openTextDocument(uri);
    const editor = await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn.One });
    const pos = new vscode.Position(Math.max(0, line - 1), 0);
    editor.selection = new vscode.Selection(pos, pos);
    editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
  } catch {
    vscode.window.showWarningMessage(`Couldn't open ${file}:${line} — file may have moved since analysis ran.`);
  }
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));
}

const BASE_STYLE = `
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 12px 16px; }
  .dim { color: var(--vscode-descriptionForeground); }
  .err { color: var(--vscode-errorForeground); }
  h2 { margin: 0 0 4px; }
`;

function renderShell(bodyHtml: string): string {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>${BASE_STYLE}</style></head><body>${bodyHtml}</body></html>`;
}

function renderReport(r: AnalysisReport): string {
  const gate = GATE_META[r.gate_decision] ?? { label: r.gate_decision, color: 'var(--vscode-foreground)' };
  const score = r.risk?.risk_score ?? 0;
  const issues = r.top_issues ?? [];

  const issuesHtml = issues.length
    ? issues.map(issueHtml).join('')
    : `<p class="dim">No issues found — looks clean.</p>`;

  const filesHtml = r.files_changed_list?.length
    ? `<details><summary>Files changed (${r.files_changed_list.length})</summary>
         <ul class="files">${r.files_changed_list.map((f) => `<li>${escapeHtml(f)}</li>`).join('')}</ul>
       </details>`
    : '';

  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
    ${BASE_STYLE}
    .gate { font-size: 20px; font-weight: 700; color: ${gate.color}; }
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
    .errs { color: var(--vscode-errorForeground); font-size: 12px; margin-top: 10px; }
  </style></head>
  <body>
    <div class="gate">${gate.label}</div>
    <div class="meta">
      <span>Risk score: ${score}/100</span>
      <span>${r.files_changed} file${r.files_changed === 1 ? '' : 's'} changed</span>
      <span>${r.duration_s?.toFixed?.(1) ?? '—'}s</span>
    </div>
    ${r.risk?.rationale ? `<p class="dim">${escapeHtml(r.risk.rationale)}</p>` : ''}
    <h2 style="margin-top:16px;font-size:14px;">Top issues (${issues.length})</h2>
    ${issuesHtml}
    ${filesHtml}
    ${r.errors?.length ? `<div class="errs">${r.errors.map(escapeHtml).join('<br>')}</div>` : ''}
    <script>
      const vscode = acquireVsCodeApi();
      document.querySelectorAll('.issue').forEach(el => {
        el.addEventListener('click', () => {
          vscode.postMessage({ command: 'openFinding', file: el.dataset.file, line: el.dataset.line });
        });
      });
    </script>
  </body></html>`;
}

function issueHtml(it: CorrelatedIssue): string {
  const color = SEVERITY_COLOR[it.severity?.toLowerCase()] ?? SEVERITY_COLOR.low;
  const clickable = it.file_path ? ` data-file="${escapeHtml(it.file_path)}" data-line="${it.line || 1}"` : '';
  return `<div class="issue"${clickable}>
    <div class="issue-title">
      <span class="sev" style="color:${color}">${escapeHtml(it.severity || 'info')}</span>${escapeHtml(it.title)}
    </div>
    ${it.file_path ? `<div class="loc">${escapeHtml(it.file_path)}${it.line ? ':' + it.line : ''}</div>` : ''}
    ${it.agents?.length ? `<div class="agents">${it.agents.map(escapeHtml).join(', ')}</div>` : ''}
  </div>`;
}
