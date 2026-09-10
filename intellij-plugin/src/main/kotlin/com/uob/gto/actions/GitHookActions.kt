package com.uob.gto.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.ui.Messages
import com.uob.gto.git.GitDiffProvider
import com.uob.gto.git.GitException
import com.uob.gto.settings.GtoCredentials
import com.uob.gto.settings.GtoSettingsState
import java.io.File
import java.nio.file.Files
import java.nio.file.attribute.PosixFilePermissions

/** "GTO: Install/Uninstall Git Hook" — the pre-push script itself is plain
 * bash, IDE-agnostic, so it's reused byte-for-byte from
 * vscode-extension/src/gitHook.ts::buildScript rather than reimplemented —
 * see that file for the full rationale (warn vs block mode, why the
 * key/URL are embedded as plain shell vars, the known worktree/submodule
 * limitation). Keep this MARKER and script text in sync with that file if
 * either changes. */
private const val MARKER = "# === GTO pre-push hook"

private fun buildScript(backendUrl: String, apiKey: String, mode: String): String = """
    #!/usr/bin/env bash
    $MARKER — managed by "GTO: Install Git Hook", remove via "GTO: Uninstall Git Hook" ===
    set -u
    GTO_BACKEND_URL="$backendUrl"
    GTO_API_KEY="$apiKey"
    GTO_HOOK_MODE="$mode"
    ZERO="0000000000000000000000000000000000000000"
    EMPTY_TREE="4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    json_escape() {
      awk '{ gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); lines[NR] = ${'$'}0 }
        END {
          for (i = 1; i <= NR; i++) {
            printf "%s", lines[i]
            if (i < NR) printf "\\n"
          }
        }'
    }

    while read -r local_ref local_sha remote_ref remote_sha; do
      [ "${'$'}local_sha" = "${'$'}ZERO" ] && continue
      base="${'$'}remote_sha"
      [ "${'$'}base" = "${'$'}ZERO" ] && base="${'$'}EMPTY_TREE"
      diff=${'$'}(git diff "${'$'}base" "${'$'}local_sha" -- 2>/dev/null | head -c 200000)
      [ -z "${'$'}diff" ] && continue
      echo "GTO: checking ${'$'}(basename "${'$'}remote_ref")…"
      esc_diff=${'$'}(printf '%s' "${'$'}diff" | json_escape)
      payload="{\"repo_url\":\"local/pre-push\",\"source_ref\":\"${'$'}local_sha\",\"target_ref\":\"${'$'}base\",\"diff_text\":\"${'$'}esc_diff\",\"selected_agents\":[\"code_analysis\",\"security\"]}"
      submit=${'$'}(curl -s -m 20 -X POST "${'$'}GTO_BACKEND_URL/api/v1/analyse" \
        -H "X-API-Key: ${'$'}GTO_API_KEY" -H "Content-Type: application/json" \
        -d "${'$'}payload")
      request_id=${'$'}(printf '%s' "${'$'}submit" | sed -n 's/.*"request_id":"\([^"]*\)".*/\1/p')
      if [ -z "${'$'}request_id" ]; then
        echo "GTO: could not reach backend — skipping check."
        continue
      fi
      gate=""
      i=0
      while [ "${'$'}i" -lt 15 ]; do
        sleep 2
        status=${'$'}(curl -s -m 10 "${'$'}GTO_BACKEND_URL/api/v1/status/${'$'}request_id" -H "X-API-Key: ${'$'}GTO_API_KEY")
        st=${'$'}(printf '%s' "${'$'}status" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
        if [ "${'$'}st" = "done" ]; then
          report=${'$'}(curl -s -m 10 "${'$'}GTO_BACKEND_URL/api/v1/report/${'$'}request_id" -H "X-API-Key: ${'$'}GTO_API_KEY")
          gate=${'$'}(printf '%s' "${'$'}report" | sed -n 's/.*"gate_decision":"\([^"]*\)".*/\1/p')
          break
        fi
        i=${'$'}((i + 1))
      done
      if [ -z "${'$'}gate" ]; then
        echo "GTO: analysis taking too long — skipping check."
        continue
      fi
      echo "GTO: gate = ${'$'}gate"
      if [ "${'$'}gate" = "BLOCK" ]; then
        if [ "${'$'}GTO_HOOK_MODE" = "block" ]; then
          echo "GTO: push refused — BLOCK-severity finding(s) present. See the GTO Review tool window or backend report for detail." >&2
          exit 1
        else
          echo "GTO: warning — BLOCK-severity finding(s) present, but the hook is in 'warn' mode — push proceeding." >&2
        fi
      fi
    done
    exit 0
""".trimIndent()

class InstallGitHookAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val basePath = project.basePath ?: return
        val apiKey = GtoCredentials.apiKey
        if (apiKey.isNullOrBlank()) {
            Messages.showWarningDialog(project, "Run \"GTO: Set API Key\" first.", "GTO Review")
            return
        }
        val repo = try {
            GitDiffProvider.resolveRepoRoot(basePath)
        } catch (ex: GitException) {
            Messages.showErrorDialog(project, ex.message ?: "git failed", "GTO Review")
            return
        }
        val settings = GtoSettingsState.getInstance().state
        val script = buildScript(settings.backendUrl, apiKey, settings.gitHookMode)
        val hookFile = File(repo.cwd, ".git/hooks/pre-push")
        try {
            hookFile.parentFile.mkdirs()
            hookFile.writeText(script)
            Files.setPosixFilePermissions(hookFile.toPath(), PosixFilePermissions.fromString("rwxr-xr-x"))
        } catch (ex: Exception) {
            Messages.showErrorDialog(project, "Couldn't write the hook — ${ex.message}", "GTO Review")
            return
        }
        Messages.showInfoMessage(
            project,
            "Pre-push hook installed (${settings.gitHookMode} mode). Re-run this command after changing the hook mode or rotating your API key.",
            "GTO Review",
        )
    }
}

class UninstallGitHookAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val basePath = project.basePath ?: return
        val repo = try {
            GitDiffProvider.resolveRepoRoot(basePath)
        } catch (ex: GitException) {
            Messages.showErrorDialog(project, ex.message ?: "git failed", "GTO Review")
            return
        }
        val hookFile = File(repo.cwd, ".git/hooks/pre-push")
        if (!hookFile.exists()) {
            Messages.showInfoMessage(project, "No pre-push hook installed.", "GTO Review")
            return
        }
        val content = try {
            hookFile.readText()
        } catch (ex: Exception) {
            Messages.showErrorDialog(project, "Couldn't read the hook — ${ex.message}", "GTO Review")
            return
        }
        if (!content.contains(MARKER)) {
            Messages.showWarningDialog(project, ".git/hooks/pre-push exists but was not installed by GTO — leaving it untouched.", "GTO Review")
            return
        }
        hookFile.delete()
        Messages.showInfoMessage(project, "Pre-push hook removed.", "GTO Review")
    }
}
