package com.uob.gto

import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.Presentation
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.StatusBar
import com.intellij.openapi.wm.StatusBarWidget
import com.intellij.openapi.wm.StatusBarWidgetFactory
import com.intellij.util.Consumer
import com.uob.gto.api.AnalysisReportView
import java.awt.event.MouseEvent

/** Status bar item — mirrors extension.ts's statusBarItem: shows a gate
 * icon + label, click-to-reanalyze, colored background on HOLD/BLOCK. */
class GtoStatusBarWidget(private val project: Project) : StatusBarWidget, StatusBarWidget.TextPresentation {

    private var statusBar: StatusBar? = null
    private var text = "▶ GTO Review"
    private var tooltip = "Analyze uncommitted changes with GTO Pull Request Review Framework"

    override fun ID(): String = "GtoStatusBarWidget"
    override fun install(statusBar: StatusBar) {
        this.statusBar = statusBar
    }
    override fun dispose() {
        statusBar = null
    }
    override fun getPresentation(): StatusBarWidget.WidgetPresentation = this

    override fun getText(): String = text
    override fun getTooltipText(): String = tooltip
    override fun getAlignment(): Float = java.awt.Component.LEFT_ALIGNMENT

    override fun getClickConsumer(): Consumer<MouseEvent> = Consumer {
        val action = ActionManager.getInstance().getAction("com.uob.gto.AnalyzeChanges") ?: return@Consumer
        val event = AnActionEvent.createFromDataContext("GtoStatusBar", Presentation(), com.intellij.openapi.actionSystem.DataContext.EMPTY_CONTEXT)
        action.actionPerformed(event)
    }

    fun reset() {
        text = "▶ GTO Review"
        tooltip = "Analyze uncommitted changes with GTO Pull Request Review Framework"
        statusBar?.updateWidget(ID())
    }

    fun update(report: AnalysisReportView) {
        val gate = report.gateDecision
        val icon = when (gate) { "APPROVE" -> "✓"; "HOLD" -> "⚠"; "BLOCK" -> "⛔"; else -> "?" }
        text = "$icon GTO: $gate"
        tooltip = "$gate · ${report.topIssuesCount} issue(s) · click to re-analyze"
        statusBar?.updateWidget(ID())
    }

    fun spinning() {
        text = "⏳ GTO Review…"
        tooltip = "Analysis in progress"
        statusBar?.updateWidget(ID())
    }

    companion object {
        private val widgets = mutableMapOf<Project, GtoStatusBarWidget>()
        fun getInstance(project: Project): GtoStatusBarWidget? = widgets[project]
        fun register(project: Project, widget: GtoStatusBarWidget) {
            widgets[project] = widget
        }
    }
}

class GtoStatusBarWidgetFactory : StatusBarWidgetFactory {
    override fun getId(): String = "GtoStatusBarWidget"
    override fun getDisplayName(): String = "GTO Review"
    override fun isAvailable(project: Project): Boolean = true
    override fun createWidget(project: Project): StatusBarWidget {
        val widget = GtoStatusBarWidget(project)
        GtoStatusBarWidget.register(project, widget)
        return widget
    }
    override fun disposeWidget(widget: StatusBarWidget) {
        widget.dispose()
    }
    override fun canBeEnabledOn(statusBar: StatusBar): Boolean = true
}
