package com.uob.gto

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.editor.Document
import com.intellij.openapi.fileEditor.FileDocumentManagerListener
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.uob.gto.actions.AnalysisRunner
import com.uob.gto.git.GitDiffProvider
import com.uob.gto.git.GitException
import com.uob.gto.settings.GtoCredentials
import com.uob.gto.settings.GtoSettingsState
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import javax.swing.Timer

/** Auto-analyze-on-save — mirrors extension.ts's onDidSave/autoAnalyze:
 * off by default (GtoSettingsState.autoAnalyzeOnSave), 1.5s-debounced,
 * non-interactive (no preset/priorities prompts), never overlaps a run
 * already in flight (AnalysisRunner.isInFlight() — a single global flag,
 * same as extension.ts's own module-level one), warns once (not on every
 * save) if no API key is set. */
class GtoStartupActivity : ProjectActivity {

    override suspend fun execute(project: Project) {
        val debounceTimer = AtomicReference<Timer?>(null)
        val warnedNoApiKey = AtomicBoolean(false)

        val connection = project.messageBus.connect()
        connection.subscribe(
            FileDocumentManagerListener.TOPIC,
            object : FileDocumentManagerListener {
                override fun beforeDocumentSaving(document: Document) {
                    if (!GtoSettingsState.getInstance().state.autoAnalyzeOnSave) return
                    debounceTimer.get()?.stop()
                    val timer = Timer(1500) {
                        debounceTimer.set(null)
                        autoAnalyze(project, warnedNoApiKey)
                    }
                    timer.isRepeats = false
                    debounceTimer.set(timer)
                    timer.start()
                }
            }
        )
    }

    private fun autoAnalyze(project: Project, warnedNoApiKey: AtomicBoolean) {
        if (AnalysisRunner.isInFlight()) return // a manual or previous auto run is already going — don't overlap
        val apiKey = GtoCredentials.apiKey
        if (apiKey.isNullOrBlank()) {
            if (warnedNoApiKey.compareAndSet(false, true)) {
                ApplicationManager.getApplication().invokeLater {
                    NotificationGroupManager.getInstance().getNotificationGroup("GTO Review")
                        .createNotification(
                            "GTO: auto-analyze-on-save is on but no API key is set. Run \"GTO: Set API Key\" to enable it.",
                            NotificationType.WARNING,
                        ).notify(project)
                }
            }
            return
        }
        val basePath = project.basePath ?: return
        try {
            val repo = GitDiffProvider.resolveRepoRoot(basePath)
            val excludes = GtoSettingsState.getInstance().state.excludePatterns
            val diff = GitDiffProvider.getUncommittedDiff(repo.cwd, excludes)
            AnalysisRunner.runInteractive(project, repo.cwd, diff, repo.branch, "HEAD", interactive = false)
        } catch (e: GitException) {
            // Not a git repo, or git failed — silent, matching extension.ts's
            // auto-save path (no error dialogs from an automatic trigger).
        }
    }
}
