package com.uob.gto.toolwindow

import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.content.ContentFactory

class GtoResultsToolWindowFactory : ToolWindowFactory, DumbAware {
    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val panel = GtoResultsPanel(project)
        val content = ContentFactory.getInstance().createContent(panel, "", false)
        toolWindow.contentManager.addContent(content)
        toolWindow.project.putUserData(PANEL_KEY, panel)
    }

    companion object {
        val PANEL_KEY = com.intellij.openapi.util.Key.create<GtoResultsPanel>("gto.resultsPanel")

        fun getOrShowPanel(project: Project): GtoResultsPanel? {
            val toolWindow = com.intellij.openapi.wm.ToolWindowManager.getInstance(project).getToolWindow("GTO Review")
            toolWindow?.show()
            return project.getUserData(PANEL_KEY)
        }
    }
}
