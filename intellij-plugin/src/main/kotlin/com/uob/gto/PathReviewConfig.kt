package com.uob.gto

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.yaml.snakeyaml.Yaml
import java.io.File

/**
 * Reads a repo's own .gto.yaml — per-path review rules. Kotlin port of
 * vscode-extension/src/pathReviewConfig.ts (same file, same schema, same
 * trust treatment: the backend re-scans any free text here through
 * governance.prompt_guard before it ever reaches an LLM prompt — this
 * client-side parse is never itself a trust boundary). Malformed or missing
 * .gto.yaml is never an error — same failure mode as GtoReportState's
 * .gto-ignore.json handling.
 */
object PathReviewConfig {

    private const val CONFIG_FILE = ".gto.yaml"
    private const val MAX_CONFIG_BYTES = 64_000

    data class Rule(val match: String, val agents: List<String>?, val userInstructions: String?, val skip: Boolean)
    data class Config(val version: Int, val paths: List<Rule>)

    @Suppress("UNCHECKED_CAST")
    private fun isValidRule(v: Any?): Boolean {
        val m = v as? Map<String, Any?> ?: return false
        return (m["match"] as? String)?.isNotEmpty() == true
    }

    /** Parses already-read .gto.yaml text — separated from [load] so it's
     * testable without touching the filesystem, same as parsePathReviewConfig
     * in the VS Code source. */
    @Suppress("UNCHECKED_CAST")
    fun parse(raw: String): Config? {
        if (raw.isEmpty() || raw.length > MAX_CONFIG_BYTES) return null
        return try {
            val data = Yaml().load<Any?>(raw) as? Map<String, Any?> ?: return null
            val rawPaths = data["paths"] as? List<Any?> ?: emptyList()
            val paths = rawPaths.filter { isValidRule(it) }.map {
                val m = it as Map<String, Any?>
                Rule(
                    match = m["match"] as String,
                    agents = (m["agents"] as? List<Any?>)?.map { a -> a.toString() },
                    userInstructions = m["user_instructions"] as? String,
                    skip = m["skip"] as? Boolean ?: false,
                )
            }
            if (paths.isEmpty()) null else Config(version = (data["version"] as? Number)?.toInt() ?: 1, paths = paths)
        } catch (e: Exception) {
            null // malformed YAML — proceed without path-scoped rules
        }
    }

    fun load(repoRoot: String): Config? {
        val file = File(repoRoot, CONFIG_FILE)
        if (!file.exists()) return null
        return try {
            parse(file.readText())
        } catch (e: Exception) {
            null
        }
    }

    /** Serializes to the JSON shape the backend's AnalysisRequest.
     * path_review_config field expects — same as what apiClient.ts sends
     * (a plain object, not string-keyed differently). */
    fun toJson(config: Config): JsonObject = buildJsonObject {
        put("version", config.version)
        put("paths", buildJsonArray {
            config.paths.forEach { rule ->
                add(buildJsonObject {
                    put("match", rule.match)
                    if (rule.agents != null) {
                        put("agents", buildJsonArray { rule.agents.forEach { add(JsonPrimitive(it)) } })
                    }
                    if (rule.userInstructions != null) put("user_instructions", rule.userInstructions)
                    put("skip", rule.skip)
                })
            }
        })
    }
}
