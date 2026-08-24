// vscode-extension/src/diagnostics.ts
// -----------------------------------------------------------------------------
// Mirrors report.top_issues into VS Code's native Diagnostics API — squiggly
// underlines in the editor + entries in the Problems panel, in addition to
// (not instead of) the results webview panel, which still carries context
// (rationale, agent names, confidence) a diagnostic can't show.

import * as vscode from 'vscode';
import { AnalysisReport } from './apiClient';

const SEVERITY_MAP: Record<string, vscode.DiagnosticSeverity> = {
  critical: vscode.DiagnosticSeverity.Error,
  high: vscode.DiagnosticSeverity.Error,
  medium: vscode.DiagnosticSeverity.Warning,
  low: vscode.DiagnosticSeverity.Information,
};

let collection: vscode.DiagnosticCollection | undefined;

export function initDiagnostics(context: vscode.ExtensionContext): void {
  collection = vscode.languages.createDiagnosticCollection('gto');
  context.subscriptions.push(collection);
}

export function updateDiagnostics(report: AnalysisReport, repoRoot: string): void {
  if (!collection) return;
  collection.clear();

  const byUri = new Map<string, vscode.Diagnostic[]>();
  for (const issue of report.top_issues ?? []) {
    if (!issue.file_path) continue;
    const line = Math.max(0, (issue.line || 1) - 1);
    // No need to read the file to know the real line length — VS Code clips
    // the underline to the actual line end visually.
    const range = new vscode.Range(line, 0, line, 1000);
    const severity = SEVERITY_MAP[(issue.severity || '').toLowerCase()] ?? vscode.DiagnosticSeverity.Information;

    const diag = new vscode.Diagnostic(range, issue.title, severity);
    diag.source = 'GTO';
    if (issue.categories?.length) diag.code = issue.categories[0];

    const key = vscode.Uri.joinPath(vscode.Uri.file(repoRoot), issue.file_path).toString();
    if (!byUri.has(key)) byUri.set(key, []);
    byUri.get(key)!.push(diag);
  }

  for (const [uriStr, diags] of byUri) {
    collection.set(vscode.Uri.parse(uriStr), diags);
  }
}

export function clearDiagnostics(): void {
  collection?.clear();
}
