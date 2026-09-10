package com.uob.gto

import com.intellij.ide.util.PropertiesComponent
import com.intellij.openapi.project.Project
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.io.File

/**
 * Two kinds of local, cross-run state — mirrors
 * vscode-extension/src/reportState.ts exactly (same two stores, same
 * separation rationale):
 *  - Suppressions: a team decision, persisted to a git-trackable
 *    .gto-ignore.json at the repo root — shared the same way a
 *    .eslintignore would be. MUST use the same fingerprint formula as
 *    shared/report-view's `fingerprint()` and the web app's, since this
 *    file can be read by any of the three.
 *  - "Seen" snapshots: purely local delta-view bookkeeping, stored in
 *    IntelliJ's PropertiesComponent (the workspace-state equivalent of
 *    vscode.Memento) — never committed.
 */
object GtoReportState {

    private const val IGNORE_FILE = ".gto-ignore.json"
    private const val SEEN_KEY_PREFIX = "gto.seenIssues::"

    /** MUST match shared/report-view's fingerprint() exactly — see that
     * function's own comment for why (file,line) is deliberately not more
     * specific than this. */
    fun fingerprint(filePath: String, line: Int): String = "$filePath:${if (line > 0) line else 0}"

    data class SuppressedEntry(
        val fingerprint: String,
        val filePath: String,
        val line: Int,
        val title: String,
        val reason: String,
        val suppressedAt: String,
    )

    private fun ignoreFile(repoRoot: String) = File(repoRoot, IGNORE_FILE)

    fun loadSuppressed(repoRoot: String): List<SuppressedEntry> {
        val file = ignoreFile(repoRoot)
        if (!file.exists()) return emptyList()
        return try {
            val root = Json.parseToJsonElement(file.readText()).jsonObject
            val arr = root["suppressed"] as? JsonArray ?: return emptyList()
            arr.map {
                val o = it.jsonObject
                SuppressedEntry(
                    fingerprint = o["fingerprint"]?.jsonPrimitive?.content ?: "",
                    filePath = o["file_path"]?.jsonPrimitive?.content ?: "",
                    line = o["line"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                    title = o["title"]?.jsonPrimitive?.content ?: "",
                    reason = o["reason"]?.jsonPrimitive?.contentOrNull ?: "",
                    suppressedAt = o["suppressed_at"]?.jsonPrimitive?.contentOrNull ?: "",
                )
            }
        } catch (e: Exception) {
            emptyList() // missing or invalid JSON — treat as empty, same as reportState.ts
        }
    }

    private fun saveSuppressed(repoRoot: String, entries: List<SuppressedEntry>) {
        val json = buildJsonObject {
            put("suppressed", buildJsonArray {
                entries.forEach { e ->
                    add(buildJsonObject {
                        put("fingerprint", e.fingerprint)
                        put("file_path", e.filePath)
                        put("line", e.line)
                        put("title", e.title)
                        put("reason", e.reason)
                        put("suppressed_at", e.suppressedAt)
                    })
                }
            })
        }
        ignoreFile(repoRoot).writeText(
            Json { prettyPrint = true }.encodeToString(JsonObject.serializer(), json) + "\n"
        )
    }

    fun addSuppression(repoRoot: String, entry: SuppressedEntry) {
        val without = loadSuppressed(repoRoot).filterNot { it.fingerprint == entry.fingerprint }
        saveSuppressed(repoRoot, without + entry)
    }

    fun removeSuppression(repoRoot: String, fp: String) {
        saveSuppressed(repoRoot, loadSuppressed(repoRoot).filterNot { it.fingerprint == fp })
    }

    // ── Delta / "seen" snapshots ─────────────────────────────────────────────

    private fun seenKey(repoRoot: String, sourceRef: String) = "$SEEN_KEY_PREFIX$repoRoot::$sourceRef"

    /** Fingerprints seen as of the END of the previous run on this (repo,
     * branch) — null if this is the first run (no baseline to diff against). */
    fun getLastSeenFingerprints(project: Project, repoRoot: String, sourceRef: String): Set<String>? {
        val raw = PropertiesComponent.getInstance(project).getValue(seenKey(repoRoot, sourceRef)) ?: return null
        // JSON-array-encoded, NOT space-joined — a fingerprint embeds a file
        // path, which can itself contain spaces.
        return try {
            Json.parseToJsonElement(raw).jsonArray.map { it.jsonPrimitive.content }.toSet()
        } catch (e: Exception) {
            emptySet()
        }
    }

    fun setLastSeenFingerprints(project: Project, repoRoot: String, sourceRef: String, fingerprints: List<String>) {
        val arr = buildJsonArray { fingerprints.forEach { add(JsonPrimitive(it)) } }
        PropertiesComponent.getInstance(project).setValue(seenKey(repoRoot, sourceRef), arr.toString())
    }
}
