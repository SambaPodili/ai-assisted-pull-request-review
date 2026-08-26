// vscode-extension/src/reportState.ts
// -----------------------------------------------------------------------------
// Two kinds of local, cross-run state — deliberately kept separate:
//  - Suppressions: a team decision ("we've triaged this, stop flagging it"),
//    persisted to a git-trackable .gto-ignore.json at the repo root so it's
//    shared the same way a .eslintignore or `# noqa` comment would be.
//  - "Seen" snapshots: purely local bookkeeping for the delta view (what's
//    new since your last run on this branch), stored in workspace state —
//    nobody else needs to see that, and committing it would just be git
//    noise on every analysis run.

import * as vscode from 'vscode';

/** Stable identity for one finding — (file, line) is enough in practice: if
 * the line moves, treating it as a "new" location (rather than matching a
 * stale suppression/seen-entry to the wrong code) is the safer failure mode. */
export function fingerprint(filePath: string, line: number): string {
  return `${filePath}:${line || 0}`;
}

export interface SuppressedEntry {
  fingerprint: string;
  file_path: string;
  line: number;
  title: string;
  reason: string;
  suppressed_at: string; // ISO date
}

const IGNORE_FILE = '.gto-ignore.json';

function ignoreUri(repoRoot: string): vscode.Uri {
  return vscode.Uri.joinPath(vscode.Uri.file(repoRoot), IGNORE_FILE);
}

export async function loadSuppressed(repoRoot: string): Promise<SuppressedEntry[]> {
  try {
    const bytes = await vscode.workspace.fs.readFile(ignoreUri(repoRoot));
    const parsed = JSON.parse(Buffer.from(bytes).toString('utf8'));
    return Array.isArray(parsed?.suppressed) ? parsed.suppressed : [];
  } catch {
    return []; // file doesn't exist yet, or isn't valid JSON — treat as empty
  }
}

async function saveSuppressed(repoRoot: string, entries: SuppressedEntry[]): Promise<void> {
  const content = JSON.stringify({ suppressed: entries }, null, 2) + '\n';
  await vscode.workspace.fs.writeFile(ignoreUri(repoRoot), Buffer.from(content, 'utf8'));
}

export async function addSuppression(repoRoot: string, entry: SuppressedEntry): Promise<void> {
  const entries = await loadSuppressed(repoRoot);
  const without = entries.filter((e) => e.fingerprint !== entry.fingerprint);
  without.push(entry);
  await saveSuppressed(repoRoot, without);
}

export async function removeSuppression(repoRoot: string, fp: string): Promise<void> {
  const entries = await loadSuppressed(repoRoot);
  await saveSuppressed(repoRoot, entries.filter((e) => e.fingerprint !== fp));
}

// ── Delta / "seen" snapshots ─────────────────────────────────────────────────

const SEEN_KEY_PREFIX = 'gto.seenIssues::';

function seenKey(repoRoot: string, sourceRef: string): string {
  return `${SEEN_KEY_PREFIX}${repoRoot}::${sourceRef}`;
}

/** Fingerprints seen as of the END of the previous run on this (repo, branch)
 * — undefined if this is the first run (no baseline to diff against yet). */
export function getLastSeenFingerprints(
  state: vscode.Memento,
  repoRoot: string,
  sourceRef: string
): Set<string> | undefined {
  const stored = state.get<string[]>(seenKey(repoRoot, sourceRef));
  return stored ? new Set(stored) : undefined;
}

export async function setLastSeenFingerprints(
  state: vscode.Memento,
  repoRoot: string,
  sourceRef: string,
  fingerprints: string[]
): Promise<void> {
  await state.update(seenKey(repoRoot, sourceRef), fingerprints);
}
