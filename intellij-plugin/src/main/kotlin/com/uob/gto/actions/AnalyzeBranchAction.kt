package com.uob.gto.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.ui.Messages
import com.uob.gto.git.GitDiffProvider
import com.uob.gto.git.GitException

/** Three-dot branch comparison — mirrors vscode-extension's
 * "GTO: Analyze Branch..." command. Prompts for the branch to compare
 * against the current one. */
class AnalyzeBranchAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val basePath = project.basePath ?: return
        try {
            val repo = GitDiffProvider.resolveRepoRoot(basePath)
            val branches = GitDiffProvider.listOtherBranches(repo.cwd).filter { it != repo.branch }
            if (branches.isEmpty()) {
                Messages.showInfoMessage(project, "No other local branches found.", "GTO Review")
                return
            }
            val target = Messages.showEditableChooseDialog(
                "Compare current branch (${repo.branch}) against:",
                "GTO: Analyze Branch",
                Messages.getQuestionIcon(),
                branches.toTypedArray(),
                branches.firstOrNull { it == "main" || it == "master" } ?: branches.first(),
                null,
            ) ?: return
            val diff = GitDiffProvider.getBranchDiff(repo.cwd, repo.branch, target)
            AnalysisRunner.runInteractive(project, repo.cwd, diff, repo.branch, target)
        } catch (ex: GitException) {
            Messages.showErrorDialog(project, "Not a git repository, or git failed: ${ex.message}", "GTO Review")
        }
    }
}
