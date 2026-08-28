// vscode-extension/src/gitHook.ts
// -----------------------------------------------------------------------------
// "GTO: Install Git Hook" writes a local, untracked .git/hooks/pre-push that
// runs a Fast-preset (code_analysis + security only — hooks must stay fast)
// GTO check on whatever's about to be pushed. Warn-only by default
// (gto.gitHookMode); can be set to block a push on a BLOCK-severity finding.
//
// The script embeds the backend URL, API key, and hook mode as plain shell
// variables — safe here specifically because .git/hooks/ is already
// local-only and untracked by git (same trust level as SecretStorage's
// on-disk keychain backing, just a different storage location, required
// because a git hook runs as a separate process outside the extension host
// and can't reach SecretStorage directly).
//
// Known limitation: resolves hooks under `<repo>/.git/hooks/` directly — a
// git worktree or submodule (where `.git` is a file, not a directory,
// pointing elsewhere) isn't handled. Fine for the common case; not worth the
// extra git-common-dir resolution for a v1 opt-in convenience feature.

import * as vscode from 'vscode';
import * as fs from 'node:fs/promises';
import { resolveRepoRoot } from './gitDiff';
import { getApiKey, getBackendUrl, getGitHookMode } from './settings';

const MARKER = '# === GTO pre-push hook';

function buildScript(backendUrl: string, apiKey: string, mode: string): string {
  return `#!/usr/bin/env bash
${MARKER} — managed by "GTO: Install Git Hook", remove via "GTO: Uninstall Git Hook" ===
# Runs a Fast-preset GTO analysis on what's about to be pushed. Never blocks
# by default (mode=warn); set gto.gitHookMode to "block" and reinstall to
# refuse a push when the gate comes back BLOCK.
set -u
GTO_BACKEND_URL="${backendUrl}"
GTO_API_KEY="${apiKey}"
GTO_HOOK_MODE="${mode}"
ZERO="0000000000000000000000000000000000000000"
EMPTY_TREE="4b825dc642cb6eb9a060e54bf8d69288fbee4904"

json_escape() {
  # Minimal, dependency-free JSON string escaping for a best-effort local
  # check — not a hardened parser. awk, not sed's ":a;N;\$!ba" idiom, since
  # BSD sed (macOS's default, and this hook runs on a developer's own
  # machine) doesn't treat \\n in a pattern as "newline" the way GNU sed
  # does — that idiom silently no-ops there and leaves raw newlines in the
  # JSON string, breaking it. awk's per-line loop works identically on both.
  awk '{ gsub(/\\\\/, "\\\\\\\\"); gsub(/"/, "\\\\\\""); lines[NR] = $0 }
    END {
      for (i = 1; i <= NR; i++) {
        printf "%s", lines[i]
        if (i < NR) printf "\\\\n"
      }
    }'
}

while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "$ZERO" ] && continue

  base="$remote_sha"
  [ "$base" = "$ZERO" ] && base="$EMPTY_TREE"

  diff=$(git diff "$base" "$local_sha" -- 2>/dev/null | head -c 200000)
  [ -z "$diff" ] && continue

  echo "GTO: checking $(basename "$remote_ref")…"
  esc_diff=$(printf '%s' "$diff" | json_escape)
  payload="{\\"repo_url\\":\\"local/pre-push\\",\\"source_ref\\":\\"$local_sha\\",\\"target_ref\\":\\"$base\\",\\"diff_text\\":\\"$esc_diff\\",\\"selected_agents\\":[\\"code_analysis\\",\\"security\\"]}"

  submit=$(curl -s -m 20 -X POST "$GTO_BACKEND_URL/api/v1/analyse" \\
    -H "Authorization: Bearer $GTO_API_KEY" -H "Content-Type: application/json" \\
    -d "$payload")
  request_id=$(printf '%s' "$submit" | sed -n 's/.*"request_id":"\\([^"]*\\)".*/\\1/p')
  if [ -z "$request_id" ]; then
    echo "GTO: could not reach backend — skipping check."
    continue
  fi

  gate=""
  i=0
  while [ "$i" -lt 15 ]; do
    sleep 2
    status=$(curl -s -m 10 "$GTO_BACKEND_URL/api/v1/status/$request_id" -H "Authorization: Bearer $GTO_API_KEY")
    st=$(printf '%s' "$status" | sed -n 's/.*"status":"\\([^"]*\\)".*/\\1/p')
    if [ "$st" = "done" ]; then
      report=$(curl -s -m 10 "$GTO_BACKEND_URL/api/v1/report/$request_id" -H "Authorization: Bearer $GTO_API_KEY")
      gate=$(printf '%s' "$report" | sed -n 's/.*"gate":"\\([^"]*\\)".*/\\1/p')
      break
    fi
    i=$((i + 1))
  done

  if [ -z "$gate" ]; then
    echo "GTO: analysis taking too long — skipping check."
    continue
  fi

  echo "GTO: gate = $gate"
  if [ "$gate" = "BLOCK" ]; then
    if [ "$GTO_HOOK_MODE" = "block" ]; then
      echo "GTO: push refused — BLOCK-severity finding(s) present. See the results panel or backend report for detail." >&2
      exit 1
    else
      echo "GTO: warning — BLOCK-severity finding(s) present, but gto.gitHookMode is 'warn' — push proceeding." >&2
    fi
  fi
done

exit 0
`;
}

export async function installGitHookCommand(context: vscode.ExtensionContext): Promise<void> {
  const apiKey = await getApiKey(context.secrets);
  if (!apiKey) {
    vscode.window.showWarningMessage('GTO: run "GTO: Set API Key" first.');
    return;
  }
  let repo;
  try {
    repo = await resolveRepoRoot(true);
  } catch (e) {
    vscode.window.showErrorMessage(`GTO: ${e instanceof Error ? e.message : String(e)}`);
    return;
  }
  const backendUrl = getBackendUrl();
  const mode = getGitHookMode();
  const script = buildScript(backendUrl, apiKey, mode);
  const hookPath = `${repo.cwd}/.git/hooks/pre-push`;
  try {
    await fs.writeFile(hookPath, script, { mode: 0o755 });
    await fs.chmod(hookPath, 0o755);
  } catch (e) {
    vscode.window.showErrorMessage(`GTO: couldn't write the hook — ${e instanceof Error ? e.message : String(e)}`);
    return;
  }
  vscode.window.showInformationMessage(`GTO: pre-push hook installed (${mode} mode). Re-run this command after changing gto.gitHookMode or rotating your API key.`);
}

export async function uninstallGitHookCommand(): Promise<void> {
  let repo;
  try {
    repo = await resolveRepoRoot(true);
  } catch (e) {
    vscode.window.showErrorMessage(`GTO: ${e instanceof Error ? e.message : String(e)}`);
    return;
  }
  const hookPath = `${repo.cwd}/.git/hooks/pre-push`;
  let content: string;
  try {
    content = await fs.readFile(hookPath, 'utf8');
  } catch {
    vscode.window.showInformationMessage('GTO: no pre-push hook installed.');
    return;
  }
  if (!content.includes(MARKER)) {
    vscode.window.showWarningMessage('GTO: .git/hooks/pre-push exists but was not installed by GTO — leaving it untouched.');
    return;
  }
  await fs.unlink(hookPath);
  vscode.window.showInformationMessage('GTO: pre-push hook removed.');
}
