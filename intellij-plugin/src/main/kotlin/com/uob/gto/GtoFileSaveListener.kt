package com.uob.gto

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.editor.Document
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.fileEditor.FileDocumentManagerListener
import com.intellij.openapi.project.Project
import com.intellij.openapi.project.ProjectLocator
import com.uob.gto.actions.AnalysisRunner
import com.uob.gto.git.GitDiffProvider
import com.uob.gto.git.GitException
import com.uob.gto.settings.GtoCredentials
import com.uob.gto.settings.GtoSettingsState
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import javax.swing.Timer

/**
 * Auto-analyze-on-save — mirrors extension.ts's onDidSave/autoAnalyze:
 * off by default (GtoSettingsState.autoAnalyzeOnSave), 1.5s-debounced,
 * non-interactive (no preset/priorities prompts), never overlaps a run
 * already in flight (AnalysisRunner.isInFlight()), warns once (not on
 * every save) if no API key is set.
 *
 * Registered via the `fileDocumentManagerListener` extension point
 * (application-level, declared in plugin.xml) rather than
 * `com.intellij.openapi.startup.ProjectActivity` + a per-project message-bus
 * subscription — ProjectActivity doesn't exist before 2022.3/2023.1, and
 * this plugin supports back to 2022.3 (see build.gradle.kts's
 * `ideaVersion.sinceBuild`). FileDocumentManagerListener.EP_NAME is present
 * in every version this plugin targets, old and new, unlike its
 * newer `.TOPIC` message-bus form. The tradeoff: this listener is
 * application-scoped, not project-scoped, so it resolves the owning
 * project per save via [ProjectLocator] instead of getting one handed to it.
 */
class GtoFileSaveListener : FileDocumentManagerListener {

    // One debounce timer / one "already warned" flag per open project — an
    // auto-save in one project shouldn't debounce-cancel a pending one in
    // another, and a warning shown for one shouldn't suppress it for another.
    private val debounceTimers = ConcurrentHashMap<Project, Timer>()
    private val warnedNoApiKey = ConcurrentHashMap<Project, AtomicBoolean>()

    override fun beforeDocumentSaving(document: Document) {
        if (!GtoSettingsState.getInstance().state.autoAnalyzeOnSave) return
        val file = FileDocumentManager.getInstance().getFile(document) ?: return
        val project = ProjectLocator.getInstance().guessProjectForFile(file) ?: return

        debounceTimers.remove(project)?.stop()
        val timer = Timer(1500) {
            debounceTimers.remove(project)
            autoAnalyze(project)
        }
        timer.isRepeats = false
        debounceTimers[project] = timer
        timer.start()
    }

    private fun autoAnalyze(project: Project) {
        if (AnalysisRunner.isInFlight()) return // a manual or previous auto run is already going — don't overlap
        val apiKey = GtoCredentials.apiKey
        if (apiKey.isNullOrBlank()) {
            val warned = warnedNoApiKey.getOrPut(project) { AtomicBoolean(false) }
            if (warned.compareAndSet(false, true)) {
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
