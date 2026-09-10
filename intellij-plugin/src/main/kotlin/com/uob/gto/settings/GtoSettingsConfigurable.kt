package com.uob.gto.settings

import com.intellij.openapi.options.Configurable
import com.intellij.openapi.ui.DialogPanel
import com.intellij.ui.components.JBPasswordField
import com.intellij.ui.components.JBTextArea
import com.intellij.ui.dsl.builder.AlignX
import com.intellij.ui.dsl.builder.bindItem
import com.intellij.ui.dsl.builder.bindSelected
import com.intellij.ui.dsl.builder.bindText
import com.intellij.ui.dsl.builder.panel
import javax.swing.JComponent

/** Settings > Tools > GTO Review. Mirrors the union of vscode-extension's
 * gto.* configuration entries (package.json) plus its three SecretStorage-
 * backed prompts (Set API Key / Set Model API Key / Set Personal Git
 * Provider Token) on one page — IntelliJ's Settings dialog is the natural
 * single home for all of it. Single-value fields bind directly to
 * [GtoSettingsState] via the Kotlin UI DSL's `bindText`/`bindItem`/
 * `bindSelected` (flushed by `panel.apply()`/`panel.reset()`); the three
 * multi-line "one per line" list fields and the three password fields are
 * handled by hand — the former because the DSL binds a single scalar per
 * control, not a `List<String>` needing a split/join step; the latter
 * because they read/write PasswordSafe, not the persisted XML state, and
 * must never be pre-filled with the existing secret (same "leave blank to
 * keep it" pattern as vscode-extension/src/settings.ts's promptForApiKey). */
class GtoSettingsConfigurable : Configurable {

    private val state get() = GtoSettingsState.getInstance().state
    private val apiKeyField = JBPasswordField()
    private val modelApiKeyField = JBPasswordField()
    private val gitTokenField = JBPasswordField()
    private val connectedReposArea = JBTextArea(4, 40)
    private val connectedRepoPathsArea = JBTextArea(4, 40)
    private val functionalSpecPathsArea = JBTextArea(4, 40)
    private lateinit var panel: DialogPanel

    override fun getDisplayName(): String = "GTO Review"

    override fun createComponent(): JComponent {
        panel = panel {
            group("Backend") {
                row("Backend URL:") { textField().bindText(state::backendUrl).align(AlignX.FILL) }
                row("API key:") { cell(apiKeyField).align(AlignX.FILL) }
                    .rowComment("Stored in the OS keychain via PasswordSafe — never written to gto-review.xml. Leave blank to keep the current key.")
            }
            group("Git provider") {
                row("Provider:") {
                    comboBox(listOf("github", "bitbucket", "bitbucket_server")).bindItem(
                        { state.gitProvider }, { state.gitProvider = it ?: "github" }
                    )
                }.rowComment("Which provider \"Post to PR\"/\"Approve PR\" target.")
                row("Personal token:") { cell(gitTokenField).align(AlignX.FILL) }
                    .rowComment("Only used for \"Approve PR\" (your own identity) — \"Post to PR\" uses the shared bot credential. Leave blank to keep the current token.")
                row("Pre-push hook:") {
                    comboBox(listOf("warn", "block")).bindItem(
                        { state.gitHookMode }, { state.gitHookMode = it ?: "warn" }
                    )
                }.rowComment("warn = never blocks a push. block = refuses on a BLOCK-severity finding.")
            }
            group("Review") {
                row("Agent preset:") {
                    comboBox(listOf("fast", "standard", "thorough")).bindItem(
                        { state.agentPreset }, { state.agentPreset = it ?: "fast" }
                    )
                }
                row { checkBox("Auto-analyze on save").bindSelected(state::autoAnalyzeOnSave) }
            }
            group("Model") {
                row("Server model preset:") { textField().bindText(state::selectedModelPreset).align(AlignX.FILL) }
                    .rowComment("Primary path for a shared multi-user backend — leave blank to use the manual override below, or the backend's own default.")
                row("Manual provider:") {
                    comboBox(listOf("", "anthropic", "openai", "azure_openai", "ollama", "custom")).bindItem(
                        { state.modelProvider }, { state.modelProvider = it ?: "" }
                    )
                }
                row("Manual model name:") { textField().bindText(state::modelName).align(AlignX.FILL) }
                row("Manual base URL:") { textField().bindText(state::modelBaseUrl).align(AlignX.FILL) }
                row("Model API key:") { cell(modelApiKeyField).align(AlignX.FILL) }
                    .rowComment("Only consulted when Server model preset is blank. Leave blank to keep the current key.")
            }
            group("Review context (metadata sent with every analysis)") {
                row { label("Connected repos (one per line):") }
                row { cell(connectedReposArea).align(AlignX.FILL) }
                    .rowComment("Dependent repo names/slugs — blast-radius baseline for the dependency agent.")
                row { label("Connected repo local paths (one per line):") }
                row { cell(connectedRepoPathsArea).align(AlignX.FILL) }
                    .rowComment("Local checkouts of dependent repos — grepped for real call-sites of this diff's changed symbols.")
                row { label("Functional spec paths (one per line):") }
                row { cell(functionalSpecPathsArea).align(AlignX.FILL) }
                    .rowComment(".md/.txt/.docx/.pdf spec documents to trace the change against.")
            }
        }
        connectedReposArea.lineWrap = false
        connectedRepoPathsArea.lineWrap = false
        functionalSpecPathsArea.lineWrap = false
        resetListFields()
        return panel
    }

    override fun isModified(): Boolean =
        panel.isModified() || hasPendingCredential() || listFieldsModified()

    override fun apply() {
        panel.apply()
        applyCredentials()
        state.connectedRepos = splitLines(connectedReposArea.text)
        state.connectedRepoPaths = splitLines(connectedRepoPathsArea.text)
        state.functionalSpecPaths = splitLines(functionalSpecPathsArea.text)
    }

    override fun reset() {
        panel.reset()
        apiKeyField.text = ""
        modelApiKeyField.text = ""
        gitTokenField.text = ""
        resetListFields()
    }

    private fun resetListFields() {
        connectedReposArea.text = state.connectedRepos.joinToString("\n")
        connectedRepoPathsArea.text = state.connectedRepoPaths.joinToString("\n")
        functionalSpecPathsArea.text = state.functionalSpecPaths.joinToString("\n")
    }

    private fun listFieldsModified(): Boolean =
        splitLines(connectedReposArea.text) != state.connectedRepos ||
            splitLines(connectedRepoPathsArea.text) != state.connectedRepoPaths ||
            splitLines(functionalSpecPathsArea.text) != state.functionalSpecPaths

    private fun hasPendingCredential(): Boolean =
        apiKeyField.password.isNotEmpty() || modelApiKeyField.password.isNotEmpty() || gitTokenField.password.isNotEmpty()

    private fun applyCredentials() {
        if (apiKeyField.password.isNotEmpty()) {
            GtoCredentials.apiKey = String(apiKeyField.password)
            apiKeyField.text = ""
        }
        if (modelApiKeyField.password.isNotEmpty()) {
            GtoCredentials.modelApiKey = String(modelApiKeyField.password)
            modelApiKeyField.text = ""
        }
        if (gitTokenField.password.isNotEmpty()) {
            GtoCredentials.gitProviderToken = String(gitTokenField.password)
            gitTokenField.text = ""
        }
    }

    private fun splitLines(text: String): MutableList<String> =
        text.lines().map { it.trim() }.filter { it.isNotEmpty() }.toMutableList()
}
