// vscode-extension/src/codeLenses.ts
// -----------------------------------------------------------------------------
// Mirrors report.top_issues into inline CodeLens annotations above each flagged
// line — same self-clearing module-level cache pattern as codeActions.ts, but
// CodeLens (unlike CodeActions, which VS Code invokes on demand) needs to be
// told to re-query after each refresh, hence the onDidChangeCodeLenses emitter.

import * as vscode from 'vscode';
import { AnalysisReport, CorrelatedIssue } from './apiClient';

interface CachedLensGroup {
  uri: vscode.Uri;
  line: number; // 0-based
  issues: CorrelatedIssue[];
}

let cachedLenses: CachedLensGroup[] = [];

const onDidChangeCodeLensesEmitter = new vscode.EventEmitter<void>();
export const codeLensChangeEvent = onDidChangeCodeLensesEmitter.event;

/** Rebuilds the CodeLens cache from report.top_issues. Issues with `line <= 0`
 * (a legitimate value for an LLM finding with no resolvable line_range — see
 * governance/correlation.py) are skipped: a CodeLens pinned to line 1 of an
 * unrelated file is more misleading than a diagnostic squiggle would be.
 * Multiple issues on the same line are grouped into one CodeLens rather than
 * stacking several — CodeLens is more visually prominent than a Quick Fix
 * menu, so grouping keeps the editor readable. */
export function updateCodeLenses(report: AnalysisReport, repoRoot: string): void {
  const byKey = new Map<string, CachedLensGroup>();

  for (const issue of report.top_issues ?? []) {
    if (!issue.file_path || !issue.line || issue.line <= 0) continue;
    const uri = vscode.Uri.joinPath(vscode.Uri.file(repoRoot), issue.file_path);
    const line = issue.line - 1;
    const key = `${uri.toString()}:${line}`;
    if (!byKey.has(key)) byKey.set(key, { uri, line, issues: [] });
    byKey.get(key)!.issues.push(issue);
  }

  cachedLenses = Array.from(byKey.values());
  onDidChangeCodeLensesEmitter.fire();
}

export function clearCodeLenses(): void {
  cachedLenses = [];
  onDidChangeCodeLensesEmitter.fire();
}

function labelFor(group: CachedLensGroup): string {
  const n = group.issues.length;
  return `⚠ ${n} GTO issue${n > 1 ? 's' : ''}`;
}

class GtoCodeLensProvider implements vscode.CodeLensProvider {
  onDidChangeCodeLenses = codeLensChangeEvent;

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const lenses: vscode.CodeLens[] = [];
    for (const group of cachedLenses) {
      if (group.uri.toString() !== document.uri.toString()) continue;
      if (group.line >= document.lineCount) continue;
      const range = document.lineAt(group.line).range;
      lenses.push(
        new vscode.CodeLens(range, {
          title: labelFor(group),
          command: 'gto.showLineIssues',
          arguments: [group.issues],
        })
      );
    }
    return lenses;
  }
}

export function registerCodeLenses(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider({ scheme: 'file' }, new GtoCodeLensProvider())
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('gto.showLineIssues', async (issues: CorrelatedIssue[]) => {
      if (!issues || issues.length === 0) return;
      let picked: CorrelatedIssue;
      if (issues.length === 1) {
        picked = issues[0];
      } else {
        const pick = await vscode.window.showQuickPick(
          issues.map((issue) => ({
            label: issue.title,
            description: `${issue.severity} · ${issue.confidence}`,
            issue,
          })),
          { title: 'GTO issues on this line' }
        );
        if (!pick) return;
        picked = pick.issue;
      }
      const detail = picked.descriptions?.length ? ` — ${picked.descriptions.join(' ')}` : '';
      vscode.window.showInformationMessage(`${picked.title}${detail}`);
    })
  );
}
