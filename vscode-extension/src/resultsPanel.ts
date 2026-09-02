// vscode-extension/src/resultsPanel.ts
// -----------------------------------------------------------------------------
// Single-instance webview panel that renders an AnalysisReport. Styled with
// VS Code's own CSS custom properties (--vscode-*) so it matches whatever
// theme the user has, light or dark, with zero extra theming code.

import * as vscode from 'vscode';
import { AnalysisReport, CorrelatedIssue, CodeFix, QAScenario, MermaidDiagram, submitFindingFeedback, fetchSimilarPRs, explainFinding, postFindingsToPR, approvePR, ApiError, describeError } from './apiClient';
import { parseFixLine, applyCodeFix } from './codeActions';
import { fingerprint, SuppressedEntry, addSuppression, removeSuppression } from './reportState';
import { ownerRepoFromUrl } from './gitDiff';
import { getGitProvider, getBitbucketToken } from './settings';

export interface ReportViewOpts {
  suppressed: SuppressedEntry[];
  newFingerprints: Set<string>;
  // Needed to call POST /report/{id}/feedback for "mark false positive" — the
  // same reviewer feedback loop the web app's ResultsView already exposes.
  // Optional so showLoading/showError (which never need it) stay unaffected.
  backendUrl?: string;
  apiKey?: string;
  // Needed only for "Approve PR" to read the reviewer's personal token
  // (settings.ts::getBitbucketToken) — "Post to PR" doesn't need this, it
  // uses the shared bot credential instead.
  secrets?: vscode.SecretStorage;
}

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
  private report: AnalysisReport | undefined;
  private opts: ReportViewOpts | undefined;

  private constructor(panel: vscode.WebviewPanel, repoRoot: string) {
    this.panel = panel;
    this.repoRoot = repoRoot;
    this.panel.onDidDispose(() => {
      ResultsPanel.current = undefined;
    });
    this.panel.webview.onDidReceiveMessage(async (msg) => {
      if (msg?.command === 'openFinding' && typeof msg.file === 'string') {
        await openFinding(this.repoRoot, msg.file, Number(msg.line) || 1);
      } else if (msg?.command === 'applyFix' && typeof msg.file === 'string' && typeof msg.line0 === 'number') {
        await this.handleApplyFix(msg.file, msg.line0);
      } else if (msg?.command === 'copyMarkdown') {
        await this.handleCopyMarkdown();
      } else if (msg?.command === 'copyText' && typeof msg.text === 'string') {
        await vscode.env.clipboard.writeText(msg.text);
        this.panel.webview.postMessage({ command: 'copyTextDone', id: msg.id });
      } else if (msg?.command === 'suppressFinding' && typeof msg.fingerprint === 'string') {
        await this.handleSuppress(msg.fingerprint, msg.file ?? '', Number(msg.line) || 0, msg.title ?? '');
      } else if (msg?.command === 'unsuppressFinding' && typeof msg.fingerprint === 'string') {
        await removeSuppression(this.repoRoot, msg.fingerprint);
        this.panel.webview.postMessage({ command: 'unsuppressDone', fingerprint: msg.fingerprint });
      } else if (msg?.command === 'createTestFile') {
        await this.handleCreateTestFile(msg.affectedFile ?? '', msg.filename ?? '', msg.code ?? '');
      } else if (msg?.command === 'markFalsePositive') {
        await this.handleMarkFalsePositive(msg.fingerprint ?? '', msg.agent ?? '', msg.category ?? '', msg.file ?? '');
      } else if (msg?.command === 'applyAllFixes') {
        await this.handleApplyAllFixes();
      } else if (msg?.command === 'explainIssue') {
        await this.handleExplainIssue(msg.fingerprint ?? '', msg.agent ?? '', msg.category ?? '', msg.file ?? '', msg.title ?? '');
      } else if (msg?.command === 'postToPr') {
        await this.handlePostToPr();
      } else if (msg?.command === 'approvePr') {
        await this.handleApprovePr();
      }
    });
  }

  private async handleExplainIssue(fp: string, agent: string, category: string, filePath: string, title: string): Promise<void> {
    if (!this.report || !this.opts?.backendUrl || !this.opts?.apiKey) {
      vscode.window.showErrorMessage('GTO: could not fetch an explanation — no active backend connection for this report.');
      return;
    }
    try {
      const text = await explainFinding(this.opts.backendUrl, this.opts.apiKey, this.report.request_id, {
        agent,
        category,
        file_path: filePath,
        title,
      });
      this.panel.webview.postMessage({ command: 'explainDone', fingerprint: fp, text });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : describeError(err);
      this.panel.webview.postMessage({ command: 'explainDone', fingerprint: fp, text: '', error: message });
    }
  }

  private async handleMarkFalsePositive(fp: string, agent: string, category: string, filePath: string): Promise<void> {
    if (!this.report || !this.opts?.backendUrl || !this.opts?.apiKey) {
      vscode.window.showErrorMessage('GTO: could not submit feedback — no active backend connection for this report.');
      return;
    }
    try {
      await submitFindingFeedback(this.opts.backendUrl, this.opts.apiKey, this.report.request_id, {
        agent,
        category,
        file_path: filePath,
        verdict: 'false_positive',
      });
      this.panel.webview.postMessage({ command: 'fpDone', fingerprint: fp });
      vscode.window.setStatusBarMessage('GTO: recorded as false positive', 2500);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : describeError(err);
      vscode.window.showErrorMessage(`GTO: ${message}`);
    }
  }

  private async handleSuppress(fp: string, filePath: string, line: number, title: string): Promise<void> {
    const reason = await vscode.window.showInputBox({
      title: 'Suppress this finding',
      prompt: `Optional reason — saved to .gto-ignore.json so the team can see why this was suppressed.`,
      placeHolder: 'e.g. "false positive — this is test fixture data"',
      ignoreFocusOut: true,
    });
    if (reason === undefined) return; // Escape = cancelled, not "suppress with no reason"
    await addSuppression(this.repoRoot, {
      fingerprint: fp,
      file_path: filePath,
      line,
      title,
      reason: reason.trim(),
      suppressed_at: new Date().toISOString(),
    });
    this.panel.webview.postMessage({ command: 'suppressDone', fingerprint: fp });
  }

  private async handleCreateTestFile(affectedFile: string, filename: string, code: string): Promise<void> {
    if (!filename) return;
    const suggested = suggestTestFilePath(this.repoRoot, affectedFile, filename);
    const target = await vscode.window.showSaveDialog({ defaultUri: suggested, saveLabel: 'Create Test File' });
    if (!target) return;
    await vscode.workspace.fs.writeFile(target, Buffer.from(code, 'utf8'));
    const doc = await vscode.workspace.openTextDocument(target);
    await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn.One });
  }

  private async handleApplyFix(file: string, line0: number): Promise<void> {
    const fixes = this.report?.remediation?.code_fixes ?? [];
    const fix = fixes.find((f) => f.file_path === file && parseFixLine(f) === line0);
    const status = fix ? await applyCodeFix(this.repoRoot, fix) : 'error';
    this.panel.webview.postMessage({ command: 'fixResult', file, line0, status });
    if (status === 'stale') {
      vscode.window.showWarningMessage('GTO: file changed since analysis — fix not applied to avoid editing the wrong line.');
    } else if (status === 'error') {
      vscode.window.showErrorMessage("GTO: couldn't apply that fix.");
    }
  }

  /** Shared by "Post to PR" and "Approve PR" — neither gitDiff.ts's RepoRoot
   * nor the report itself carries a PR number (confirmed no such concept
   * exists locally), so both ask once per click. */
  private async promptForPrId(): Promise<string | undefined> {
    const prId = await vscode.window.showInputBox({
      title: 'Pull request number',
      prompt: 'Which PR is this for?',
      placeHolder: 'e.g. 42',
      ignoreFocusOut: true,
      validateInput: (v) => (/^\d+$/.test(v.trim()) ? undefined : 'Enter a numeric PR number'),
    });
    return prId?.trim();
  }

  private async handlePostToPr(): Promise<void> {
    if (!this.report || !this.opts?.backendUrl || !this.opts?.apiKey) {
      vscode.window.showErrorMessage('GTO: could not post to PR — no active backend connection for this report.');
      return;
    }
    const prId = await this.promptForPrId();
    if (!prId) {
      this.panel.webview.postMessage({ command: 'postToPrDone' });
      return;
    }
    const repoSlug = ownerRepoFromUrl(this.report.repo_url);
    try {
      const result = await postFindingsToPR(this.opts.backendUrl, this.opts.apiKey, this.report.request_id, {
        repoSlug,
        prId,
        provider: getGitProvider(),
      });
      vscode.window.showInformationMessage(
        result.ok
          ? `GTO: posted ${result.files_commented} file comment(s) + summary to PR #${prId}.`
          : "GTO: couldn't post any comments — check the backend's shared bot credential and PR number."
      );
    } catch (err) {
      const message = err instanceof ApiError ? err.message : describeError(err);
      vscode.window.showErrorMessage(`GTO: ${message}`);
    }
    this.panel.webview.postMessage({ command: 'postToPrDone' });
  }

  private async handleApprovePr(): Promise<void> {
    if (!this.report || !this.opts?.backendUrl || !this.opts?.apiKey) {
      vscode.window.showErrorMessage('GTO: could not approve — no active backend connection for this report.');
      return;
    }
    const token = this.opts.secrets ? await getBitbucketToken(this.opts.secrets) : undefined;
    if (!token) {
      vscode.window.showWarningMessage(
        'GTO: no personal token set — run "GTO: Set Personal Git Provider Token" first (the approval must show as you, not the shared bot).'
      );
      this.panel.webview.postMessage({ command: 'approvePrDone' });
      return;
    }
    const prId = await this.promptForPrId();
    if (!prId) {
      this.panel.webview.postMessage({ command: 'approvePrDone' });
      return;
    }
    const repoSlug = ownerRepoFromUrl(this.report.repo_url);
    try {
      const result = await approvePR(this.opts.backendUrl, this.opts.apiKey, this.report.request_id, {
        provider: getGitProvider(),
        token,
        repoSlug,
        prId,
      });
      vscode.window.showInformationMessage(
        result.status === 'approved'
          ? `GTO: PR #${prId} approved.`
          : `GTO: could not approve PR #${prId} — ${result.pr_action?.errors?.[0] ?? 'unknown error'}`
      );
    } catch (err) {
      const message = err instanceof ApiError ? err.message : describeError(err);
      vscode.window.showErrorMessage(`GTO: ${message}`);
    }
    this.panel.webview.postMessage({ command: 'approvePrDone' });
  }

  private async handleApplyAllFixes(): Promise<void> {
    const fixes = (this.report?.remediation?.code_fixes ?? []).filter((f) => f.confidence === 'high');
    let applied = 0;
    let skipped = 0;
    for (const fix of fixes) {
      const line0 = parseFixLine(fix);
      if (line0 === null) continue;
      const status = await applyCodeFix(this.repoRoot, fix);
      this.panel.webview.postMessage({ command: 'fixResult', file: fix.file_path, line0, status });
      if (status === 'applied') applied++;
      else skipped++;
    }
    vscode.window.showInformationMessage(
      skipped > 0 ? `GTO: applied ${applied}/${fixes.length} — ${skipped} stale, skipped.` : `GTO: applied ${applied} fix(es).`
    );
    this.panel.webview.postMessage({ command: 'applyAllDone' });
  }

  private async handleCopyMarkdown(): Promise<void> {
    if (!this.report) return;
    await vscode.env.clipboard.writeText(reportToMarkdown(this.report));
    this.panel.webview.postMessage({ command: 'copyMarkdownDone' });
    vscode.window.setStatusBarMessage('GTO: report copied as Markdown', 2500);
  }

  static showLoading(repoRoot: string): ResultsPanel {
    const p = ResultsPanel.getOrCreate(repoRoot);
    p.report = undefined;
    p.panel.webview.html = renderShell('<p class="dim">Submitting to GTO backend…</p>');
    return p;
  }

  static setStatus(text: string): void {
    if (!ResultsPanel.current) return;
    ResultsPanel.current.panel.webview.html = renderShell(`<p class="dim">${escapeHtml(text)}</p>`);
  }

  static showError(message: string, repoRoot: string): void {
    const p = ResultsPanel.getOrCreate(repoRoot);
    p.report = undefined;
    p.panel.webview.html = renderShell(`<p class="err">${escapeHtml(message)}</p>`);
  }

  static showReport(report: AnalysisReport, repoRoot: string, opts?: ReportViewOpts): void {
    const p = ResultsPanel.getOrCreate(repoRoot);
    p.report = report;
    p.opts = opts ?? { suppressed: [], newFingerprints: new Set() };
    p.panel.webview.html = renderReport(report, p.opts);
    void p.loadSimilarPrs();
  }

  /** Fire-and-forget — a nice-to-have context panel, never worth blocking or
   * erroring the main report render over. Silently does nothing on failure
   * or an empty result (fetchSimilarPRs already swallows non-OK responses). */
  private async loadSimilarPrs(): Promise<void> {
    if (!this.report || !this.opts?.backendUrl || !this.opts?.apiKey) return;
    const items = await fetchSimilarPRs(this.opts.backendUrl, this.opts.apiKey, this.report.request_id).catch(() => []);
    if (!items.length) return;
    this.panel.webview.postMessage({ command: 'similarPrsResult', items });
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

/** Where to offer creating a new test file: co-located with the affected
 * source file by default (works for pytest, JS/TS co-location, etc.), but
 * mirrored into src/test/... when the source lives under src/main/... — the
 * standard Maven/Gradle layout, the one common convention that co-location
 * would get wrong. The user can still redirect via the save dialog either way. */
function suggestTestFilePath(repoRoot: string, affectedFile: string, filename: string): vscode.Uri {
  const idx = affectedFile.lastIndexOf('/');
  let dir = idx === -1 ? '' : affectedFile.slice(0, idx);
  if (dir.includes('/src/main/') || dir.startsWith('src/main/')) {
    dir = dir.replace('src/main/', 'src/test/');
  }
  const relPath = dir ? `${dir}/${filename}` : filename;
  return vscode.Uri.joinPath(vscode.Uri.file(repoRoot), relPath);
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

/** Cuts long text at the last word boundary before `max` chars, rather than
 * mid-word — some upstream findings (esp. LLM output cut off by a max-tokens
 * budget) can otherwise arrive already truncated mid-sentence; this at least
 * keeps what's displayed from *looking* broken. */
function truncateAtWord(s: string, max: number): string {
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const lastSpace = cut.lastIndexOf(' ');
  const trimmed = lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut;
  return trimmed.trimEnd() + '…';
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

/** Makes .gto.yaml's effect visible — without this, path-scoped rules only
 * ever show up as "fewer findings than expected," with nothing telling the
 * user a config file was even read. */
function pathReviewBannerHtml(r: AnalysisReport): string {
  const s = r.path_review_summary;
  if (!s || (!s.agents_excluded?.length && !s.steering_applied)) return '';
  const parts: string[] = [];
  if (s.agents_excluded?.length) {
    parts.push(`skipped ${s.agents_excluded.length} agent(s) (${s.agents_excluded.map(escapeHtml).join(', ')})`);
  }
  if (s.steering_applied) parts.push('applied path-scoped priorities');
  return `<p class="path-review-banner">📄 <code>.gto.yaml</code> — ${parts.join('; ')}</p>`;
}

function findFixForIssue(issue: CorrelatedIssue, fixes: CodeFix[]): CodeFix | undefined {
  if (!issue.file_path) return undefined;
  const sameFile = fixes.filter((f) => f.file_path === issue.file_path);
  if (!sameFile.length) return undefined;
  const exact = sameFile.find((f) => {
    const line = parseFixLine(f);
    return line !== null && line + 1 === issue.line;
  });
  return exact ?? sameFile[0];
}

function renderReport(r: AnalysisReport, opts: ReportViewOpts): string {
  const gate = GATE_META[r.gate_decision] ?? { label: r.gate_decision, color: 'var(--vscode-foreground)' };
  const score = r.risk?.risk_score ?? 0;
  const issues = r.top_issues ?? [];
  const fixes = r.remediation?.code_fixes ?? [];

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

  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
    ${BASE_STYLE}
    .top-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
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
  </style></head>
  <body>
    <div class="top-row">
      <div class="gate">${gate.label}</div>
      <div>
        ${
          (r.remediation?.code_fixes ?? []).filter((f) => f.confidence === 'high').length > 0
            ? `<button class="gto-btn" id="applyAll" style="margin-right:6px;">Apply all (${
                (r.remediation?.code_fixes ?? []).filter((f) => f.confidence === 'high').length
              })</button>`
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
    ${diagramsHtml(r.remediation?.diagrams ?? [])}
    ${qaScenariosHtml(r.qa_scenarios?.scenarios ?? [])}
    ${suppressedHtml}
    ${filesHtml}
    ${r.errors?.length ? `<div class="errs">${r.errors.map(escapeHtml).join('<br>')}</div>` : ''}
    <script>
      const vscode = acquireVsCodeApi();

      document.querySelectorAll('.issue').forEach(el => {
        el.addEventListener('click', (e) => {
          if (e.target.closest('.fix')) return; // let the fix <details> toggle without navigating
          vscode.postMessage({ command: 'openFinding', file: el.dataset.file, line: el.dataset.line });
        });
      });

      document.querySelectorAll('.file-item').forEach(el => {
        el.addEventListener('click', () => {
          vscode.postMessage({ command: 'openFinding', file: el.dataset.file, line: 1 });
        });
      });

      document.querySelectorAll('.apply-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          btn.disabled = true;
          btn.textContent = 'Applying…';
          vscode.postMessage({ command: 'applyFix', file: btn.dataset.file, line0: Number(btn.dataset.line0) });
        });
      });

      const copyBtn = document.getElementById('copyMd');
      if (copyBtn) copyBtn.addEventListener('click', () => vscode.postMessage({ command: 'copyMarkdown' }));

      const applyAllBtn = document.getElementById('applyAll');
      if (applyAllBtn) applyAllBtn.addEventListener('click', () => {
        applyAllBtn.disabled = true;
        applyAllBtn.textContent = 'Applying…';
        vscode.postMessage({ command: 'applyAllFixes' });
      });

      const postToPrBtn = document.getElementById('postToPr');
      if (postToPrBtn) postToPrBtn.addEventListener('click', () => {
        postToPrBtn.disabled = true;
        postToPrBtn.textContent = 'Posting…';
        vscode.postMessage({ command: 'postToPr' });
      });

      const approvePrBtn = document.getElementById('approvePr');
      if (approvePrBtn) approvePrBtn.addEventListener('click', () => {
        approvePrBtn.disabled = true;
        approvePrBtn.textContent = 'Approving…';
        vscode.postMessage({ command: 'approvePr' });
      });

      document.querySelectorAll('.copy-skeleton-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          vscode.postMessage({ command: 'copyText', text: btn.dataset.code, id: btn.dataset.id });
        });
      });

      document.querySelectorAll('.create-test-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          vscode.postMessage({
            command: 'createTestFile',
            affectedFile: btn.dataset.affected,
            filename: btn.dataset.filename,
            code: btn.dataset.code,
          });
        });
      });

      document.querySelectorAll('.suppress-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          vscode.postMessage({
            command: 'suppressFinding',
            fingerprint: btn.dataset.fingerprint,
            file: btn.dataset.file,
            line: Number(btn.dataset.line),
            title: btn.dataset.title,
          });
        });
      });

      document.querySelectorAll('.unsuppress-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          vscode.postMessage({ command: 'unsuppressFinding', fingerprint: btn.dataset.fingerprint });
        });
      });

      document.querySelectorAll('.fp-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          btn.disabled = true;
          btn.textContent = 'Recording…';
          vscode.postMessage({
            command: 'markFalsePositive',
            fingerprint: btn.dataset.fingerprint,
            agent: btn.dataset.agent,
            category: btn.dataset.category,
            file: btn.dataset.file,
          });
        });
      });

      document.querySelectorAll('.explain-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          btn.disabled = true;
          btn.textContent = 'Asking…';
          vscode.postMessage({
            command: 'explainIssue',
            fingerprint: btn.dataset.fingerprint,
            agent: btn.dataset.agent,
            category: btn.dataset.category,
            file: btn.dataset.file,
            title: btn.dataset.title,
          });
        });
      });

      window.addEventListener('message', (event) => {
        const msg = event.data;
        if (msg?.command === 'fixResult') {
          const btn = document.querySelector(
            '.apply-btn[data-file="' + CSS.escape(msg.file) + '"][data-line0="' + msg.line0 + '"]'
          );
          if (!btn) return;
          btn.disabled = msg.status === 'applied';
          btn.dataset.status = msg.status;
          btn.textContent = msg.status === 'applied' ? 'Applied ✓' : msg.status === 'stale' ? 'File changed — skipped' : 'Failed to apply';
        } else if (msg?.command === 'copyMarkdownDone' && copyBtn) {
          const prev = copyBtn.textContent;
          copyBtn.textContent = 'Copied ✓';
          setTimeout(() => { copyBtn.textContent = prev; }, 2000);
        } else if (msg?.command === 'copyTextDone') {
          const btn = document.querySelector('.copy-skeleton-btn[data-id="' + CSS.escape(msg.id) + '"]');
          if (!btn) return;
          const prev = btn.textContent;
          btn.textContent = 'Copied ✓';
          setTimeout(() => { btn.textContent = prev; }, 2000);
        } else if (msg?.command === 'suppressDone') {
          const card = document.querySelector('.issue[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
          if (card) card.remove();
        } else if (msg?.command === 'unsuppressDone') {
          const row = document.querySelector('.suppressed-row[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
          if (row) row.remove();
        } else if (msg?.command === 'fpDone') {
          const btn = document.querySelector('.fp-btn[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
          if (btn) { btn.textContent = 'Recorded ✓'; }
        } else if (msg?.command === 'explainDone') {
          const btn = document.querySelector('.explain-btn[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
          const slot = document.querySelector('.explain-slot[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
          if (btn) { btn.disabled = false; btn.textContent = '❓ Explain'; }
          if (slot) {
            const esc = (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            if (msg.error) {
              slot.innerHTML = '<p class="dim" style="margin:6px 0;">Could not fetch an explanation — ' + esc(msg.error) + '</p>';
            } else if (msg.text) {
              slot.innerHTML = '<details class="fix" open><summary>❓ Explanation</summary>' +
                '<p class="dim" style="margin:6px 0;white-space:pre-wrap;">' + esc(msg.text) + '</p></details>';
            }
          }
        } else if (msg?.command === 'applyAllDone') {
          if (applyAllBtn) { applyAllBtn.disabled = false; applyAllBtn.textContent = 'Apply all'; }
        } else if (msg?.command === 'postToPrDone') {
          if (postToPrBtn) { postToPrBtn.disabled = false; postToPrBtn.textContent = '📮 Post to PR'; }
        } else if (msg?.command === 'approvePrDone') {
          if (approvePrBtn) { approvePrBtn.disabled = false; approvePrBtn.textContent = '✅ Approve PR'; }
        } else if (msg?.command === 'similarPrsResult') {
          const el = document.getElementById('similarPrs');
          if (!el || !Array.isArray(msg.items) || !msg.items.length) return;
          const esc = (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
          el.innerHTML = '<details open><summary>Similar past PRs (' + msg.items.length + ')</summary>' +
            msg.items.map((it) => {
              const pct = Math.round((it.similarity || 0) * 100);
              const label = it.pr_title || it.source_ref || it.repo;
              const branch = it.pr_title && it.source_ref ? ' (' + esc(it.source_ref) + ')' : '';
              const shared = (it.shared_files && it.shared_files.length)
                ? ' · ' + it.shared_files.length + ' shared file' + (it.shared_files.length > 1 ? 's' : '') : '';
              return '<div class="similar-row"><span class="sim-pct">' + pct + '%</span> ' +
                '<span>' + esc(label) + branch + '</span> ' +
                '<span class="dim">' + esc(it.gate) + ' · ' + esc(it.elapsed) + shared + '</span></div>';
            }).join('') + '</details>';
        }
      });
    </script>
  </body></html>`;
}

function renderDiffLines(diff: string): string {
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

function fixHtml(fix: CodeFix, file: string): string {
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
function fixSuggestionsHtml(suggestions: string[]): string {
  if (!suggestions.length) return '';
  return `<h2 style="margin-top:16px;font-size:14px;">Suggested fixes (${suggestions.length})</h2>
    <ul class="suggestions">${suggestions.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ul>`;
}

function scenarioHtml(s: QAScenario): string {
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

function qaScenariosHtml(scenarios: QAScenario[]): string {
  if (!scenarios.length) return '';
  return `<h2 style="margin-top:16px;font-size:14px;">Unit test coverage gaps (${scenarios.length})</h2>
    ${scenarios.map(scenarioHtml).join('')}`;
}

/** Raw Mermaid source, not rendered — VS Code's webview has no Mermaid
 * runtime bundled, and adding one is a bigger step than this earns until
 * there's evidence people actually want inline rendering (Thorough preset +
 * medium/high risk + real reference_impact data all have to line up for this
 * to even appear, so it's rare). Paste into a Mermaid live editor to view. */
function diagramsHtml(diagrams: MermaidDiagram[]): string {
  if (!diagrams.length) return '';
  return `<h2 style="margin-top:16px;font-size:14px;">Sequence diagrams (${diagrams.length})</h2>
    ${diagrams
      .map(
        (d, i) => `<details class="fix">
          <summary>📈 ${escapeHtml(d.diagram_type || 'sequenceDiagram')}
            <span class="ai-tag">AI-generated — not verified against the real call graph</span>
          </summary>
          ${d.note ? `<p class="dim" style="margin:6px 0;">${escapeHtml(d.note)}</p>` : ''}
          <pre class="diff">${escapeHtml(d.mermaid_source)}</pre>
          <button class="gto-btn copy-skeleton-btn" data-id="diagram-${i}" data-code="${escapeHtml(d.mermaid_source)}">Copy code</button>
        </details>`
      )
      .join('')}`;
}

function issueHtml(it: CorrelatedIssue, fix: CodeFix | undefined, isNew: boolean): string {
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

function suppressedListHtml(suppressed: SuppressedEntry[]): string {
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

function reportToMarkdown(r: AnalysisReport): string {
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
