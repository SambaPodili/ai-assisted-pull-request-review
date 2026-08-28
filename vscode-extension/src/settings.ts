// vscode-extension/src/settings.ts
// -----------------------------------------------------------------------------
// Backend URL + agent preset come from regular VS Code settings (visible,
// synced, fine to be non-secret). The API key is a credential — it goes
// through SecretStorage (OS keychain-backed), never settings.json, never
// workspace state, so it can't leak via synced settings or a shared repo.

import * as vscode from 'vscode';

const SECRET_KEY = 'gto.apiKey';
const MODEL_SECRET_KEY = 'gto.modelApiKey';
const BITBUCKET_SECRET_KEY = 'gto.bitbucketToken';

export function getBackendUrl(): string {
  const url = vscode.workspace.getConfiguration('gto').get<string>('backendUrl', 'http://localhost:8080');
  return url.replace(/\/+$/, '');
}

/** Which provider "Post to PR"/"Approve PR" target — matches the backend's
 * own GIT_PROVIDER setting so repo_slug/URL shapes line up. Only relevant to
 * those two report-level actions; local Analyze Changes/Branch never needs it. */
export function getGitProvider(): 'github' | 'bitbucket' | 'bitbucket_server' {
  return vscode.workspace.getConfiguration('gto').get('gitProvider', 'github');
}

/** Pre-push hook behavior — "warn" (default, never blocks) or "block"
 * (refuses the push on a BLOCK-severity finding). See gitHook.ts. */
export function getGitHookMode(): 'warn' | 'block' {
  return vscode.workspace.getConfiguration('gto').get('gitHookMode', 'warn');
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

// ── Model override — mirrors the web app's Configure → AI Model panel
// (frontend/src/state.js's modelProvider/modelName/modelApiKey/modelBaseUrl/
// modelApiVer, sent as `llm_config` in the request body). Entirely opt-in:
// leaving gto.modelProvider unset sends no llm_config at all, byte-identical
// to today's behaviour — the backend then uses whatever it's configured
// with. The API key is a credential, so it goes through SecretStorage like
// the main API key, never a plain setting.
//
// Two ways to pick a model, resolved in extension.ts's runAnalysis:
//  1. gto.modelPreset — the PRIMARY path for a shared multi-user backend.
//     Names a preset the admin already configured server-side (see
//     config/settings.py's MODEL_PRESETS / GET /api/v1/model-presets in
//     apiClient.ts::fetchModelPresets) — no credential ever leaves the
//     server or touches this extension.
//  2. gto.modelProvider/modelName/modelBaseUrl/modelApiVersion below — the
//     ADVANCED/manual path, for a personal backend where you want to bring
//     your own separate provider or endpoint. Only consulted when
//     gto.modelPreset is empty.

export interface ModelOverride {
  provider: string;
  model: string;
  api_key: string;
  base_url: string;
  api_version: string;
}

export function getSelectedModelPreset(): string {
  return (vscode.workspace.getConfiguration('gto').get<string>('modelPreset', '') || '').trim();
}

export async function setSelectedModelPreset(name: string): Promise<void> {
  await vscode.workspace.getConfiguration('gto').update('modelPreset', name, vscode.ConfigurationTarget.Global);
}

/** The ADVANCED/manual override — undefined when gto.modelProvider is
 * unset/empty (the common case: no gto.modelPreset either, so nothing is
 * overridden at all). A blank api_key here is intentional, not an error: it
 * tells the backend "use the configured env key for this provider" rather
 * than supplying the caller's own — the same fallback the web app relies on.
 * Only called when gto.modelPreset is empty — see extension.ts::runAnalysis. */
export async function getModelOverride(secrets: vscode.SecretStorage): Promise<ModelOverride | undefined> {
  const cfg = vscode.workspace.getConfiguration('gto');
  const provider = (cfg.get<string>('modelProvider', '') || '').trim();
  if (!provider) return undefined;
  return {
    provider,
    model: (cfg.get<string>('modelName', '') || '').trim(),
    api_key: (await getModelApiKey(secrets)) || '',
    base_url: (cfg.get<string>('modelBaseUrl', '') || '').trim(),
    api_version: (cfg.get<string>('modelApiVersion', '') || '').trim(),
  };
}

export async function getModelApiKey(secrets: vscode.SecretStorage): Promise<string | undefined> {
  return secrets.get(MODEL_SECRET_KEY);
}

export async function setModelApiKey(secrets: vscode.SecretStorage, key: string): Promise<void> {
  await secrets.store(MODEL_SECRET_KEY, key);
}

export async function promptForModelApiKey(secrets: vscode.SecretStorage): Promise<void> {
  const key = await vscode.window.showInputBox({
    title: 'GTO — Model API Key',
    prompt: 'API key for the model provider set in gto.modelProvider. Leave blank and press Enter to clear it (falls back to the backend\'s configured key).',
    password: true,
    ignoreFocusOut: true,
  });
  if (key === undefined) return; // Escape — leave the stored key untouched
  await setModelApiKey(secrets, key.trim());
  vscode.window.showInformationMessage(key.trim() ? 'GTO: model API key saved.' : 'GTO: model API key cleared.');
}

/** Personal Bitbucket/GitHub access token — used ONLY for "Approve PR"
 * (resultsPanel.ts::handleApprovePr), never for "Post to PR" (which
 * deliberately uses the backend's shared bot credential instead — see
 * apiClient.ts::postFindingsToPR). Approving as the shared bot would defeat
 * the entire point of this feature: the approval must show as YOU on the
 * PR, for audit/compliance purposes, not "GTO Bot". */
export async function getBitbucketToken(secrets: vscode.SecretStorage): Promise<string | undefined> {
  return secrets.get(BITBUCKET_SECRET_KEY);
}

export async function setBitbucketToken(secrets: vscode.SecretStorage, key: string): Promise<void> {
  await secrets.store(BITBUCKET_SECRET_KEY, key);
}

export async function promptForBitbucketToken(secrets: vscode.SecretStorage): Promise<void> {
  const key = await vscode.window.showInputBox({
    title: 'GTO — Personal Git Provider Token',
    prompt: 'Your own Bitbucket/GitHub access token, used only for "Approve PR" so the approval shows as you, not the shared bot. Stored securely in your OS keychain.',
    password: true,
    ignoreFocusOut: true,
  });
  if (key === undefined) return; // Escape — leave the stored token untouched
  await setBitbucketToken(secrets, key.trim());
  vscode.window.showInformationMessage(key.trim() ? 'GTO: personal token saved.' : 'GTO: personal token cleared.');
}
