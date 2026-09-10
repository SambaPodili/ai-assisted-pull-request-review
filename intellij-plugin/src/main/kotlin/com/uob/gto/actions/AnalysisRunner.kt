package com.uob.gto.actions

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.progress.ProgressIndicator
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.progress.Task
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.InputValidator
import com.intellij.openapi.ui.Messages
import com.intellij.ui.popup.list.ListPopupImpl
import com.uob.gto.GtoPromptGuard
import com.uob.gto.GtoReportState
import com.uob.gto.GtoStatusBarWidget
import com.uob.gto.LastReportHolder
import com.uob.gto.PathReviewConfig
import com.uob.gto.ReviewContext
import com.uob.gto.api.AnalyzeOptions
import com.uob.gto.api.ApiClient
import com.uob.gto.api.ApiException
import com.uob.gto.git.GitDiffProvider
import com.uob.gto.settings.GtoCredentials
import com.uob.gto.settings.GtoSettingsState
import com.uob.gto.toolwindow.GtoResultsToolWindowFactory
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** Shared "resolve preset/priorities, submit a diff, poll, show the result"
 * flow — mirrors extension.ts::runAnalysis() end to end: preset/priorities
 * prompts, path-scoped config, review-context metadata, model-override
 * resolution, run-dedup, suppression filtering, delta badges, status bar,
 * and gate-based notifications. Both AnalyzeChangesAction and
 * AnalyzeBranchAction differ only in how they obtain diffText/sourceRef/
 * targetRef — see [runInteractive]. */
object AnalysisRunner {

    // Single global in-flight flag — mirrors extension.ts's module-level
    // `analysisInFlight`. Set true right before the Task.Backgroundable
    // starts and false in its onFinished() (runs on EDT after run()
    // completes regardless of success/cancellation/exception) — NOT in a
    // try/finally around the ProgressManager.run() call itself, since that
    // call returns immediately (the task runs asynchronously) and a
    // finally there would clear the flag before the analysis actually ends.
    private val inFlight = java.util.concurrent.atomic.AtomicBoolean(false)
    fun isInFlight(): Boolean = inFlight.get()

    val PRESET_META = listOf(
        Triple("fast", "Fast", "code_analysis + security only — fastest, catches the sharpest issues."),
        Triple("standard", "Standard", "Core review + risk gate (6 agents)."),
        Triple("thorough", "Thorough", "All ~22 agents, incl. remediation (unlocks Quick Fix) — slow for an editor loop."),
    )

    private val PRESET_AGENTS = mapOf(
        "fast" to listOf("code_analysis", "security"),
        "standard" to listOf("code_analysis", "security", "dependency", "test_coverage", "interface", "risk"),
        "thorough" to null, // null = no filtering, run everything
    )

    /** Interactive entry point — prompts for preset + priorities (skipped
     * for a non-interactive/automatic trigger, matching extension.ts's own
     * "asking would defeat the point of automatic" rationale), then runs. */
    fun runInteractive(project: Project, repoRoot: String, diffText: String, sourceRef: String, targetRef: String, interactive: Boolean = true) {
        val settings = GtoSettingsState.getInstance().state
        var preset = settings.agentPreset
        var userInstructions = ""
        if (interactive) {
            preset = promptForPreset(project, preset) ?: preset
            userInstructions = promptForPriorities(project) ?: return // Escape aborts the whole command here (unlike VS Code) — no partial state to preserve yet
        }
        run(project, repoRoot, diffText, sourceRef, targetRef, preset, userInstructions, interactive)
    }

    private fun promptForPreset(project: Project, current: String): String? {
        var result: String? = current
        ApplicationManager.getApplication().invokeAndWait {
            val items = PRESET_META.sortedBy { if (it.first == current) 0 else 1 }
            val step = object : com.intellij.openapi.ui.popup.util.BaseListPopupStep<Triple<String, String, String>>(
                "GTO — Analysis Depth", items
            ) {
                override fun getTextFor(value: Triple<String, String, String>): String =
                    "${value.second}${if (value.first == current) "  (configured default)" else ""} — ${value.third}"
                override fun onChosen(selectedValue: Triple<String, String, String>, finalChoice: Boolean): com.intellij.openapi.ui.popup.PopupStep<*>? {
                    result = selectedValue.first
                    return com.intellij.openapi.ui.popup.PopupStep.FINAL_CHOICE
                }
            }
            ListPopupImpl(project, step).showCenteredInCurrentWindow(project)
        }
        return result
    }

    /** Returns the trimmed priorities text, '' if left blank, or null if the
     * user cancelled the dialog outright. Live-validated against
     * [GtoPromptGuard] — advisory only, same as promptGuard.ts's role; the
     * backend's governance/prompt_guard.py is the real enforcement point. */
    private fun promptForPriorities(project: Project): String? {
        var result: String? = null
        ApplicationManager.getApplication().invokeAndWait {
            result = Messages.showInputDialog(
                project,
                "What should agents emphasize? e.g. \"focus on security in the payment module\". Leave empty to skip.",
                "GTO — Analysis Priorities (Optional)",
                null,
                "",
                object : InputValidator {
                    override fun checkInput(inputString: String?): Boolean {
                        val matches = GtoPromptGuard.scan(inputString ?: "")
                        return matches.isEmpty()
                    }
                    override fun canClose(inputString: String?): Boolean = checkInput(inputString)
                }
            )
        }
        return result?.trim()
    }

    private fun run(
        project: Project, repoRoot: String, diffText: String, sourceRef: String, targetRef: String,
        preset: String, userInstructions: String, interactive: Boolean,
    ) {
        val settings = GtoSettingsState.getInstance().state
        val apiKey = GtoCredentials.apiKey
        if (apiKey.isNullOrBlank()) {
            if (!interactive) return // never pop a blocking prompt from an automatic trigger
            Messages.showErrorDialog(project, "No GTO API key set — configure one in Settings > Tools > GTO Review.", "GTO Review")
            return
        }
        if (diffText.isBlank()) {
            if (interactive) Messages.showInfoMessage(project, "No changes to analyze.", "GTO Review")
            return
        }

        val backendUrl = settings.backendUrl
        val pathReviewConfig = PathReviewConfig.load(repoRoot)
        val modelOverride = resolveModelOverride(project, backendUrl, apiKey)
        // connected_repos/existing_tests/external_references/functional_docs —
        // gathered here (before the run-dedup key), same reasoning as
        // extension.ts: a metadata-only settings change between two runs of
        // the same diff must not be masked by the "nothing changed" cache.
        val metadata = ReviewContext.gather(repoRoot, diffText, backendUrl, apiKey)

        val runKey = buildJsonObject {
            put("diffText", diffText); put("sourceRef", sourceRef); put("targetRef", targetRef)
            put("preset", preset); put("userInstructions", userInstructions)
            if (pathReviewConfig != null) put("pathReviewConfig", PathReviewConfig.toJson(pathReviewConfig).toString())
            if (modelOverride != null) put("modelOverride", modelOverride.toString())
            put("metadata", metadata.toString())
        }.toString()

        val holder = LastReportHolder.getInstance()
        if (interactive && runKey == holder.runKey && holder.report != null) {
            notify(project, "Nothing changed since the last run — showing the previous result.", false)
            val panel = GtoResultsToolWindowFactory.getOrShowPanel(project)
            panel?.showReport(holder.report!!, holder.repoRoot!!, backendUrl, apiKey, holder.suppressed, holder.newFingerprints)
            return
        }

        val panel = GtoResultsToolWindowFactory.getOrShowPanel(project)
        panel?.showLoading("Submitting to GTO backend…")
        GtoStatusBarWidget.getInstance(project)?.spinning()
        inFlight.set(true)

        ProgressManager.getInstance().run(object : Task.Backgroundable(project, "GTO Review", true) {
            override fun onFinished() {
                inFlight.set(false)
            }
            override fun run(indicator: ProgressIndicator) {
                try {
                    val client = ApiClient(backendUrl, apiKey)
                    val opts = AnalyzeOptions(
                        backendUrl = backendUrl,
                        apiKey = apiKey,
                        // repoUrl is already normalized by resolveRepoRoot (see
                        // GitDiffProvider.normalizeRepoUrl) — do NOT run it
                        // through ownerRepoFromUrl here, that's a DIFFERENT,
                        // later step for git-provider API calls (see
                        // GtoResultsPanel's postToPr/approvePr), not for this
                        // submission field. Matches extension.ts::runAnalysis,
                        // which passes repo.repoUrl straight through.
                        repoUrl = GitDiffProvider.resolveRepoRoot(repoRoot).repoUrl,
                        sourceRef = sourceRef,
                        targetRef = targetRef,
                        diffText = diffText,
                        selectedAgents = PRESET_AGENTS[preset],
                        userInstructions = userInstructions,
                        pathReviewConfig = pathReviewConfig?.let { PathReviewConfig.toJson(it) },
                        modelOverride = modelOverride,
                        metadata = metadata,
                    )
                    val report = runBlocking {
                        client.runAnalysisToCompletion(
                            opts,
                            isCancelled = { indicator.isCanceled },
                            onStatus = { status -> indicator.text = statusText(status) },
                        )
                    }
                    if (indicator.isCanceled) {
                        panel?.showError("Cancelled.")
                        GtoStatusBarWidget.getInstance(project)?.reset()
                        return
                    }
                    if (report == null) {
                        panel?.showError("No analyzable diff (metadata-only change?).")
                        GtoStatusBarWidget.getInstance(project)?.reset()
                        return
                    }

                    // Suppressed findings (.gto-ignore.json, team-shared) are
                    // dropped before display — mirrors extension.ts filtering
                    // report.top_issues before ResultsPanel.showReport.
                    val suppressed = GtoReportState.loadSuppressed(repoRoot)
                    val suppressedFps = suppressed.map { it.fingerprint }.toSet()
                    val allIssues = report.raw["top_issues"]?.let { it as? kotlinx.serialization.json.JsonArray } ?: kotlinx.serialization.json.JsonArray(emptyList())
                    val visibleIssues = allIssues.filter { issue ->
                        val o = issue as? kotlinx.serialization.json.JsonObject ?: return@filter true
                        val fp = GtoReportState.fingerprint(
                            o["file_path"]?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content } ?: "",
                            o["line"]?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content?.toIntOrNull() } ?: 0,
                        )
                        fp !in suppressedFps
                    }
                    val filteredReport = com.uob.gto.api.AnalysisReportView(
                        kotlinx.serialization.json.JsonObject(report.raw.toMutableMap().apply {
                            put("top_issues", kotlinx.serialization.json.JsonArray(visibleIssues))
                        })
                    )

                    // Delta view: badge issues not present in the previous
                    // run's snapshot for this exact (repo, branch) as new.
                    val currentFps = visibleIssues.mapNotNull { issue ->
                        val o = issue as? kotlinx.serialization.json.JsonObject ?: return@mapNotNull null
                        GtoReportState.fingerprint(
                            o["file_path"]?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content } ?: "",
                            o["line"]?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content?.toIntOrNull() } ?: 0,
                        )
                    }
                    val lastSeen = GtoReportState.getLastSeenFingerprints(project, repoRoot, sourceRef)
                    val newFingerprints = if (lastSeen != null) currentFps.filterNot { it in lastSeen }.toSet() else emptySet()
                    GtoReportState.setLastSeenFingerprints(project, repoRoot, sourceRef, currentFps)

                    holder.set(filteredReport, repoRoot, suppressed, newFingerprints, runKey)
                    panel?.showReport(filteredReport, repoRoot, backendUrl, apiKey, suppressed, newFingerprints)
                    GtoStatusBarWidget.getInstance(project)?.update(filteredReport)

                    val gate = filteredReport.gateDecision
                    val msg = "GTO: $gate — ${filteredReport.topIssuesCount} issue(s)"
                    when {
                        gate == "BLOCK" -> notify(project, msg, true)
                        gate == "HOLD" -> notify(project, msg, true)
                        interactive -> notify(project, msg, false)
                    }
                } catch (e: ApiException) {
                    panel?.showError(e.message ?: "Analysis failed.")
                    GtoStatusBarWidget.getInstance(project)?.reset()
                    notify(project, "GTO analysis failed: ${e.message}", true)
                } catch (e: Exception) {
                    panel?.showError("Unexpected error: ${e.message}")
                    GtoStatusBarWidget.getInstance(project)?.reset()
                }
            }
        })
    }

    /** Preset (gto.modelPreset) takes priority when set — the primary path
     * for a shared backend. Falls back to the advanced free-text override
     * only when no preset is selected. A selected-but-unresolved preset
     * (removed server-side, fetch failed) does NOT silently fall through to
     * the free-text settings — better to warn and run unmodified. */
    private fun resolveModelOverride(project: Project, backendUrl: String, apiKey: String): JsonObject? {
        val settings = GtoSettingsState.getInstance().state
        val presetName = settings.selectedModelPreset.trim()
        if (presetName.isEmpty()) {
            val provider = settings.modelProvider.trim()
            if (provider.isEmpty()) return null
            return buildJsonObject {
                put("provider", provider)
                put("model", settings.modelName.trim())
                put("api_key", GtoCredentials.modelApiKey ?: "")
                put("base_url", settings.modelBaseUrl.trim())
                put("api_version", settings.modelApiVersion.trim())
            }
        }
        return try {
            val presets = ApiClient(backendUrl, apiKey).fetchModelPresets()
            val match = presets.find { it.name == presetName }
            if (match != null) {
                buildJsonObject {
                    put("provider", match.provider)
                    put("model", match.model)
                    put("api_key", ""); put("base_url", ""); put("api_version", "")
                }
            } else {
                notify(project, "Model preset \"$presetName\" not found on the backend — running with the default model.", true)
                null
            }
        } catch (e: Exception) {
            notify(project, "Could not reach the backend for model presets — running with the default model.", true)
            null
        }
    }

    private fun statusText(s: com.uob.gto.api.StatusResponse): String = when (s.status) {
        "queued" -> if (s.queuePosition != null) "Queued (${s.queuePosition}/${s.queueTotal ?: "?"})…" else "Queued…"
        "running" -> "Running agents…"
        else -> s.status
    }

    private fun notify(project: Project, message: String, isWarningOrError: Boolean) {
        ApplicationManager.getApplication().invokeLater {
            NotificationGroupManager.getInstance().getNotificationGroup("GTO Review")
                .createNotification(message, if (isWarningOrError) NotificationType.WARNING else NotificationType.INFORMATION)
                .notify(project)
        }
    }
}
