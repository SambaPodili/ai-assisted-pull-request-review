package com.uob.gto.api

import kotlinx.coroutines.delay
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration

/**
 * Thin client for the backend's existing REST contract — mirrors
 * vscode-extension/src/apiClient.ts exactly (same endpoints, same request
 * shape), so a review submitted from IntelliJ is indistinguishable
 * server-side from one submitted through VS Code or the web app.
 *
 * Deliberately does NOT deserialize AnalysisReport into a Kotlin data class:
 * the report is rendered entirely by the shared JS bundle
 * (shared/report-view, loaded into a JCEF browser — see toolwindow/), which
 * consumes the raw JSON directly via JSON.parse in the browser. Kotlin only
 * needs to read a handful of top-level fields (gate_decision, request_id) —
 * see [AnalysisReportView] — not the whole ~40-field contract.
 */
class ApiException(message: String, val status: Int = 0) : Exception(message)

data class AnalyzeOptions(
    val backendUrl: String,
    val apiKey: String,
    val repoUrl: String,
    val sourceRef: String,
    val targetRef: String = "HEAD",
    val diffText: String,
    val selectedAgents: List<String>?,
    val userInstructions: String = "",
    val pathReviewConfig: JsonObject? = null,
    val modelOverride: JsonObject? = null,
    val metadata: JsonObject? = null,
)

data class SubmitResponse(val requestId: String, val status: String, val message: String?)
data class StatusResponse(val requestId: String, val status: String, val queuePosition: Int?, val queueTotal: Int?)

/** Thin read-only view over the raw report JSON for the handful of fields
 * Kotlin-side logic (notifications, inspections, "Show Last Result") needs
 * to inspect without round-tripping through a full data class. */
class AnalysisReportView(val raw: JsonObject) {
    val requestId: String get() = raw["request_id"]?.jsonPrimitive?.content ?: ""
    val gateDecision: String get() = raw["gate_decision"]?.jsonPrimitive?.content ?: ""
    val topIssuesCount: Int get() = raw["top_issues"]?.jsonArray?.size ?: 0
    val filesChanged: Int get() = raw["files_changed"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
    /** Raw JSON text — handed verbatim to the JCEF browser's JSON.parse(). */
    val json: String get() = raw.toString()
}

class ApiClient(private val backendUrl: String, private val apiKey: String) {

    // Pinned to HTTP/1.1: the JDK client's default HTTP/2 preference sends an
    // "Upgrade: h2c" preface on the first cleartext (http://) request, which
    // uvicorn's h11 parser can't handle and rejects with "Invalid HTTP
    // request received." — most visibly on POST /analyse (a body-bearing
    // request), while bodyless GETs could slip through.
    private val http: HttpClient = HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(15))
        .version(HttpClient.Version.HTTP_1_1)
        .build()

    private fun request(path: String, method: String = "GET", body: String? = null): HttpRequest.Builder {
        val builder = HttpRequest.newBuilder()
            .uri(URI.create("$backendUrl$path"))
            .header("Content-Type", "application/json")
            .header("X-API-Key", apiKey)
            .timeout(Duration.ofSeconds(60))
        return when (method) {
            "POST" -> builder.POST(HttpRequest.BodyPublishers.ofString(body ?: "{}"))
            else -> builder.GET()
        }
    }

    private fun checkOk(resp: HttpResponse<String>) {
        if (resp.statusCode() == 401) {
            throw ApiException("Unauthorized — check your API key (GTO Review settings).", 401)
        }
        if (resp.statusCode() !in 200..299) {
            val detail = try {
                Json.parseToJsonElement(resp.body()).jsonObject["detail"]?.jsonPrimitive?.content
            } catch (e: Exception) {
                null
            }
            throw ApiException(detail ?: resp.body().take(300).ifBlank { "HTTP ${resp.statusCode()}" }, resp.statusCode())
        }
    }

    fun submitAnalysis(opts: AnalyzeOptions): SubmitResponse {
        val body = buildJsonObject {
            put("repo_url", opts.repoUrl)
            put("source_ref", opts.sourceRef)
            put("target_ref", opts.targetRef)
            put("change_type", "branch_diff")
            put("diff_text", opts.diffText)
            if (opts.selectedAgents != null) {
                putJsonArray("selected_agents") { opts.selectedAgents.forEach { add(JsonPrimitive(it)) } }
            } else {
                put("selected_agents", JsonPrimitive(null as String?))
            }
            put("user_instructions", opts.userInstructions)
            put("path_review_config", opts.pathReviewConfig ?: JsonPrimitive(null as String?))
            if (opts.modelOverride != null) put("llm_config", opts.modelOverride)
            if (opts.metadata != null && opts.metadata.isNotEmpty()) put("metadata", opts.metadata)
        }
        val resp = http.send(request("/api/v1/analyse", "POST", body.toString()).build(), HttpResponse.BodyHandlers.ofString())
        checkOk(resp)
        val json = Json.parseToJsonElement(resp.body()).jsonObject
        return SubmitResponse(
            requestId = json["request_id"]?.jsonPrimitive?.content ?: "",
            status = json["status"]?.jsonPrimitive?.content ?: "unknown",
            message = json["message"]?.jsonPrimitive?.contentOrNull,
        )
    }

    fun getStatus(requestId: String): StatusResponse {
        val resp = http.send(request("/api/v1/status/$requestId").build(), HttpResponse.BodyHandlers.ofString())
        checkOk(resp)
        val json = Json.parseToJsonElement(resp.body()).jsonObject
        return StatusResponse(
            requestId = json["request_id"]?.jsonPrimitive?.content ?: requestId,
            status = json["status"]?.jsonPrimitive?.content ?: "unknown",
            queuePosition = json["queue_position"]?.jsonPrimitive?.content?.toIntOrNull(),
            queueTotal = json["queue_total"]?.jsonPrimitive?.content?.toIntOrNull(),
        )
    }

    fun getReport(requestId: String): AnalysisReportView {
        val resp = http.send(request("/api/v1/report/$requestId?fmt=full").build(), HttpResponse.BodyHandlers.ofString())
        checkOk(resp)
        return AnalysisReportView(Json.parseToJsonElement(resp.body()).jsonObject)
    }

    /** "Explain this finding" — a fixed, never-user-typed question, answered
     * by the same guardrailed Q&A engine used for PR chat replies.
     * [modelOverride], when given, is the same llm_config shape AnalyzeOptions
     * sends — lets Explain reuse whichever provider/model the analysis itself
     * used instead of the backend's global default model. */
    fun explainFinding(
        requestId: String, agent: String, category: String?, filePath: String?, title: String?,
        modelOverride: JsonObject? = null,
    ): String {
        val body = buildJsonObject {
            put("agent", agent)
            if (category != null) put("category", category)
            if (filePath != null) put("file_path", filePath)
            if (title != null) put("title", title)
            if (modelOverride != null) put("llm_config", modelOverride)
        }
        val resp = http.send(request("/api/v1/report/$requestId/explain-finding", "POST", body.toString()).build(), HttpResponse.BodyHandlers.ofString())
        if (resp.statusCode() == 404) {
            val detail = try { Json.parseToJsonElement(resp.body()).jsonObject["detail"]?.jsonPrimitive?.content } catch (e: Exception) { null }
            if (detail != null && detail.trim().lowercase().matches(Regex("not found\\.?"))) {
                throw ApiException("This GTO backend is too old to support Explain — ask your admin to update it to the latest build.", 404)
            }
        }
        checkOk(resp)
        return Json.parseToJsonElement(resp.body()).jsonObject["answer"]?.jsonPrimitive?.content ?: ""
    }

    /** Records a reviewer verdict on one finding — the same feedback loop the
     * web app's ResultsView already exposes. Aggregated over ≥3
     * false_positive verdicts for the same (agent, category, repo),
     * governance/suppression.py auto-suppresses that pattern on future runs. */
    fun submitFindingFeedback(requestId: String, agent: String, category: String?, filePath: String?, verdict: String, note: String? = null) {
        val body = buildJsonObject {
            put("agent", agent)
            if (category != null) put("category", category)
            if (filePath != null) put("file_path", filePath)
            put("verdict", verdict)
            if (note != null) put("note", note)
        }
        val resp = http.send(request("/api/v1/report/$requestId/feedback", "POST", body.toString()).build(), HttpResponse.BodyHandlers.ofString())
        checkOk(resp)
    }

    data class SimilarPR(
        val requestId: String, val repo: String, val prTitle: String, val prNumber: Int,
        val sourceRef: String, val riskScore: Int, val gate: String, val similarity: Double,
        val sharedFiles: List<String>, val elapsed: String,
    )

    /** Best-effort — a nice-to-have context panel, never worth surfacing an
     * error for. Returns empty on any failure, same as apiClient.ts. */
    fun fetchSimilarPRs(requestId: String, topK: Int = 5): List<SimilarPR> {
        return try {
            val resp = http.send(request("/api/v1/insights/similar/$requestId?top_k=$topK").build(), HttpResponse.BodyHandlers.ofString())
            if (resp.statusCode() !in 200..299) return emptyList()
            val body = Json.parseToJsonElement(resp.body()).jsonObject
            val arr = body["similar"]?.jsonArray ?: return emptyList()
            arr.map {
                val o = it.jsonObject
                SimilarPR(
                    requestId = o["request_id"]?.jsonPrimitive?.content ?: "",
                    repo = o["repo"]?.jsonPrimitive?.content ?: "",
                    prTitle = o["pr_title"]?.jsonPrimitive?.content ?: "",
                    prNumber = o["pr_number"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                    sourceRef = o["source_ref"]?.jsonPrimitive?.content ?: "",
                    riskScore = o["risk_score"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                    gate = o["gate"]?.jsonPrimitive?.content ?: "",
                    similarity = o["similarity"]?.jsonPrimitive?.content?.toDoubleOrNull() ?: 0.0,
                    sharedFiles = o["shared_files"]?.jsonArray?.map { f -> f.jsonPrimitive.content } ?: emptyList(),
                    elapsed = o["elapsed"]?.jsonPrimitive?.content ?: "",
                )
            }
        } catch (e: Exception) {
            emptyList()
        }
    }

    data class PostToPrResult(val ok: Boolean, val commentsPosted: Int, val filesCommented: Int)

    /** Posts findings as PR comments — reuses the shared bot credential
     * server-side, no personal token needed (unlike [approvePr]). */
    fun postFindingsToPr(requestId: String, provider: String, repoSlug: String, prId: String): PostToPrResult {
        val body = buildJsonObject {
            put("provider", provider)
            put("repo_slug", repoSlug)
            put("pr_id", prId)
            put("inline", true)
        }
        val resp = http.send(request("/api/v1/report/$requestId/comment-pr", "POST", body.toString()).build(), HttpResponse.BodyHandlers.ofString())
        checkOk(resp)
        val o = Json.parseToJsonElement(resp.body()).jsonObject
        return PostToPrResult(
            ok = (o["ok"] as? kotlinx.serialization.json.JsonPrimitive)?.content?.toBoolean() ?: false,
            commentsPosted = o["comments_posted"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
            filesCommented = o["files_commented"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
        )
    }

    data class ApprovePrResult(val status: String, val ok: Boolean, val errors: List<String>)

    /** Reviewer sign-off — approval only, never merges. Requires the
     * reviewer's OWN token (no server-side fallback) so it shows as them,
     * not the shared bot. */
    fun approvePr(requestId: String, provider: String, token: String, repoSlug: String, prId: String): ApprovePrResult {
        val body = buildJsonObject {
            put("provider", provider)
            put("token", token)
            put("repo_slug", repoSlug)
            put("pr_id", prId)
        }
        val resp = http.send(request("/api/v1/gate/$requestId/approve-pr", "POST", body.toString()).build(), HttpResponse.BodyHandlers.ofString())
        if (resp.statusCode() == 403) throw ApiException("Your API key does not have PR-approval permission.", 403)
        checkOk(resp)
        val o = Json.parseToJsonElement(resp.body()).jsonObject
        val prAction = o["pr_action"]?.jsonObject
        return ApprovePrResult(
            status = o["status"]?.jsonPrimitive?.content ?: "",
            ok = (prAction?.get("ok") as? kotlinx.serialization.json.JsonPrimitive)?.content?.toBoolean() ?: false,
            errors = prAction?.get("errors")?.jsonArray?.map { it.jsonPrimitive.content } ?: emptyList(),
        )
    }

    data class ModelPreset(val name: String, val label: String, val provider: String, val model: String)

    fun fetchModelPresets(): List<ModelPreset> {
        val resp = http.send(request("/api/v1/model-presets").build(), HttpResponse.BodyHandlers.ofString())
        checkOk(resp)
        val arr = Json.parseToJsonElement(resp.body()).jsonObject["presets"]?.jsonArray ?: return emptyList()
        return arr.map {
            val o = it.jsonObject
            ModelPreset(
                name = o["name"]?.jsonPrimitive?.content ?: "",
                label = o["label"]?.jsonPrimitive?.content ?: "",
                provider = o["provider"]?.jsonPrimitive?.content ?: "",
                model = o["model"]?.jsonPrimitive?.content ?: "",
            )
        }
    }

    /** Polls /status until done, then fetches the full report. `cancelled`
     * is checked between polls (mirrors apiClient.ts::runAnalysisToCompletion's
     * cancellation token, driven by IntelliJ's ProgressIndicator instead of
     * VS Code's CancellationToken). */
    suspend fun runAnalysisToCompletion(
        opts: AnalyzeOptions,
        isCancelled: () -> Boolean,
        onStatus: (StatusResponse) -> Unit = {},
        pollMs: Long = 2000,
    ): AnalysisReportView? {
        val submitted = submitAnalysis(opts)
        if (submitted.status == "no_diff") return null
        var status = getStatus(submitted.requestId)
        onStatus(status)
        while (status.status != "done" && !isCancelled()) {
            delay(pollMs)
            status = getStatus(submitted.requestId)
            onStatus(status)
        }
        if (isCancelled()) return null
        return getReport(submitted.requestId)
    }
}
