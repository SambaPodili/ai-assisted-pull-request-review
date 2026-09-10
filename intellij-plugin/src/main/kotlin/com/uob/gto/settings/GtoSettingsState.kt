package com.uob.gto.settings

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.util.xmlb.XmlSerializerUtil

/**
 * Non-secret settings — mirrors vscode-extension/src/settings.ts's gto.*
 * configuration keys one-to-one so the two clients stay conceptually
 * interchangeable for a team using both. Credentials (API key, model API
 * key, git provider token) are NOT here — see [GtoCredentials], which uses
 * IntelliJ's PasswordSafe (OS keychain-backed), the same separation
 * settings.ts draws with vscode.SecretStorage.
 */
@Service(Service.Level.APP)
@State(name = "GtoSettings", storages = [Storage("gto-review.xml")])
class GtoSettingsState : PersistentStateComponent<GtoSettingsState.State> {

    class State {
        var backendUrl: String = "http://localhost:8080"
        var gitProvider: String = "github" // github | bitbucket | bitbucket_server
        var gitHookMode: String = "warn"   // warn | block
        var agentPreset: String = "fast"   // fast | standard | thorough
        var autoAnalyzeOnSave: Boolean = false
        var excludePatterns: MutableList<String> = defaultExcludePatterns()

        // Model selection — same precedence as settings.ts: a non-blank
        // selectedModelPreset wins; the manual override fields below are only
        // consulted when it's blank.
        var selectedModelPreset: String = ""
        var modelProvider: String = "" // "" | anthropic | openai | azure_openai | ollama | custom
        var modelName: String = ""
        var modelBaseUrl: String = ""
        var modelApiVersion: String = ""

        // Review-context gathering (reviewContext.ts equivalent).
        var connectedRepos: MutableList<String> = mutableListOf()
        var connectedRepoPaths: MutableList<String> = mutableListOf()
        var functionalSpecPaths: MutableList<String> = mutableListOf()
    }

    private var state = State()

    override fun getState(): State = state
    override fun loadState(state: State) {
        XmlSerializerUtil.copyBean(state, this.state)
    }

    companion object {
        fun getInstance(): GtoSettingsState =
            com.intellij.openapi.application.ApplicationManager.getApplication().getService(GtoSettingsState::class.java)

        /** Same list as vscode-extension/package.json's gto.excludePatterns default —
         * keep both in sync if you add a new ecosystem's build/dependency output dir. */
        fun defaultExcludePatterns(): MutableList<String> = mutableListOf(
            ".vscode/**", ".vscode-test/**", ".claude/**", ".idea/**",
            ".gitignore", ".gto-ignore.json", ".gto.yaml", ".gitattributes",
            ".editorconfig", ".DS_Store", ".trunk/**",
            "**/node_modules/**", "**/dist/**", "**/out/**", "**/build/**",
            "*.lock", "*.iml", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            "**/target/**", "**/.gradle/**", "*.class",
            "**/obj/**", "**/__pycache__/**", "*.pyc", "**/.venv/**", "**/venv/**",
            "**/.pytest_cache/**", "**/.mypy_cache/**", "**/.ruff_cache/**",
            "**/*.egg-info/**", "**/vendor/**", "**/.build/**", "**/DerivedData/**",
        )
    }
}
