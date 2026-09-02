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

/** An LLM-narrated (not call-graph-grounded) sequence diagram for a complex
 * change — see agents/remediation_agent.py::_maybe_generate_diagram. Only
 * generated on the Thorough preset, and only when the change has real
 * reference_impact data AND medium+ risk — most reports won't have one.
 * confidence is hardcoded "low" server-side, never LLM-settable — same
 * "review before relying on it" framing as an AI-suggested code fix. */
export interface MermaidDiagram {
  diagram_type: string;
  mermaid_source: string;
  confidence: string;
  note: string;
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
  repo_url: string;
  gate_decision: string;
  final_risk: string;
  risk?: { risk_score?: number; rationale?: string };
  remediation?: { code_fixes?: CodeFix[]; fix_suggestions?: string[]; diagrams?: MermaidDiagram[] };
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

/** Node's global fetch() collapses every network-level failure (DNS,
 * connection refused, timeout, TLS) into a generic "fetch failed" — the
 * actual reason lives on `err.cause`, which a plain `.message` read drops
 * silently. Walks the cause chain so the real reason (e.g. "connect
 * ECONNREFUSED 10.0.0.5:8080") reaches the user instead of a dead end. Only
 * matters for errors that never got an HTTP response — ApiError (a real
 * response came back) already carries a useful, backend-provided message. */
export function describeError(e: unknown): string {
  if (!(e instanceof Error)) return String(e);
  const parts = [e.message];
  let cause: unknown = (e as { cause?: unknown }).cause;
  let depth = 0;
  while (cause && depth < 5) {
    if (cause instanceof Error) {
      parts.push(cause.message);
      cause = (cause as { cause?: unknown }).cause;
    } else {
      parts.push(String(cause));
      cause = undefined;
    }
    depth++;
  }
  return parts.filter((p, i) => parts.indexOf(p) === i).join(' — caused by: ');
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

/** One past analysis similar to the current one — file-overlap + summary-keyword
 * similarity, see api/routes/insights.py::similar_prs. Same web-app feature
 * already shown in the frontend's ResultsView, now also in the panel. */
export interface SimilarPR {
  request_id: string;
  repo: string;
  pr_title: string;
  pr_number: number;
  source_ref: string;
  risk_score: number;
  gate: string;
  similarity: number;
  shared_files: string[];
  elapsed: string;
}

/** Best-effort — a nice-to-have context panel, never worth surfacing an error
 * for. Callers should treat a thrown/empty result as "just hide the section." */
export async function fetchSimilarPRs(backendUrl: string, apiKey: string, requestId: string, topK = 5): Promise<SimilarPR[]> {
  const resp = await fetch(`${backendUrl}/api/v1/insights/similar/${requestId}?top_k=${topK}`, { headers: headers(apiKey) });
  if (!resp.ok) return [];
  const body = (await resp.json()) as { similar?: SimilarPR[] };
  return body.similar ?? [];
}

/** Records a reviewer verdict on one finding — the same feedback loop the web
 * app's ResultsView already exposes (⚐/✓ controls). Aggregated over ≥3
 * false_positive verdicts for the same (agent, category, repo),
 * governance/suppression.py auto-suppresses that pattern on future runs of
 * this repo — always visibly, via report.suppressed_notes, never silently. */
/** Posts findings as PR comments (one grouped comment per file + an overall
 * summary) — reuses the existing POST /report/{id}/comment-pr endpoint
 * unchanged. Deliberately omits token/base_url/workspace so the backend
 * falls back to its own shared bot credential (the same identity webhook-
 * triggered comments already post under) — no personal token needed. */
export async function postFindingsToPR(
  backendUrl: string,
  apiKey: string,
  requestId: string,
  opts: { repoSlug: string; prId: string; provider: string }
): Promise<{ ok: boolean; comments_posted: number; files_commented: number }> {
  const resp = await fetch(`${backendUrl}/api/v1/report/${requestId}/comment-pr`, {
    method: 'POST',
    headers: headers(apiKey),
    body: JSON.stringify({ provider: opts.provider, repo_slug: opts.repoSlug, pr_id: opts.prId, inline: true }),
  });
  if (resp.status === 401) throw new ApiError('Unauthorized — check your API key (GTO: Set API Key).', 401);
  if (!resp.ok) throw new ApiError(await parseErrorBody(resp), resp.status);
  return (await resp.json()) as { ok: boolean; comments_posted: number; files_commented: number };
}

/** Reviewer sign-off — approval only, never merges. Requires the reviewer's
 * OWN token (no server-side fallback, unlike postFindingsToPR) so the
 * approval shows as them, not the shared bot — see settings.ts's
 * getBitbucketToken doc comment for why. Distinct endpoint from gate
 * override: this never changes GTO's own gate decision. */
export async function approvePR(
  backendUrl: string,
  apiKey: string,
  requestId: string,
  opts: { provider: string; token: string; repoSlug: string; prId: string }
): Promise<{ status: string; pr_action: { ok: boolean; errors?: string[] } }> {
  const resp = await fetch(`${backendUrl}/api/v1/gate/${requestId}/approve-pr`, {
    method: 'POST',
    headers: headers(apiKey),
    body: JSON.stringify({ provider: opts.provider, token: opts.token, repo_slug: opts.repoSlug, pr_id: opts.prId }),
  });
  if (resp.status === 401) throw new ApiError('Unauthorized — check your API key (GTO: Set API Key).', 401);
  if (resp.status === 403) throw new ApiError('Your API key does not have PR-approval permission.', 403);
  if (!resp.ok) throw new ApiError(await parseErrorBody(resp), resp.status);
  return (await resp.json()) as { status: string; pr_action: { ok: boolean; errors?: string[] } };
}

/** "Explain this finding" — a fixed, never-user-typed question, answered by
 * the same guardrailed Q&A engine already used for PR chat replies. */
export async function explainFinding(
  backendUrl: string,
  apiKey: string,
  requestId: string,
  body: { agent: string; category?: string; file_path?: string; title?: string }
): Promise<string> {
  const resp = await fetch(`${backendUrl}/api/v1/report/${requestId}/explain-finding`, {
    method: 'POST',
    headers: headers(apiKey),
    body: JSON.stringify(body),
  });
  if (resp.status === 401) throw new ApiError('Unauthorized — check your API key (GTO: Set API Key).', 401);
  if (!resp.ok) {
    const detail = await parseErrorBody(resp);
    // A bare "Not Found" (FastAPI's unknown-route body) means this backend
    // build predates the /explain-finding endpoint — distinct from
    // "Report '…' not found." which is a real, expired report.
    if (resp.status === 404 && /^not found\.?$/i.test(detail.trim())) {
      throw new ApiError(
        'This GTO backend is too old to support Explain — ask your admin to update it to the latest build.',
        404,
      );
    }
    throw new ApiError(detail, resp.status);
  }
  const data = (await resp.json()) as { answer?: string };
  return data.answer ?? '';
}

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
