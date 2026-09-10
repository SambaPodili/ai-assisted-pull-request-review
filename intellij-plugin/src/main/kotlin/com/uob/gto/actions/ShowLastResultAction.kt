package com.uob.gto.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.ui.Messages
import com.uob.gto.LastReportHolder
import com.uob.gto.settings.GtoCredentials
import com.uob.gto.settings.GtoSettingsState
import com.uob.gto.toolwindow.GtoResultsToolWindowFactory

class ShowLastResultAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val holder = LastReportHolder.getInstance()
        val report = holder.report
        val repoRoot = holder.repoRoot
        if (report == null || repoRoot == null) {
            Messages.showInfoMessage(project, "No GTO review has run yet in this session.", "GTO Review")
            return
        }
        val backendUrl = GtoSettingsState.getInstance().state.backendUrl
        val apiKey = GtoCredentials.apiKey ?: ""
        GtoResultsToolWindowFactory.getOrShowPanel(project)?.showReport(report, repoRoot, backendUrl, apiKey, holder.suppressed, holder.newFingerprints)
    }
}
