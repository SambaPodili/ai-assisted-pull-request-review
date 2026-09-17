package com.uob.gto

import com.uob.gto.settings.GtoSettingsState
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import java.io.File
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration

/**
 * Kotlin port of vscode-extension/src/reviewContext.ts — gathers
 * metadata.* the backend agents can use: functional_docs, connected_repos,
 * existing_tests, external_references. See that file's header for why
 * existing_tests/external_references are actually CHEAPER here than in the
 * web app (a local checkout has filesystem access a browser doesn't).
 * Every gatherer is independently best-effort, matching the TS original.
 */
object ReviewContext {

    private const val MAX_DOCS = 10
    private const val MAX_DOC_CHARS = 40000
    private const val MAX_TEST_FILES = 300
    private const val MAX_REPOS_GREPPED = 5
    private const val MAX_FILES_PER_REPO = 4000
    private const val MAX_REFERENCES = 50
    private const val MAX_FILE_BYTES = 1_000_000L

    private val SKIP_DIRS = setOf(
        ".git", "node_modules", "dist", "out", "build", "target", ".gradle",
        "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "vendor", ".build", "DerivedData", "obj", "bin", ".idea", ".vscode", ".trunk",
    )

    private val TEST_NAME_PATTERNS = listOf(
        Regex("""^test_.*\.py$"""), Regex(""".*_test\.py$"""),
        Regex(""".*\.test\.(js|jsx|ts|tsx)$"""), Regex(""".*\.spec\.(js|jsx|ts|tsx)$"""),
        Regex(""".*Test\.java$"""), Regex(""".*Tests\.java$"""), Regex(""".*Test\.kt$"""), Regex(""".*Tests\.kt$"""),
        Regex(""".*_test\.go$"""),
        Regex(""".*Test\.cs$"""), Regex(""".*Tests\.cs$"""),
        Regex(""".*_spec\.rb$"""), Regex(""".*_test\.rb$"""),
        Regex(""".*Test\.php$"""),
        Regex(""".*Tests\.swift$"""),
        Regex(""".*_test\.dart$"""),
    )

    /** Rough, best-effort symbol-definition matchers — see reviewContext.ts's
     * SYMBOL_PATTERNS for the full rationale (good enough to seed a local
     * grep, not a real parser; only matched against ADDED diff lines). */
    private val SYMBOL_PATTERNS = listOf(
        Regex("""^\+\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"""),
        Regex("""^\+\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"""),
        Regex("""^\+\s*def\s+([A-Za-z_]\w*)"""),
        Regex("""^\+\s*fun\s+([A-Za-z_]\w*)"""),
        Regex("""^\+\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"""),
        Regex("""^\+\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)"""),
        Regex("""^\+\s*(?:public|private|protected|internal|static|final|override|virtual|async)\b[\w<>\[\],\s]*\s+([A-Za-z_]\w*)\s*\("""),
    )

    private fun extractChangedSymbols(diffText: String): List<String> {
        val names = LinkedHashSet<String>()
        for (line in diffText.lineSequence()) {
            if (!line.startsWith("+") || line.startsWith("+++")) continue
            for (re in SYMBOL_PATTERNS) {
                val m = re.find(line)
                val name = m?.groupValues?.getOrNull(1)
                if (name != null && name.length > 2) {
                    names.add(name)
                    break
                }
            }
            if (names.size >= 40) break
        }
        return names.toList()
    }

    /** Existing test files already in the repo — sent as metadata.
     * existing_tests so test_coverage_agent doesn't flag a changed file as
     * untested when its test lives elsewhere in the repo. Only `.path` is
     * read server-side (matched by basename) — no content needs sending. */
    fun gatherExistingTests(repoRoot: String): List<String> {
        val root = File(repoRoot)
        val out = mutableListOf<String>()
        fun walk(dir: File) {
            if (out.size >= MAX_TEST_FILES) return
            val entries = dir.listFiles() ?: return
            for (entry in entries) {
                if (out.size >= MAX_TEST_FILES) return
                if (entry.name.startsWith(".") && entry.name !in SKIP_DIRS) continue
                if (entry.isDirectory) {
                    if (entry.name in SKIP_DIRS) continue
                    walk(entry)
                } else if (TEST_NAME_PATTERNS.any { it.matches(entry.name) }) {
                    out.add(root.toPath().relativize(entry.toPath()).toString().replace('\\', '/'))
                }
            }
        }
        return try {
            walk(root)
            out
        } catch (e: Exception) {
            emptyList() // best-effort — never block analysis over this
        }
    }

    /** Reviewer-declared dependent repos (names only) — sent as metadata.
     * connected_repos, the dependency agent's blast-radius baseline. */
    fun gatherConnectedRepos(): List<String> =
        GtoSettingsState.getInstance().state.connectedRepos.filter { it.isNotBlank() }

    data class Reference(val symbol: String, val filePath: String, val line: Int, val context: String, val repo: String)

    private fun walkForSymbols(repoLocalPath: File, repoLabel: String, symbols: List<String>): List<Reference> {
        val patterns = symbols.map { it to Regex("\\b${Regex.escape(it)}\\b") }
        val found = mutableListOf<Reference>()
        var filesLeft = MAX_FILES_PER_REPO

        fun walk(dir: File) {
            if (found.size >= MAX_REFERENCES || filesLeft <= 0) return
            val entries = dir.listFiles() ?: return
            for (entry in entries) {
                if (found.size >= MAX_REFERENCES || filesLeft <= 0) return
                if (entry.name.startsWith(".") && entry.name !in SKIP_DIRS) continue
                if (entry.isDirectory) {
                    if (entry.name in SKIP_DIRS) continue
                    walk(entry)
                    continue
                }
                if (!entry.isFile) continue
                filesLeft--
                if (entry.length() == 0L || entry.length() > MAX_FILE_BYTES) continue
                val text = try {
                    entry.readText()
                } catch (e: Exception) {
                    continue // binary or unreadable — skip rather than fail the whole walk
                }
                val lines = text.split("\n")
                for (i in lines.indices) {
                    for ((symbol, re) in patterns) {
                        if (re.containsMatchIn(lines[i])) {
                            found.add(Reference(
                                symbol = symbol,
                                filePath = repoLocalPath.toPath().relativize(entry.toPath()).toString().replace('\\', '/'),
                                line = i + 1,
                                context = lines[i].trim().take(200),
                                repo = repoLabel,
                            ))
                            if (found.size >= MAX_REFERENCES) return
                            break
                        }
                    }
                }
            }
        }
        walk(repoLocalPath)
        return found
    }

    /** Call-sites of this diff's changed symbols found in locally-checked-out
     * dependent repos — sent as metadata.external_references, same shape the
     * backend's own /xref search produces. Purely local — no git provider
     * credentials needed. */
    fun gatherExternalReferences(diffText: String): List<Reference> {
        val repoPaths = GtoSettingsState.getInstance().state.connectedRepoPaths
            .filter { it.isNotBlank() }.take(MAX_REPOS_GREPPED)
        if (repoPaths.isEmpty()) return emptyList()
        val symbols = extractChangedSymbols(diffText)
        if (symbols.isEmpty()) return emptyList()

        val out = mutableListOf<Reference>()
        for (repoPath in repoPaths) {
            if (out.size >= MAX_REFERENCES) break
            val dir = File(repoPath)
            if (!dir.isDirectory) continue // configured path doesn't exist on this machine — skip, don't fail the run
            val label = dir.name
            out.addAll(walkForSymbols(dir, label, symbols))
        }
        return out.take(MAX_REFERENCES)
    }

    /** Uploaded functional/requirement spec text — sent as metadata.
     * functional_docs. .md/.txt read directly; .docx/.pdf go through the
     * backend's own /docs/extract (same endpoint the web app's upload UI
     * calls) since parsing those formats client-side isn't worth a new
     * dependency here. */
    fun gatherFunctionalDocs(repoRoot: String, backendUrl: String, apiKey: String): List<Pair<String, String>> {
        val configured = GtoSettingsState.getInstance().state.functionalSpecPaths
            .filter { it.isNotBlank() }.take(MAX_DOCS)
        if (configured.isEmpty()) return emptyList()

        // HTTP/1.1 pinned: see ApiClient.kt's HttpClient for why — the JDK
        // default HTTP/2 preference sends an Upgrade: h2c preface on
        // cleartext POSTs that uvicorn's h11 parser rejects.
        val http by lazy {
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(15)).version(HttpClient.Version.HTTP_1_1).build()
        }
        val out = mutableListOf<Pair<String, String>>()
        for (p in configured) {
            val abs = if (File(p).isAbsolute) File(p) else File(repoRoot, p)
            val name = abs.name
            val ext = name.substringAfterLast('.', "").lowercase()
            try {
                if (ext == "md" || ext == "txt") {
                    out.add(name to abs.readText().take(MAX_DOC_CHARS))
                    continue
                }
                if (ext == "docx" || ext == "pdf") {
                    if (backendUrl.isBlank() || apiKey.isBlank()) continue
                    val bytes = abs.readBytes()
                    val req = HttpRequest.newBuilder()
                        .uri(URI.create("$backendUrl/api/v1/docs/extract?filename=${java.net.URLEncoder.encode(name, "UTF-8")}"))
                        .header("Content-Type", "application/octet-stream")
                        .header("X-API-Key", apiKey)
                        .timeout(Duration.ofSeconds(60))
                        .POST(HttpRequest.BodyPublishers.ofByteArray(bytes))
                        .build()
                    val resp = http.send(req, HttpResponse.BodyHandlers.ofString())
                    if (resp.statusCode() !in 200..299) continue
                    val text = Json.parseToJsonElement(resp.body()).jsonObject["text"]?.jsonPrimitive?.content
                    if (!text.isNullOrEmpty()) out.add(name to text.take(MAX_DOC_CHARS))
                    continue
                }
                // Unrecognized extension — best-effort raw read, same as .md/.txt.
                out.add(name to abs.readText().take(MAX_DOC_CHARS))
            } catch (e: Exception) {
                // Missing/unreadable file, or extraction failed — skip it,
                // don't fail the whole analysis over one bad spec path.
            }
        }
        return out
    }

    /** Everything above, combined into the metadata object ApiClient sends
     * with the analysis request. */
    fun gather(repoRoot: String, diffText: String, backendUrl: String, apiKey: String): JsonObject {
        val existingTests = gatherExistingTests(repoRoot)
        val externalReferences = gatherExternalReferences(diffText)
        val functionalDocs = gatherFunctionalDocs(repoRoot, backendUrl, apiKey)
        val connectedRepos = gatherConnectedRepos()

        return buildJsonObject {
            if (connectedRepos.isNotEmpty()) {
                put("connected_repos", buildJsonArray { connectedRepos.forEach { add(JsonPrimitive(it)) } })
            }
            if (existingTests.isNotEmpty()) {
                put("existing_tests", buildJsonArray {
                    existingTests.forEach { add(buildJsonObject { put("path", it) }) }
                })
            }
            if (externalReferences.isNotEmpty()) {
                put("external_references", buildJsonArray {
                    externalReferences.forEach { ref ->
                        add(buildJsonObject {
                            put("symbol", ref.symbol)
                            put("file_path", ref.filePath)
                            put("line", ref.line)
                            put("context", ref.context)
                            put("repo", ref.repo)
                        })
                    }
                })
            }
            if (functionalDocs.isNotEmpty()) {
                put("functional_docs", buildJsonArray {
                    functionalDocs.forEach { (name, text) -> add(buildJsonObject { put("name", name); put("text", text) }) }
                })
            }
        }
    }
}
