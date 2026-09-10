package com.uob.gto.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.progress.Task
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.ui.popup.PopupStep
import com.intellij.openapi.ui.popup.util.BaseListPopupStep
import com.intellij.ui.popup.list.ListPopupImpl
import com.uob.gto.api.ApiClient
import com.uob.gto.settings.GtoCredentials
import com.uob.gto.settings.GtoSettingsState

/** Set API Key / Set Model API Key / Set Personal Git Provider Token — the
 * three vscode-extension SecretStorage prompts. The Settings page
 * ([GtoSettingsConfigurable]) covers the same three fields for users who
 * prefer that flow; these commands are the quick-access equivalent of VS
 * Code's command-palette actions for users who don't want to open Settings. */

class SetApiKeyAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT
    override fun actionPerformed(e: AnActionEvent) {
        val key = Messages.showPasswordDialog(e.project, "GTO backend API key:", "GTO: Set API Key", null) ?: return
        if (key.isBlank()) return
        GtoCredentials.apiKey = key
        Messages.showInfoMessage(e.project, "GTO API key saved.", "GTO Review")
    }
}

class SetModelApiKeyAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT
    override fun actionPerformed(e: AnActionEvent) {
        val key = Messages.showPasswordDialog(
            e.project,
            "API key for the model provider set in GTO Review settings. Leave blank to clear it (falls back to the backend's configured key).",
            "GTO: Set Model API Key",
            null,
        ) ?: return
        GtoCredentials.modelApiKey = key.ifBlank { null }
        Messages.showInfoMessage(e.project, if (key.isBlank()) "Model API key cleared." else "Model API key saved.", "GTO Review")
    }
}

class SetGitProviderTokenAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT
    override fun actionPerformed(e: AnActionEvent) {
        val token = Messages.showPasswordDialog(
            e.project,
            "Personal git provider token — used only for \"Approve PR\" (your own identity).",
            "GTO: Set Personal Git Provider Token",
            null,
        ) ?: return
        GtoCredentials.gitProviderToken = token.ifBlank { null }
        Messages.showInfoMessage(e.project, if (token.isBlank()) "Token cleared." else "Token saved.", "GTO Review")
    }
}

/** "GTO: Select Model" — mirrors extension.ts's selectModelCommand: fetches
 * the backend's server-configured presets and shows a quick-pick, current
 * selection listed first / marked. Persists to
 * GtoSettingsState.selectedModelPreset (the primary path for a shared
 * backend — AnalysisRunner.resolveModelOverride only falls back to the
 * advanced manual gto.modelProvider settings when this is blank). */
class SelectModelAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val apiKey = GtoCredentials.apiKey
        if (apiKey.isNullOrBlank()) {
            Messages.showWarningDialog(project, "Run \"GTO: Set API Key\" first.", "GTO Review")
            return
        }
        val backendUrl = GtoSettingsState.getInstance().state.backendUrl

        ProgressManager.getInstance().run(object : Task.Backgroundable(project, "Fetching GTO model presets…", false) {
            override fun run(indicator: com.intellij.openapi.progress.ProgressIndicator) {
                val presets = try {
                    ApiClient(backendUrl, apiKey).fetchModelPresets()
                } catch (ex: Exception) {
                    com.intellij.openapi.application.ApplicationManager.getApplication().invokeLater {
                        Messages.showErrorDialog(project, "Couldn't fetch model presets — ${ex.message}", "GTO Review")
                    }
                    return
                }
                if (presets.isEmpty()) {
                    com.intellij.openapi.application.ApplicationManager.getApplication().invokeLater {
                        Messages.showInfoMessage(
                            project,
                            "The backend has no model presets configured — it always uses its own default model. Ask your admin to add presets, or use the advanced settings for a personal backend.",
                            "GTO Review",
                        )
                    }
                    return
                }

                val current = GtoSettingsState.getInstance().state.selectedModelPreset
                data class Item(val name: String, val label: String)
                val items = listOf(Item("", "Default — use the backend's own configured model")) +
                    presets.map { Item(it.name, "${it.label} (${it.provider} · ${it.model})") }
                val sorted = items.sortedBy { if (it.name == current) 0 else 1 }

                com.intellij.openapi.application.ApplicationManager.getApplication().invokeLater {
                    val step = object : BaseListPopupStep<Item>("GTO — Select Model", sorted) {
                        override fun getTextFor(value: Item): String =
                            value.label + if (value.name == current) "  (current)" else ""
                        override fun onChosen(selectedValue: Item, finalChoice: Boolean): PopupStep<*>? {
                            GtoSettingsState.getInstance().state.selectedModelPreset = selectedValue.name
                            com.intellij.notification.NotificationGroupManager.getInstance().getNotificationGroup("GTO Review")
                                .createNotification(
                                    if (selectedValue.name.isEmpty()) "Using the backend default model." else "Model set to ${selectedValue.label}.",
                                    com.intellij.notification.NotificationType.INFORMATION,
                                ).notify(project)
                            return PopupStep.FINAL_CHOICE
                        }
                    }
                    ListPopupImpl(project, step).showCenteredInCurrentWindow(project)
                }
            }
        })
    }
}
