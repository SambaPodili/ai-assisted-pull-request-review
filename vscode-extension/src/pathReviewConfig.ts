// vscode-extension/src/pathReviewConfig.ts
// -----------------------------------------------------------------------------
// Reads a repo's own .gto.yaml — per-path review rules (e.g. "skip the
// performance agent under scripts/**", "steer stricter review for
// payments/**"). Same file, same schema, and same trust treatment as the
// backend's ingestion/path_review_config.py — the VS Code extension already
// has full workspace filesystem access, so it loads the file directly and
// sends the parsed content along with the analysis request (see
// core.models.AnalysisRequest.path_review_config on the backend, which
// re-scans any free text here through governance.prompt_guard before it
// ever reaches an LLM prompt — never trust this file's content by itself).
//
// Malformed or missing .gto.yaml is never an error — it just means no
// path-scoped rules for this run, same failure mode as any other optional
// per-repo file (mirrors reportState.ts's .gto-ignore.json handling).

import * as vscode from 'vscode';
import * as yaml from 'js-yaml';

export interface PathReviewRule {
  match: string;
  agents?: string[] | null;
  user_instructions?: string;
  skip?: boolean;
}

export interface PathReviewConfig {
  version: number;
  paths: PathReviewRule[];
}

const CONFIG_FILE = '.gto.yaml';
const MAX_CONFIG_BYTES = 64_000; // defensive cap — this is a small hand-written file

function configUri(repoRoot: string): vscode.Uri {
  return vscode.Uri.joinPath(vscode.Uri.file(repoRoot), CONFIG_FILE);
}

function isValidRule(v: unknown): v is PathReviewRule {
  if (!v || typeof v !== 'object') return false;
  const r = v as Record<string, unknown>;
  return typeof r.match === 'string' && r.match.length > 0;
}

/** Parses already-read .gto.yaml text. Exported separately from
 * loadPathReviewConfig so it's testable without touching the filesystem. */
export function parsePathReviewConfig(raw: string): PathReviewConfig | undefined {
  if (!raw || raw.length > MAX_CONFIG_BYTES) return undefined;
  try {
    const data = yaml.load(raw);
    if (!data || typeof data !== 'object') return undefined;
    const d = data as Record<string, unknown>;
    const rawPaths = Array.isArray(d.paths) ? d.paths : [];
    const paths = rawPaths.filter(isValidRule);
    if (!paths.length) return undefined;
    return { version: typeof d.version === 'number' ? d.version : 1, paths };
  } catch {
    return undefined; // malformed YAML — proceed without path-scoped rules
  }
}

export async function loadPathReviewConfig(repoRoot: string): Promise<PathReviewConfig | undefined> {
  try {
    const bytes = await vscode.workspace.fs.readFile(configUri(repoRoot));
    return parsePathReviewConfig(Buffer.from(bytes).toString('utf8'));
  } catch {
    return undefined; // file doesn't exist — the common case, not an error
  }
}
