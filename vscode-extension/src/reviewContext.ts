// vscode-extension/src/reviewContext.ts
// -----------------------------------------------------------------------------
// Gathers metadata.* the backend agents can use but apiClient.ts::submitAnalysis
// never sent from the editor — functional_docs, connected_repos, existing_tests,
// external_references. Without these, functional_validation/cross_repo_impact
// silently no-op and test_coverage/dependency lose repo-aware context on every
// VS Code run, even though the exact same diff run through the web app gets
// the full picture. See docs/CAPABILITIES.md gap: "VS Code never sends metadata".
//
// Two of the four are actually CHEAPER here than in the web app: existing_tests
// and external_references there go through backend git-proxy round-trips
// (api/routes/git_proxy.py — /git/file, /git/xref) because a browser has no
// filesystem access. A local checkout does, so both are plain local scans here
// — no git provider credentials needed, no network round-trip, no per-request
// rate limit against the provider's API.

import * as fs from 'node:fs/promises';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { getConnectedRepoPaths, getConnectedRepos, getFunctionalSpecPaths } from './settings';

const MAX_DOCS = 10;
const MAX_DOC_CHARS = 40000; // matches frontend/src/views/ResultsView.jsx's own cap
const MAX_TEST_FILES = 300;
const MAX_REPOS_GREPPED = 5;
const MAX_FILES_PER_REPO = 4000;
const MAX_REFERENCES = 50;
const MAX_FILE_BYTES = 1_000_000; // skip anything bigger — not source code

// Directory names never worth walking into, for the local external_references
// grep — mirrors the top-level segments of gto.excludePatterns' defaults
// (package.json) without needing a full glob engine for an internal scan.
const SKIP_DIRS = new Set([
  '.git', 'node_modules', 'dist', 'out', 'build', 'target', '.gradle',
  '__pycache__', '.venv', 'venv', '.pytest_cache', '.mypy_cache', '.ruff_cache',
  'vendor', '.build', 'DerivedData', 'obj', 'bin', '.idea', '.vscode', '.trunk',
]);

const TEST_GLOBS = [
  '**/test_*.py', '**/*_test.py',
  '**/*.test.{js,jsx,ts,tsx}', '**/*.spec.{js,jsx,ts,tsx}',
  '**/*Test.java', '**/*Tests.java', '**/*Test.kt', '**/*Tests.kt',
  '**/*_test.go',
  '**/*Test.cs', '**/*Tests.cs',
  '**/*_spec.rb', '**/*_test.rb',
  '**/*Test.php',
  '**/*Tests.swift',
  '**/*_test.dart',
];

/** Rough, best-effort symbol-definition matchers for the languages this repo
 * already special-cases elsewhere (qa_scenarios_agent.py's _lang_for, etc.) —
 * good enough to seed a local grep, not a real parser. Only run against ADDED
 * diff lines (never removed/context), so a rename shows up as the NEW name. */
const SYMBOL_PATTERNS: RegExp[] = [
  /^\+\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/,
  /^\+\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)/,
  /^\+\s*def\s+([A-Za-z_]\w*)/,
  /^\+\s*fun\s+([A-Za-z_]\w*)/,
  /^\+\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)/,
  /^\+\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)/,
  // Java/C#/Kotlin method — modifiers + return type + name(args). Deliberately
  // requires at least one access/other modifier to avoid matching arbitrary
  // added statements that merely contain "word word(".
  /^\+\s*(?:public|private|protected|internal|static|final|override|virtual|async)\b[\w<>\[\],\s]*\s+([A-Za-z_]\w*)\s*\(/,
];

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Changed/added top-level symbol names from the diff — the seed set for the
 * local cross-repo grep. Deduped, capped so a huge diff can't blow up the walk. */
function extractChangedSymbols(diffText: string): string[] {
  const names = new Set<string>();
  for (const line of diffText.split('\n')) {
    if (!line.startsWith('+') || line.startsWith('+++')) continue;
    for (const re of SYMBOL_PATTERNS) {
      const m = re.exec(line);
      if (m?.[1] && m[1].length > 2) {
        names.add(m[1]);
        break;
      }
    }
    if (names.size >= 40) break;
  }
  return [...names];
}

/** Existing test files already in the repo (not part of this diff) — sent as
 * metadata.existing_tests so test_coverage_agent can see a changed source file
 * is covered by a test that lives elsewhere in the repo, instead of flagging
 * every change without an in-diff test as a gap. Only `.path` is read
 * server-side (agents/test_coverage_agent.py::_existing_test_basenames,
 * matched by basename) — no file content needs sending. */
export async function gatherExistingTests(repoRoot: string): Promise<{ path: string }[]> {
  try {
    const rootUri = vscode.Uri.file(repoRoot);
    const results = await Promise.all(
      TEST_GLOBS.map((g) =>
        vscode.workspace.findFiles(
          new vscode.RelativePattern(rootUri, g),
          '{**/node_modules/**,**/.git/**,**/dist/**,**/build/**,**/target/**,**/.venv/**,**/venv/**}',
          MAX_TEST_FILES
        )
      )
    );
    const seen = new Set<string>();
    const out: { path: string }[] = [];
    for (const uris of results) {
      for (const uri of uris) {
        const rel = path.relative(repoRoot, uri.fsPath).replace(/\\/g, '/');
        if (!seen.has(rel)) {
          seen.add(rel);
          out.push({ path: rel });
        }
        if (out.length >= MAX_TEST_FILES) return out;
      }
    }
    return out;
  } catch {
    return []; // best-effort — never block analysis over this
  }
}

/** Reviewer-declared dependent repos (names only, no local access needed) —
 * sent as metadata.connected_repos, the dependency agent's blast-radius
 * baseline (agents/dependency_agent.py::_declared_dependents). */
export function gatherConnectedRepos(): string[] {
  return getConnectedRepos().filter(Boolean);
}

async function walkForSymbols(
  repoLocalPath: string, repoLabel: string, symbols: string[], budget: { filesLeft: number }
): Promise<{ symbol: string; file_path: string; line: number; context: string; repo: string }[]> {
  const patterns = symbols.map((s) => ({ symbol: s, re: new RegExp(`\\b${escapeRegExp(s)}\\b`) }));
  const found: { symbol: string; file_path: string; line: number; context: string; repo: string }[] = [];

  async function walk(dir: string): Promise<void> {
    if (found.length >= MAX_REFERENCES || budget.filesLeft <= 0) return;
    let entries: import('node:fs').Dirent[];
    try {
      entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (found.length >= MAX_REFERENCES || budget.filesLeft <= 0) return;
      if (entry.name.startsWith('.') && entry.name !== '.') {
        if (!SKIP_DIRS.has(entry.name)) continue; // dotfiles other than known dirs: skip, not worth walking
        continue;
      }
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) continue;
        await walk(full);
        continue;
      }
      if (!entry.isFile()) continue;
      budget.filesLeft--;
      let stat;
      try {
        stat = await fs.stat(full);
      } catch {
        continue;
      }
      if (stat.size === 0 || stat.size > MAX_FILE_BYTES) continue;
      let text: string;
      try {
        text = await fs.readFile(full, 'utf8');
      } catch {
        continue; // binary or unreadable — skip rather than fail the whole walk
      }
      const lines = text.split('\n');
      for (let i = 0; i < lines.length; i++) {
        for (const { symbol, re } of patterns) {
          if (re.test(lines[i])) {
            found.push({
              symbol,
              file_path: path.relative(repoLocalPath, full).replace(/\\/g, '/'),
              line: i + 1,
              context: lines[i].trim().slice(0, 200),
              repo: repoLabel,
            });
            if (found.length >= MAX_REFERENCES) return;
            break;
          }
        }
      }
    }
  }

  await walk(repoLocalPath);
  return found;
}

/** Call-sites of this diff's changed symbols found in locally-checked-out
 * dependent repos — sent as metadata.external_references, the same shape the
 * backend's own cross-repo search produces (api/routes/git_proxy.py::/xref):
 * {symbol, file_path, line, context, repo}. Consumed by
 * agents/cross_repo_impact_agent.py and agents/reference_impact_agent.py.
 * Purely local — no git provider credentials needed, unlike the web app's
 * equivalent (which has no filesystem access and must go through the
 * provider's code-search API or a server-side clone). */
export async function gatherExternalReferences(diffText: string): Promise<
  { symbol: string; file_path: string; line: number; context: string; repo: string }[]
> {
  const repoPaths = getConnectedRepoPaths().filter(Boolean).slice(0, MAX_REPOS_GREPPED);
  if (!repoPaths.length) return [];
  const symbols = extractChangedSymbols(diffText);
  if (!symbols.length) return [];

  const out: { symbol: string; file_path: string; line: number; context: string; repo: string }[] = [];
  for (const repoPath of repoPaths) {
    if (out.length >= MAX_REFERENCES) break;
    try {
      const st = await fs.stat(repoPath);
      if (!st.isDirectory()) continue;
    } catch {
      continue; // configured path doesn't exist (yet) on this machine — skip, don't fail the run
    }
    const label = path.basename(repoPath.replace(/[\\/]+$/, ''));
    const matches = await walkForSymbols(repoPath, label, symbols, { filesLeft: MAX_FILES_PER_REPO });
    out.push(...matches);
  }
  return out.slice(0, MAX_REFERENCES);
}

/** Uploaded functional/requirement spec text — sent as metadata.functional_docs
 * ({name, text}[]), enabling agents/functional_validation_agent.py. .md/.txt
 * are read directly; .docx/.pdf go through the backend's own /docs/extract
 * (the same endpoint the web app's document-upload UI calls) since parsing
 * those formats client-side isn't worth a new dependency here. */
export async function gatherFunctionalDocs(
  repoRoot: string, backendUrl: string, apiKey: string
): Promise<{ name: string; text: string }[]> {
  const configured = getFunctionalSpecPaths().filter(Boolean).slice(0, MAX_DOCS);
  if (!configured.length) return [];

  const out: { name: string; text: string }[] = [];
  for (const p of configured) {
    const abs = path.isAbsolute(p) ? p : path.join(repoRoot, p);
    const name = path.basename(abs);
    const ext = path.extname(abs).toLowerCase();
    try {
      const bytes = await fs.readFile(abs);
      if (ext === '.md' || ext === '.txt') {
        out.push({ name, text: bytes.toString('utf8').slice(0, MAX_DOC_CHARS) });
        continue;
      }
      if (ext === '.docx' || ext === '.pdf') {
        if (!backendUrl || !apiKey) continue; // extraction needs the backend — skip silently
        const resp = await fetch(`${backendUrl}/api/v1/docs/extract?filename=${encodeURIComponent(name)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream', 'X-API-Key': apiKey },
          body: bytes,
        });
        if (!resp.ok) continue;
        const d = (await resp.json()) as { text?: string };
        if (d.text) out.push({ name, text: d.text.slice(0, MAX_DOC_CHARS) });
        continue;
      }
      // Unrecognized extension — best-effort raw read, same as .md/.txt.
      out.push({ name, text: bytes.toString('utf8').slice(0, MAX_DOC_CHARS) });
    } catch {
      // Missing/unreadable file, or extraction failed — skip it, don't fail
      // the whole analysis over one bad spec path.
    }
  }
  return out;
}

/** Everything above, combined into the metadata object apiClient.ts sends
 * with the analysis request. Every gatherer is independently best-effort —
 * one failing/empty source never blocks or degrades the others. */
export async function gatherReviewContext(
  repoRoot: string, diffText: string, backendUrl: string, apiKey: string
): Promise<Record<string, unknown>> {
  const [existingTests, externalReferences, functionalDocs] = await Promise.all([
    gatherExistingTests(repoRoot),
    gatherExternalReferences(diffText),
    gatherFunctionalDocs(repoRoot, backendUrl, apiKey),
  ]);
  const connectedRepos = gatherConnectedRepos();

  const metadata: Record<string, unknown> = {};
  if (connectedRepos.length) metadata.connected_repos = connectedRepos;
  if (existingTests.length) metadata.existing_tests = existingTests;
  if (externalReferences.length) metadata.external_references = externalReferences;
  if (functionalDocs.length) metadata.functional_docs = functionalDocs;
  return metadata;
}
