package com.uob.gto.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.ui.Messages
import com.uob.gto.git.GitDiffProvider
import com.uob.gto.git.GitException
import com.uob.gto.settings.GtoSettingsState

/** Uncommitted changes: staged + unstaged + untracked — mirrors
 * vscode-extension's "GTO: Analyze Changes" command. */
class AnalyzeChangesAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val basePath = project.basePath ?: return
        try {
            val repo = GitDiffProvider.resolveRepoRoot(basePath)
            val excludes = GtoSettingsState.getInstance().state.excludePatterns
            val diff = GitDiffProvider.getUncommittedDiff(repo.cwd, excludes)
            AnalysisRunner.runInteractive(project, repo.cwd, diff, repo.branch, "HEAD")
        } catch (ex: GitException) {
            Messages.showErrorDialog(project, "Not a git repository, or git failed: ${ex.message}", "GTO Review")
        }
    }
}
