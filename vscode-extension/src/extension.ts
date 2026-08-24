// vscode-extension/src/extension.ts
// -----------------------------------------------------------------------------
// Commands:
//   GTO: Analyze Changes    — uncommitted diff (staged+unstaged vs HEAD).
//   GTO: Analyze Branch...  — quick-pick a base branch, three-dot diff against it.
//   GTO: Set API Key        — stores the key in SecretStorage.
//   GTO: Show Last Result   — reopens the most recent report from this session.
// Both analyze commands: quick-pick the agent preset (defaults to the
// configured setting — press Enter to keep it, zero added friction), then an
// optional priorities prompt with real-time guardrail validation.
// Auto-analyze-on-save (gto.autoAnalyzeOnSave, default OFF) reruns "Analyze
// Changes" a moment after you save, fully non-interactive.

import * as vscode from 'vscode';
import { resolveRepoRoot, getUncommittedDiff, getBranchDiff, listOtherBranches, GitError, RepoRoot } from './gitDiff';
import {
  getApiKey,
  promptForApiKey,
  getBackendUrl,
  getSelectedAgents,
  getAgentPreset,
  getAutoAnalyzeOnSave,
  AgentPreset,
  AGENT_PRESET_META,
} from './settings';
import { runAnalysisToCompletion, ApiError, AnalysisReport, StatusResponse } from './apiClient';
import { ResultsPanel } from './resultsPanel';
import { initDiagnostics, updateDiagnostics } from './diagnostics';
import { registerCodeActions, updateCodeFixes } from './codeActions';
import { scanUserInstructions } from './promptGuard';

let lastReport: { report: AnalysisReport; repoRoot: string } | undefined;
let statusBarItem: vscode.StatusBarItem;
let lastPriorities = ''; // in-memory only, remembered for convenience within the session — never persisted
let analysisInFlight = false;
let saveDebounce: ReturnType<typeof setTimeout> | undefined;
let warnedNoApiKeyForAutoSave = false;

// Identifies "nothing changed since the last run" so re-running without any
// edits redisplays the cached result instead of re-hitting the backend.
let lastRunKey: string | undefined;

const SAVE_DEBOUNCE_MS = 1500;

export function activate(context: vscode.ExtensionContext): void {
  initDiagnostics(context);
  registerCodeActions(context);

  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = 'gto.analyzeChanges';
  resetStatusBar();
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  context.subscriptions.push(
    vscode.commands.registerCommand('gto.analyzeChanges', () => analyzeUncommitted(context, true)),
    vscode.commands.registerCommand('gto.analyzeBranch', () => analyzeBranch(context)),
    vscode.commands.registerCommand('gto.setApiKey', () => setApiKeyCommand(context)),
    vscode.commands.registerCommand('gto.showLastResult', showLastResult),
    vscode.workspace.onDidSaveTextDocument(() => onDidSave(context))
  );
}

export function deactivate(): void {
  /* nothing to clean up — VS Code disposes subscriptions automatically */
}

// ── Commands ─────────────────────────────────────────────────────────────────

async function setApiKeyCommand(context: vscode.ExtensionContext): Promise<void> {
  const key = await promptForApiKey(context.secrets);
  if (key) vscode.window.showInformationMessage('GTO API key saved.');
}

function showLastResult(): void {
  if (!lastReport) {
    vscode.window.showInformationMessage('No GTO analysis has run yet in this session.');
    return;
  }
  ResultsPanel.showReport(lastReport.report, lastReport.repoRoot);
}

async function analyzeUncommitted(context: vscode.ExtensionContext, interactive: boolean): Promise<void> {
  let repo: RepoRoot;
  let diffText: string;
  try {
    repo = await resolveRepoRoot(interactive);
    diffText = await getUncommittedDiff(repo.cwd);
  } catch (e) {
    if (interactive) vscode.window.showErrorMessage(e instanceof GitError ? e.message : String(e));
    return;
  }

  if (!diffText.trim()) {
    if (interactive) {
      vscode.window.showInformationMessage(
        "No uncommitted changes to analyze. New files must be `git add`ed before GTO can see them — `git diff` only shows changes to files git already tracks."
      );
    }
    return;
  }

  await runAnalysis(context, { repo, diffText, sourceRef: repo.branch, interactive });
}

async function analyzeBranch(context: vscode.ExtensionContext): Promise<void> {
  let repo: RepoRoot;
  try {
    repo = await resolveRepoRoot(true);
  } catch (e) {
    vscode.window.showErrorMessage(e instanceof GitError ? e.message : String(e));
    return;
  }

  let branches: string[];
  try {
    branches = await listOtherBranches(repo.cwd, repo.branch);
  } catch (e) {
    vscode.window.showErrorMessage(e instanceof GitError ? e.message : String(e));
    return;
  }
  if (!branches.length) {
    vscode.window.showInformationMessage('No other local branches to compare against.');
    return;
  }

  const base = await vscode.window.showQuickPick(branches, {
    title: `Compare "${repo.branch}" against which base branch?`,
    placeHolder: 'Shows everything your branch adds since it diverged from the base',
  });
  if (!base) return; // cancelled

  let diffText: string;
  try {
    diffText = await getBranchDiff(repo.cwd, base);
  } catch (e) {
    vscode.window.showErrorMessage(e instanceof GitError ? e.message : String(e));
    return;
  }

  if (!diffText.trim()) {
    vscode.window.showInformationMessage(`No differences between "${repo.branch}" and "${base}".`);
    return;
  }

  await runAnalysis(context, { repo, diffText, sourceRef: repo.branch, targetRef: base, interactive: true });
}

async function onDidSave(context: vscode.ExtensionContext): Promise<void> {
  if (!getAutoAnalyzeOnSave()) return;
  if (saveDebounce) clearTimeout(saveDebounce);
  saveDebounce = setTimeout(() => {
    saveDebounce = undefined;
    void autoAnalyze(context);
  }, SAVE_DEBOUNCE_MS);
}

async function autoAnalyze(context: vscode.ExtensionContext): Promise<void> {
  if (analysisInFlight) return; // a manual or previous auto run is already going — don't overlap

  const apiKey = await getApiKey(context.secrets);
  if (!apiKey) {
    if (!warnedNoApiKeyForAutoSave) {
      warnedNoApiKeyForAutoSave = true;
      vscode.window.showWarningMessage(
        'GTO: auto-analyze-on-save is on but no API key is set. Run "GTO: Set API Key" to enable it.'
      );
    }
    return;
  }
  await analyzeUncommitted(context, false);
}

// ── Shared run flow ──────────────────────────────────────────────────────────

interface RunParams {
  repo: RepoRoot;
  diffText: string;
  sourceRef: string;
  targetRef?: string;
  interactive: boolean; // false = auto-save trigger: no pickers/prompts, quieter notifications
}

async function runAnalysis(context: vscode.ExtensionContext, params: RunParams): Promise<void> {
  const { repo, diffText, sourceRef, targetRef, interactive } = params;

  let apiKey = await getApiKey(context.secrets);
  if (!apiKey) {
    if (!interactive) return; // never pop a blocking prompt from an automatic trigger
    apiKey = await promptForApiKey(context.secrets);
    if (!apiKey) return;
  }

  // Non-interactive (auto-save) runs always use the configured default preset
  // and no priorities text — asking would defeat the point of "automatic".
  let preset: AgentPreset = getAgentPreset();
  let userInstructions = '';
  if (interactive) {
    preset = await promptForPreset(preset);
    userInstructions = await promptForPriorities();
    lastPriorities = userInstructions;
  }
  const selectedAgents = getSelectedAgents(preset);

  const runKey = JSON.stringify({ diffText, sourceRef, targetRef, preset, userInstructions });
  if (interactive && runKey === lastRunKey && lastReport) {
    vscode.window.showInformationMessage('GTO: nothing changed since the last run — showing the previous result.');
    ResultsPanel.showReport(lastReport.report, lastReport.repoRoot);
    return;
  }

  const backendUrl = getBackendUrl();

  analysisInFlight = true;
  ResultsPanel.showLoading(repo.cwd);
  statusBarItem.text = '$(sync~spin) GTO Review…';
  statusBarItem.backgroundColor = undefined;

  const token = { cancelled: false };

  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'GTO Pull Request Review', cancellable: true },
    async (progress, cancellation) => {
      cancellation.onCancellationRequested(() => {
        token.cancelled = true;
      });

      const onStatus = (s: StatusResponse) => {
        const text = statusText(s);
        progress.report({ message: text });
        ResultsPanel.setStatus(text);
      };

      try {
        const report = await runAnalysisToCompletion(
          {
            backendUrl,
            apiKey: apiKey!,
            repoUrl: repo.repoUrl,
            sourceRef,
            targetRef,
            diffText,
            selectedAgents,
            userInstructions,
          },
          token,
          onStatus
        );

        if (token.cancelled) {
          ResultsPanel.showError('Cancelled.', repo.cwd);
          resetStatusBar();
          return;
        }
        if (report === null) {
          if (interactive) vscode.window.showInformationMessage('GTO: no analyzable diff (metadata-only change?).');
          resetStatusBar();
          return;
        }

        lastReport = { report, repoRoot: repo.cwd };
        lastRunKey = runKey;
        ResultsPanel.showReport(report, repo.cwd);
        updateDiagnostics(report, repo.cwd);
        updateCodeFixes(report, repo.cwd);
        updateStatusBar(report);

        const msg = `GTO: ${report.gate_decision} — risk ${report.risk?.risk_score ?? 0}/100, ${report.top_issues?.length ?? 0} issue(s)`;
        if (report.gate_decision === 'BLOCK') vscode.window.showErrorMessage(msg);
        else if (report.gate_decision === 'HOLD') vscode.window.showWarningMessage(msg);
        else if (interactive) vscode.window.showInformationMessage(msg);
      } catch (e) {
        const message = e instanceof ApiError || e instanceof Error ? e.message : String(e);
        vscode.window.showErrorMessage(`GTO analysis failed: ${message}`);
        ResultsPanel.showError(message, repo.cwd);
        resetStatusBar();
      } finally {
        analysisInFlight = false;
      }
    }
  );
}

async function promptForPreset(current: AgentPreset): Promise<AgentPreset> {
  const items = AGENT_PRESET_META.map((m) => ({
    label: m.label,
    description: m.preset === current ? '(configured default)' : undefined,
    detail: m.description,
    preset: m.preset,
  }));
  // showQuickPick's convenience overload has no `activeItem` option — put the
  // configured default first instead, since QuickPick highlights the first
  // item by default, giving the same "press Enter to keep it" behavior.
  items.sort((a, b) => (a.preset === current ? -1 : b.preset === current ? 1 : 0));

  const picked = await vscode.window.showQuickPick(items, {
    title: 'GTO — Analysis depth',
    placeHolder: 'Press Enter to keep the configured default',
  });
  return picked?.preset ?? current; // Escape keeps the default rather than aborting
}

/** Returns the trimmed priorities text, or '' if the user pressed Escape or
 * left it blank — either way that means "run without priorities", not
 * "abort the analysis": an accidental Escape shouldn't cancel a command the
 * user just explicitly invoked. */
async function promptForPriorities(): Promise<string> {
  const value = await vscode.window.showInputBox({
    title: 'GTO — Analysis Priorities (optional)',
    prompt: 'What should agents emphasize? e.g. "focus on security in the payment module". Leave empty and press Enter to skip.',
    value: lastPriorities,
    ignoreFocusOut: true,
    validateInput: (v): vscode.InputBoxValidationMessage | undefined => {
      const { blocked, matches } = scanUserInstructions(v);
      if (!blocked) return undefined;
      return {
        message: `This looks like it's trying to override review rules ("${matches[0].phrase}") rather than prioritize — rephrase it as guidance about what to focus on.`,
        severity: vscode.InputBoxValidationSeverity.Error,
      };
    },
  });
  return (value ?? '').trim();
}

function statusText(s: StatusResponse): string {
  if (s.status === 'queued') {
    return typeof s.queue_position === 'number' ? `Queued (${s.queue_position}/${s.queue_total ?? '?'})…` : 'Queued…';
  }
  if (s.status === 'running') return 'Running agents…';
  return s.status;
}

// ── Status bar ───────────────────────────────────────────────────────────────

const GATE_ICON: Record<string, string> = { APPROVE: '$(check)', HOLD: '$(warning)', BLOCK: '$(error)' };

function resetStatusBar(): void {
  statusBarItem.text = '$(play) GTO Review';
  statusBarItem.tooltip = 'Analyze uncommitted changes with GTO Pull Request Review Framework';
  statusBarItem.backgroundColor = undefined;
}

function updateStatusBar(report: AnalysisReport): void {
  const gate = report.gate_decision;
  const icon = GATE_ICON[gate] ?? '$(question)';
  statusBarItem.text = `${icon} GTO: ${gate}`;
  statusBarItem.tooltip = `Risk ${report.risk?.risk_score ?? 0}/100 · ${report.top_issues?.length ?? 0} issue(s) · click to re-analyze`;
  statusBarItem.backgroundColor =
    gate === 'BLOCK'
      ? new vscode.ThemeColor('statusBarItem.errorBackground')
      : gate === 'HOLD'
        ? new vscode.ThemeColor('statusBarItem.warningBackground')
        : undefined;
}
