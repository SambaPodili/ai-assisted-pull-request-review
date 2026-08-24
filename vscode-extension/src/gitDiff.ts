// vscode-extension/src/gitDiff.ts
// -----------------------------------------------------------------------------
// Git access via the `git` CLI directly (not VS Code's built-in git extension
// API) — fewer moving parts, no dependency on that extension being active.

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import * as vscode from 'vscode';

const execFileAsync = promisify(execFile);

export interface RepoRoot {
  cwd: string;
  branch: string;
  repoUrl: string;
}

export class GitError extends Error {}

async function git(cwd: string, args: string[]): Promise<string> {
  try {
    const { stdout } = await execFileAsync('git', args, { cwd, maxBuffer: 1024 * 1024 * 50 });
    return stdout;
  } catch (err: any) {
    throw new GitError(err?.stderr?.trim() || err?.message || `git ${args.join(' ')} failed`);
  }
}

/** Derives a `namespace/repo`-shaped identifier from a git remote URL, or a
 * sensible local fallback when there's no remote — the backend only stores
 * this for display/logging, it never resolves it against a live provider
 * for a diff_text-only submission. */
function normalizeRepoUrl(remoteUrl: string, folderName: string): string {
  const trimmed = remoteUrl.trim();
  if (!trimmed) return `local/${folderName}`;
  // git@github.com:org/repo.git -> https://github.com/org/repo
  const sshMatch = trimmed.match(/^git@([^:]+):(.+?)(\.git)?$/);
  if (sshMatch) return `https://${sshMatch[1]}/${sshMatch[2]}`;
  return trimmed.replace(/\.git$/, '');
}

async function isGitRepo(cwd: string): Promise<boolean> {
  try {
    await git(cwd, ['rev-parse', '--is-inside-work-tree']);
    return true;
  } catch {
    return false;
  }
}

async function loadRepoRoot(folder: vscode.WorkspaceFolder): Promise<RepoRoot> {
  const cwd = folder.uri.fsPath;
  const [branchRaw, remoteRaw] = await Promise.all([
    git(cwd, ['rev-parse', '--abbrev-ref', 'HEAD']).catch(() => 'HEAD'),
    git(cwd, ['remote', 'get-url', 'origin']).catch(() => ''),
  ]);
  return { cwd, branch: branchRaw.trim() || 'HEAD', repoUrl: normalizeRepoUrl(remoteRaw, folder.name) };
}

/**
 * Resolves which open workspace folder to analyze. With exactly one git-repo
 * folder open, picks it directly — zero added friction for the common case.
 * With more than one AND `interactive`, prompts so you're never silently
 * analyzing the wrong repo. Non-interactive callers (auto-analyze-on-save)
 * pass `interactive: false` to silently fall back to the first repo folder
 * instead — a picker popping up mid-typing would be worse than a wrong
 * guess for a background convenience feature. Doesn't fetch any diff — see
 * getUncommittedDiff / getBranchDiff.
 */
export async function resolveRepoRoot(interactive = true): Promise<RepoRoot> {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    throw new GitError('No folder is open — open a git repository first.');
  }

  const checks = await Promise.all(folders.map(async (f) => ({ folder: f, isRepo: await isGitRepo(f.uri.fsPath) })));
  const repoFolders = checks.filter((c) => c.isRepo).map((c) => c.folder);

  if (repoFolders.length === 0) {
    throw new GitError('No open folder is a git repository.');
  }
  if (repoFolders.length === 1 || !interactive) {
    return loadRepoRoot(repoFolders[0]);
  }

  const picked = await vscode.window.showQuickPick(
    repoFolders.map((f) => ({ label: f.name, description: f.uri.fsPath, folder: f })),
    { title: 'Multiple git repos are open — which one should GTO analyze?' }
  );
  if (!picked) {
    throw new GitError('Cancelled.');
  }
  return loadRepoRoot(picked.folder);
}

/** Staged + unstaged changes vs HEAD (your last commit). */
export async function getUncommittedDiff(cwd: string): Promise<string> {
  return git(cwd, ['diff', 'HEAD', '--']);
}

/** Local branches other than the current one, most-recently-committed first —
 * for the "compare to branch" quick pick. */
export async function listOtherBranches(cwd: string, currentBranch: string): Promise<string[]> {
  const raw = await git(cwd, ['for-each-ref', '--sort=-committerdate', '--format=%(refname:short)', 'refs/heads/']);
  return raw
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && l !== currentBranch);
}

/** What `currentBranch` adds since it diverged from `base` — a three-dot diff
 * against the merge-base, matching what a PR against `base` would contain
 * (not "everything different between the two tips", which would also include
 * commits made to `base` after the branches diverged). */
export async function getBranchDiff(cwd: string, base: string): Promise<string> {
  return git(cwd, ['diff', `${base}...HEAD`, '--']);
}
