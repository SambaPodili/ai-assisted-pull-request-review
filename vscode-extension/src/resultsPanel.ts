// vscode-extension/src/resultsPanel.ts
// -----------------------------------------------------------------------------
// Single-instance webview panel that renders an AnalysisReport. Styled with
// VS Code's own CSS custom properties (--vscode-*) so it matches whatever
// theme the user has, light or dark, with zero extra theming code.

import * as vscode from 'vscode';
import { AnalysisReport, submitFindingFeedback, fetchSimilarPRs, explainFinding, postFindingsToPR, approvePR, ApiError, describeError } from './apiClient';
import { parseFixLine, applyCodeFix } from './codeActions';
import { SuppressedEntry, addSuppression, removeSuppression } from './reportState';
import { ownerRepoFromUrl } from './gitDiff';
import { getGitProvider, getBitbucketToken } from './settings';
import {
  resolveGate, renderReportStyles, renderReportBody, reportToMarkdown, escapeHtml, BASE_STYLE,
} from '../../shared/report-view/src';

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

export class ResultsPanel {
  private static current: ResultsPanel | undefined;
  // Set once from activate() — needed to resolve media/mermaid.min.js into a
  // webview-safe URI. Diagrams render as raw copyable text (pre-0.15.0
  // behaviour) if this is never set, rather than throwing.
  private static extensionUri: vscode.Uri | undefined;
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

  static init(extensionUri: vscode.Uri): void {
    ResultsPanel.extensionUri = extensionUri;
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
    const mermaidUri = ResultsPanel.extensionUri
      ? p.panel.webview.asWebviewUri(vscode.Uri.joinPath(ResultsPanel.extensionUri, 'media', 'mermaid.min.js')).toString()
      : undefined;
    p.panel.webview.html = renderReport(report, p.opts, mermaidUri, p.panel.webview.cspSource);
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
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: ResultsPanel.extensionUri
          ? [vscode.Uri.joinPath(ResultsPanel.extensionUri, 'media')]
          : undefined,
      }
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

function renderShell(bodyHtml: string): string {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>${BASE_STYLE}</style></head><body>${bodyHtml}</body></html>`;
}

function renderReport(r: AnalysisReport, opts: ReportViewOpts, mermaidUri?: string, cspSource?: string): string {
  const gate = resolveGate(r.gate_decision);
  const csp = cspSource
    ? `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${cspSource} 'unsafe-inline'; script-src ${cspSource} 'unsafe-inline'; img-src ${cspSource} https: data:; font-src ${cspSource};">`
    : '';
  // renderReportBody/renderReportStyles come from ../../shared/report-view —
  // the same rendering the IntelliJ plugin's JCEF panel uses; see that
  // package's README for the shared/host-specific split. Everything past
  // this point (the <script> block below) is VS-Code-specific interactivity
  // wiring the shared package deliberately does not own.
  const bodyHtml = renderReportBody(r, { suppressed: opts.suppressed, newFingerprints: opts.newFingerprints }, mermaidUri);
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">${csp}<style>
    ${renderReportStyles(gate.color)}
  </style></head>
  <body>
    ${bodyHtml}
    <script>
      const vscode = acquireVsCodeApi();

      // Render any .mermaid diagram blocks inline, matching the current VS
      // Code theme. VS Code stamps a vscode-light/vscode-dark/vscode-high-
      // contrast(-light) class on <body> itself — the same signal the rest of
      // this page already reads indirectly via var(--vscode-*) tokens — so no
      // separate theme-detection message round-trip to the extension host is
      // needed. Never throws: a diagram that fails to parse just stays as the
      // raw copyable source in the <details> below it (see diagramsHtml).
      if (typeof mermaid !== 'undefined') {
        try {
          const isDark = document.body.classList.contains('vscode-dark')
            || document.body.classList.contains('vscode-high-contrast');
          mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: isDark ? 'dark' : 'default' });
          mermaid.run({ querySelector: '.mermaid' }).catch(() => { /* leave raw source below visible */ });
        } catch { /* leave raw source below visible */ }
      }

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

