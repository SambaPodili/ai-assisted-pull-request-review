// vscode-extension/src/codeActions.ts
// -----------------------------------------------------------------------------
// Turns report.remediation.code_fixes into VS Code Quick Fix (💡) actions.
// Only populated when the `remediation` agent runs (Thorough preset today —
// Fast/Standard don't include it, matching the same preset definitions used
// by the web app). Deterministic, not LLM-generated (agents/fix_generator.py),
// so it's safe to apply automatically — but only when the target line still
// matches exactly what was analyzed; if the file changed since the analysis
// ran, the fix is silently withheld rather than editing the wrong line.

import * as vscode from 'vscode';
import { AnalysisReport, CodeFix } from './apiClient';

interface CachedFix {
  uri: vscode.Uri;
  line: number; // 0-based
  fix: CodeFix;
}

const LINE_RE = /@@ line (\d+) @@/;

let cachedFixes: CachedFix[] = [];

export function updateCodeFixes(report: AnalysisReport, repoRoot: string): void {
  cachedFixes = [];
  for (const fix of report.remediation?.code_fixes ?? []) {
    if (!fix.file_path) continue;
    const m = LINE_RE.exec(fix.diff);
    if (!m) continue;
    const uri = vscode.Uri.joinPath(vscode.Uri.file(repoRoot), fix.file_path);
    cachedFixes.push({ uri, line: parseInt(m[1], 10) - 1, fix });
  }
}

export function clearCodeFixes(): void {
  cachedFixes = [];
}

class GtoCodeActionProvider implements vscode.CodeActionProvider {
  provideCodeActions(document: vscode.TextDocument, range: vscode.Range): vscode.CodeAction[] {
    const actions: vscode.CodeAction[] = [];
    for (const cached of cachedFixes) {
      if (cached.uri.toString() !== document.uri.toString()) continue;
      if (cached.line < range.start.line || cached.line > range.end.line) continue;
      if (cached.line >= document.lineCount) continue;

      const lineRange = document.lineAt(cached.line).range;
      if (document.getText(lineRange) !== cached.fix.before) continue; // stale — file changed since analysis

      // Explanation isn't shown here — the CodeAction API only surfaces
      // `title` in the lightbulb menu. The full CodeFix (incl. explanation)
      // is still visible in the results panel for anyone who wants the "why".
      const action = new vscode.CodeAction(`GTO fix: ${cached.fix.title}`, vscode.CodeActionKind.QuickFix);
      action.edit = new vscode.WorkspaceEdit();
      action.edit.replace(document.uri, lineRange, cached.fix.after);
      action.isPreferred = cached.fix.confidence === 'high';
      actions.push(action);
    }
    return actions;
  }
}

export function registerCodeActions(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.languages.registerCodeActionsProvider(
      { scheme: 'file' },
      new GtoCodeActionProvider(),
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }
    )
  );
}
