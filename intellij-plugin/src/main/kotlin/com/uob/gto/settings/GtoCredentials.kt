package com.uob.gto.settings

import com.intellij.credentialStore.CredentialAttributes
import com.intellij.credentialStore.Credentials
import com.intellij.credentialStore.generateServiceName
import com.intellij.ide.passwordSafe.PasswordSafe

/**
 * Credentials via IntelliJ's PasswordSafe (OS keychain on macOS/Windows,
 * libsecret on Linux) — the IntelliJ equivalent of vscode.SecretStorage,
 * used the same way settings.ts does: never written to the plain XML
 * settings file GtoSettingsState persists, so a shared/synced settings
 * profile can't leak a key.
 */
object GtoCredentials {
    private fun attrs(key: String) =
        CredentialAttributes(generateServiceName("GTO Review", key))

    private fun get(key: String): String? =
        PasswordSafe.instance.get(attrs(key))?.getPasswordAsString()

    private fun set(key: String, value: String?) {
        PasswordSafe.instance.set(attrs(key), if (value.isNullOrBlank()) null else Credentials(key, value))
    }

    var apiKey: String?
        get() = get("apiKey")
        set(value) = set("apiKey", value)

    var modelApiKey: String?
        get() = get("modelApiKey")
        set(value) = set("modelApiKey", value)

    /** Personal git provider token — Bitbucket/GitHub, used only for "Approve
     * PR" (the reviewer's own identity), same as settings.ts::getBitbucketToken.
     * "Post to PR" doesn't need this; it uses the shared bot credential
     * configured server-side. */
    var gitProviderToken: String?
        get() = get("gitProviderToken")
        set(value) = set("gitProviderToken", value)
}
