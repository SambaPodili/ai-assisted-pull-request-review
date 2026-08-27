// vscode-extension/src/apiClient.ts
// -----------------------------------------------------------------------------
// Thin client for the backend's existing REST contract — see
// api/routes/analysis.py. No new backend endpoints were needed for this
// extension; POST /analyse already accepts a raw diff_text and returns a
// request_id to poll, which is exactly what an editor-loop client needs.
//
// Uses the global `fetch` (Node 18+, available in the VS Code extension host
// — no extra HTTP dependency needed).

import { PathReviewConfig } from './pathReviewConfig';
import { ModelOverride } from './settings';

export interface AnalyzeOptions {
  backendUrl: string;
  apiKey: string;
  repoUrl: string;
  sourceRef: string;
  targetRef?: string;
  diffText: string;
  selectedAgents: string[] | null;
  /** Free-text prioritization guidance — scanned client-side (promptGuard.ts)
   * before this is ever called, and re-validated authoritatively server-side
   * (governance/prompt_guard.py) regardless. */
  userInstructions?: string;
  /** Parsed .gto.yaml (pathReviewConfig.ts) — re-validated/re-scanned
   * server-side (core/orchestrator.py) before any of its free text reaches
   * an LLM prompt; this client-side parse is not itself a trust boundary. */
  pathReviewConfig?: PathReviewConfig;
  /** Optional LLM override (settings.ts::getModelOverride) — matches the web
   * app's `llm_config` field exactly (api/routes/analysis.py's
   * AnalyseRequest.llm_config). Undefined = don't send it, use whatever the
   * backend is configured with (today's behavior, unchanged). */
  modelOverride?: ModelOverride;
}

export interface SubmitResponse {
  request_id: string;
  status: 'queued' | 'no_diff' | string;
  message?: string;
}

export interface StatusResponse {
  request_id: string;
  status: 'queued' | 'running' | 'done' | 'unknown' | string;
  queue_position?: number;
  queue_total?: number;
}

export interface CorrelatedIssue {
  title: string;
  file_path: string;
  line: number;
  severity: string;
  confidence: string;
  score: number;
  agents: string[];
  categories: string[];
  descriptions: string[];
}

/** A concrete, copy-pasteable suggested fix — deterministic (not LLM), only
 * populated when the `remediation` agent runs (currently: Thorough preset
 * only). `diff` embeds the source line number as "@@ line N @@" since CodeFix
 * itself doesn't carry a line field — see agents/fix_generator.py. */
export interface CodeFix {
  title: string;
  file_path: string;
  category: string;
  severity: string;
  before: string;
  after: string;
  diff: string;
  explanation: string;
  confidence: string;
}

/** One missing/at-risk test scenario found by the `qa_scenarios` agent
 * (Thorough preset only). `test_skeleton` is a real, language-aware
 * Arrange/Act/Assert stub — generated deterministically from the actual
 * changed symbol, not LLM prose — see agents/qa_scenarios_agent.py. */
export interface QAScenario {
  id: string;
  title: string;
  type: string;
  priority: string;
  description: string;
  steps: string[];
  expected_result: string;
  affected_files: string[];
  automation_hint: string;
  preconditions: string[];
  acceptance_criteria: string[];
  test_skeleton: string;
  test_skeleton_filename: string;
}

export interface QAScenariosResult {
  scenarios: QAScenario[];
  total_scenarios: number;
  critical_count: number;
  high_count: number;
  coverage_areas: string[];
  summary: string;
}

/** Display-only summary of what .gto.yaml did for this run — see
 * core.models.PathReviewSummary. Absent/undefined when no .gto.yaml (or an
 * empty one) was in play, the common case. */
export interface PathReviewSummary {
  agents_excluded: string[];
  steering_applied: boolean;
}

export interface AnalysisReport {
  request_id: string;
  gate_decision: string;
  final_risk: string;
  risk?: { risk_score?: number; rationale?: string };
  remediation?: { code_fixes?: CodeFix[]; fix_suggestions?: string[] };
  qa_scenarios?: QAScenariosResult;
  path_review_summary?: PathReviewSummary | null;
  files_changed: number;
  files_changed_list: string[];
  top_issues: CorrelatedIssue[];
  duration_s: number;
  errors: string[];
}

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
  }
}

function headers(apiKey: string): Record<string, string> {
  return { 'Content-Type': 'application/json', 'X-API-Key': apiKey };
}

async function parseErrorBody(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()) as { detail?: string | { msg?: string }[] };
    const d = body?.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map((x) => x.msg).filter(Boolean).join('; ');
  } catch {
    /* body wasn't JSON — fall through to the generic message below */
  }
  return `Backend error ${resp.status}`;
}

/** A named model choice the backend admin configured (config/settings.py's
 * MODEL_PRESETS) — secret-free (no base_url/api_key), see
 * settings.ts::getSelectedModelPreset for how a picked preset becomes a
 * ModelOverride. */
export interface ModelPreset {
  name: string;
  label: string;
  provider: string;
  model: string;
}

export async function fetchModelPresets(backendUrl: string, apiKey: string): Promise<ModelPreset[]> {
  const resp = await fetch(`${backendUrl}/api/v1/model-presets`, { headers: headers(apiKey) });
  if (resp.status === 401) throw new ApiError('Unauthorized — check your API key (GTO: Set API Key).', 401);
  if (!resp.ok) throw new ApiError(await parseErrorBody(resp), resp.status);
  const body = (await resp.json()) as { presets?: ModelPreset[] };
  return body.presets ?? [];
}

/** Records a reviewer verdict on one finding — the same feedback loop the web
 * app's ResultsView already exposes (⚐/✓ controls). Aggregated over ≥3
 * false_positive verdicts for the same (agent, category, repo),
 * governance/suppression.py auto-suppresses that pattern on future runs of
 * this repo — always visibly, via report.suppressed_notes, never silently. */
export async function submitFindingFeedback(
  backendUrl: string,
  apiKey: string,
  requestId: string,
  body: { agent: string; category?: string; file_path?: string; verdict: string; note?: string }
): Promise<void> {
  const resp = await fetch(`${backendUrl}/api/v1/report/${requestId}/feedback`, {
    method: 'POST',
    headers: headers(apiKey),
    body: JSON.stringify(body),
  });
  if (resp.status === 401) throw new ApiError('Unauthorized — check your API key (GTO: Set API Key).', 401);
  if (!resp.ok) throw new ApiError(await parseErrorBody(resp), resp.status);
}

export async function submitAnalysis(opts: AnalyzeOptions): Promise<SubmitResponse> {
  const resp = await fetch(`${opts.backendUrl}/api/v1/analyse`, {
    method: 'POST',
    headers: headers(opts.apiKey),
    body: JSON.stringify({
      repo_url: opts.repoUrl,
      source_ref: opts.sourceRef,
      target_ref: opts.targetRef ?? 'HEAD',
      change_type: 'branch_diff',
      diff_text: opts.diffText,
      selected_agents: opts.selectedAgents,
      user_instructions: opts.userInstructions ?? '',
      path_review_config: opts.pathReviewConfig ?? null,
      ...(opts.modelOverride ? { llm_config: opts.modelOverride } : {}),
    }),
  });
  if (resp.status === 401) throw new ApiError('Unauthorized — check your API key (GTO: Set API Key).', 401);
  if (!resp.ok) throw new ApiError(await parseErrorBody(resp), resp.status);
  return (await resp.json()) as SubmitResponse;
}

export async function getStatus(backendUrl: string, apiKey: string, requestId: string): Promise<StatusResponse> {
  const resp = await fetch(`${backendUrl}/api/v1/status/${requestId}`, { headers: headers(apiKey) });
  if (!resp.ok) throw new ApiError(await parseErrorBody(resp), resp.status);
  return (await resp.json()) as StatusResponse;
}

export async function getReport(backendUrl: string, apiKey: string, requestId: string): Promise<AnalysisReport> {
  const resp = await fetch(`${backendUrl}/api/v1/report/${requestId}?fmt=full`, { headers: headers(apiKey) });
  if (!resp.ok) throw new ApiError(await parseErrorBody(resp), resp.status);
  return (await resp.json()) as AnalysisReport;
}

/** Polls /status until done, then fetches the full report. Cancelable via `token`. */
export async function runAnalysisToCompletion(
  opts: AnalyzeOptions,
  token: { cancelled: boolean },
  onStatus?: (s: StatusResponse) => void,
  pollMs = 2000
): Promise<AnalysisReport | null> {
  const submitted = await submitAnalysis(opts);
  if (submitted.status === 'no_diff') {
    return null;
  }
  const requestId = submitted.request_id;
  while (!token.cancelled) {
    await new Promise((r) => setTimeout(r, pollMs));
    const s = await getStatus(opts.backendUrl, opts.apiKey, requestId);
    onStatus?.(s);
    if (s.status === 'done') {
      return getReport(opts.backendUrl, opts.apiKey, requestId);
    }
    if (s.status === 'unknown') {
      throw new ApiError('Analysis vanished server-side (restarted backend?) — please retry.');
    }
  }
  return null;
}
