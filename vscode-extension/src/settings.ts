// vscode-extension/src/settings.ts
// -----------------------------------------------------------------------------
// Backend URL + agent preset come from regular VS Code settings (visible,
// synced, fine to be non-secret). The API key is a credential — it goes
// through SecretStorage (OS keychain-backed), never settings.json, never
// workspace state, so it can't leak via synced settings or a shared repo.

import * as vscode from 'vscode';

const SECRET_KEY = 'gto.apiKey';

export function getBackendUrl(): string {
  const url = vscode.workspace.getConfiguration('gto').get<string>('backendUrl', 'http://localhost:8080');
  return url.replace(/\/+$/, '');
}

export type AgentPreset = 'fast' | 'standard' | 'thorough';

export function getAgentPreset(): AgentPreset {
  return vscode.workspace.getConfiguration('gto').get('agentPreset', 'fast');
}

// Mirrors frontend/src/state.js AGENT_PRESETS — kept in sync by hand (small,
// rarely-changing list; see that file for the canonical server-side mapping).
// Note: `remediation` (and therefore Quick Fix code actions, codeActions.ts)
// only runs in `thorough` — Fast/Standard intentionally match the web app's
// definitions rather than diverging just to unlock fixes in the extension.
const AGENT_PRESETS: Record<AgentPreset, string[] | null> = {
  fast: ['code_analysis', 'security'],
  standard: ['code_analysis', 'security', 'dependency', 'test_coverage', 'interface', 'risk'],
  thorough: null, // null = no filtering, run everything (server default)
};

export const AGENT_PRESET_META: { preset: AgentPreset; label: string; description: string }[] = [
  { preset: 'fast', label: 'Fast', description: 'code_analysis + security only — fastest, catches the sharpest issues.' },
  { preset: 'standard', label: 'Standard', description: 'Core review + risk gate (6 agents).' },
  { preset: 'thorough', label: 'Thorough', description: 'All ~22 agents, incl. remediation (unlocks Quick Fix) — slow for an editor loop.' },
];

export function getSelectedAgents(preset: AgentPreset): string[] | null {
  return AGENT_PRESETS[preset] ?? null;
}

/** Off by default — auto-running an LLM-backed analysis on every save is a
 * real cost/latency tradeoff the user should opt into explicitly. */
export function getAutoAnalyzeOnSave(): boolean {
  return vscode.workspace.getConfiguration('gto').get<boolean>('autoAnalyzeOnSave', false);
}

/** .gitignore-flavored glob patterns dropped from every analysis (both
 * Analyze Changes and Analyze Branch), on top of files .gitignore already
 * hides. Default lives in package.json's configuration schema (the source of
 * truth shown in Settings UI); the fallback array here only matters if the
 * schema default is ever missing. */
export function getExcludePatterns(): string[] {
  return vscode.workspace.getConfiguration('gto').get<string[]>('excludePatterns', []);
}

export async function getApiKey(secrets: vscode.SecretStorage): Promise<string | undefined> {
  return secrets.get(SECRET_KEY);
}

export async function setApiKey(secrets: vscode.SecretStorage, key: string): Promise<void> {
  await secrets.store(SECRET_KEY, key);
}

export async function promptForApiKey(secrets: vscode.SecretStorage): Promise<string | undefined> {
  const key = await vscode.window.showInputBox({
    title: 'GTO Pull Request Review Framework — API Key',
    prompt: 'Enter your API key (X-API-Key). Stored securely in your OS keychain, never in settings.',
    password: true,
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() ? undefined : 'API key cannot be empty'),
  });
  if (!key) return undefined;
  await setApiKey(secrets, key.trim());
  return key.trim();
}
