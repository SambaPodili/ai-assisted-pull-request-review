package com.uob.gto.toolwindow

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.editor.ScrollType
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.ui.jcef.JBCefBrowser
import com.intellij.ui.jcef.JBCefBrowserBase
import com.intellij.ui.jcef.JBCefJSQuery
import com.uob.gto.GtoCodeFixApplier
import com.uob.gto.GtoReportState
import com.uob.gto.api.AnalysisReportView
import com.uob.gto.api.ApiClient
import com.uob.gto.api.ApiException
import com.uob.gto.git.ownerRepoFromUrl
import com.uob.gto.settings.GtoCredentials
import com.uob.gto.settings.GtoSettingsState
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.awt.BorderLayout
import java.io.File
import java.nio.file.Files
import java.time.Instant
import javax.swing.JPanel

/**
 * Hosts the shared report-view JS bundle (shared/report-view — see its
 * README) in a JCEF browser. This is the IntelliJ half of the "JCEF webview
 * reuse" architecture decision: renderReportBody()/renderReportStyles() are
 * the EXACT same code the VS Code extension calls (just via the compiled
 * browser bundle here instead of a source-level import).
 *
 * Interactivity uses ONE JBCefJSQuery dispatching on a `{command, ...}`
 * payload — the IntelliJ equivalent of vscode-extension/src/resultsPanel.ts's
 * `acquireVsCodeApi().postMessage` channel, mirroring every command name
 * that file's trailing <script> handles, and every server round-trip its
 * class-level handle* methods perform.
 */
class GtoResultsPanel(private val project: Project) : JPanel(BorderLayout()) {

    private val browser = JBCefBrowser()
    private val bridge = JBCefJSQuery.create(browser as JBCefBrowserBase)

    private var repoRoot: String? = null
    private var report: AnalysisReportView? = null
    private var backendUrl: String = ""
    private var apiKey: String = ""

    init {
        add(browser.component, BorderLayout.CENTER)
        showMessage("Run <b>GTO: Analyze Changes</b> from the Tools menu to review your uncommitted changes.")
        bridge.addHandler { payload ->
            dispatch(payload)
            null
        }
    }

    fun showLoading(text: String) {
        showMessage(text)
    }

    fun showError(message: String) {
        showMessage("<span style=\"color:#c94f4f;\">$message</span>")
    }

    fun showReport(
        report: AnalysisReportView, repoRoot: String, backendUrl: String, apiKey: String,
        suppressed: List<GtoReportState.SuppressedEntry>, newFingerprints: Set<String>,
    ) {
        this.report = report
        this.repoRoot = repoRoot
        this.backendUrl = backendUrl
        this.apiKey = apiKey
        val bundleUri = extractedBundleFile.toURI().toString()
        val suppressedJson = buildJsonObject {
            put("suppressed", kotlinx.serialization.json.buildJsonArray {
                suppressed.forEach { s ->
                    add(buildJsonObject {
                        put("fingerprint", s.fingerprint); put("file_path", s.filePath); put("line", s.line)
                        put("title", s.title); put("reason", s.reason); put("suppressed_at", s.suppressedAt)
                    })
                }
            })
            put("newFingerprints", kotlinx.serialization.json.buildJsonArray { newFingerprints.forEach { add(kotlinx.serialization.json.JsonPrimitive(it)) } })
        }
        val html = buildString {
            append("<!DOCTYPE html><html><head><meta charset=\"UTF-8\">")
            append("<script src=\"$bundleUri\"></script>")
            append("<style>")
            append(VsCodeThemeTokens.cssBlock())
            append("body{margin:0;} #root{padding:0;}")
            append("</style></head><body>")
            append("<div id=\"root\"></div>")
            append("<script>")
            append("const __report = ").append(report.json).append(";\n")
            append("const __state = ").append(suppressedJson.toString()).append(";\n")
            append(
                """
                function render() {
                  const gate = GtoReportView.resolveGate(__report.gate_decision);
                  const styleEl = document.createElement('style');
                  styleEl.textContent = GtoReportView.renderReportStyles(gate.color);
                  document.head.appendChild(styleEl);
                  document.getElementById('root').innerHTML = GtoReportView.renderReportBody(
                    __report,
                    { suppressed: __state.suppressed, newFingerprints: new Set(__state.newFingerprints) },
                    undefined
                  );
                  wireEvents();
                }
                function post(command, extra) {
                  const msg = Object.assign({ command }, extra || {});
                  ${bridge.inject("JSON.stringify(msg)")}
                }
                window.__gtoRespond = function(json) {
                  const msg = JSON.parse(json);
                  if (msg.command === 'fixResult') {
                    const btn = document.querySelector('.apply-btn[data-file="' + CSS.escape(msg.file) + '"][data-line0="' + msg.line0 + '"]');
                    if (btn) {
                      btn.disabled = msg.status === 'applied';
                      btn.dataset.status = msg.status;
                      btn.textContent = msg.status === 'applied' ? 'Applied ✓' : msg.status === 'stale' ? 'File changed — skipped' : 'Failed to apply';
                    }
                  } else if (msg.command === 'suppressDone') {
                    const card = document.querySelector('.issue[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
                    if (card) card.remove();
                  } else if (msg.command === 'unsuppressDone') {
                    const row = document.querySelector('.suppressed-row[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
                    if (row) row.remove();
                  } else if (msg.command === 'fpDone') {
                    const btn = document.querySelector('.fp-btn[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
                    if (btn) btn.textContent = 'Recorded ✓';
                  } else if (msg.command === 'explainDone') {
                    const btn = document.querySelector('.explain-btn[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
                    const slot = document.querySelector('.explain-slot[data-fingerprint="' + CSS.escape(msg.fingerprint) + '"]');
                    if (btn) { btn.disabled = false; btn.textContent = '❓ Explain'; }
                    if (slot) {
                      const esc = (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                      if (msg.error) slot.innerHTML = '<p class="dim" style="margin:6px 0;">Could not fetch an explanation — ' + esc(msg.error) + '</p>';
                      else if (msg.text) slot.innerHTML = '<details class="fix" open><summary>❓ Explanation</summary><p class="dim" style="margin:6px 0;white-space:pre-wrap;">' + esc(msg.text) + '</p></details>';
                    }
                  } else if (msg.command === 'applyAllDone') {
                    const btn = document.getElementById('applyAll');
                    if (btn) { btn.disabled = false; btn.textContent = 'Apply all'; }
                  } else if (msg.command === 'copyTextDone') {
                    const btn = document.querySelector('.copy-skeleton-btn[data-id="' + CSS.escape(msg.id) + '"]');
                    if (btn) { const prev = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = prev; }, 2000); }
                  } else if (msg.command === 'similarPrsResult') {
                    const el = document.getElementById('similarPrs');
                    if (!el || !Array.isArray(msg.items) || !msg.items.length) return;
                    const esc = (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                    el.innerHTML = '<details open><summary>Similar past PRs (' + msg.items.length + ')</summary>' +
                      msg.items.map((it) => {
                        const pct = Math.round((it.similarity || 0) * 100);
                        const label = it.pr_title || it.source_ref || it.repo;
                        const branch = it.pr_title && it.source_ref ? ' (' + esc(it.source_ref) + ')' : '';
                        const shared = (it.shared_files && it.shared_files.length)
                          ? ' · ' + it.shared_files.length + ' shared file' + (it.shared_files.length > 1 ? 's' : '') : '';
                        return '<div class="similar-row"><span class="sim-pct">' + pct + '%</span> ' +
                          '<span>' + esc(label) + branch + '</span> ' +
                          '<span class="dim">' + esc(it.gate) + ' · ' + esc(it.elapsed) + shared + '</span></div>';
                      }).join('') + '</details>';
                  }
                };
                function wireEvents() {
                  document.querySelectorAll('.issue').forEach(el => {
                    el.addEventListener('click', (e) => {
                      if (e.target.closest('.fix')) return;
                      post('openFinding', { file: el.dataset.file, line: el.dataset.line });
                    });
                  });
                  document.querySelectorAll('.file-item').forEach(el => {
                    el.addEventListener('click', () => post('openFinding', { file: el.dataset.file, line: '1' }));
                  });
                  document.querySelectorAll('.apply-btn').forEach(btn => {
                    btn.addEventListener('click', (e) => {
                      e.stopPropagation(); btn.disabled = true; btn.textContent = 'Applying…';
                      post('applyFix', { file: btn.dataset.file, line0: Number(btn.dataset.line0) });
                    });
                  });
                  const copyBtn = document.getElementById('copyMd');
                  if (copyBtn) copyBtn.addEventListener('click', () => post('copyMarkdown'));
                  const applyAllBtn = document.getElementById('applyAll');
                  if (applyAllBtn) applyAllBtn.addEventListener('click', () => {
                    applyAllBtn.disabled = true; applyAllBtn.textContent = 'Applying…'; post('applyAllFixes');
                  });
                  const postToPrBtn = document.getElementById('postToPr');
                  if (postToPrBtn) postToPrBtn.addEventListener('click', () => post('postToPr'));
                  const approvePrBtn = document.getElementById('approvePr');
                  if (approvePrBtn) approvePrBtn.addEventListener('click', () => post('approvePr'));
                  document.querySelectorAll('.copy-skeleton-btn').forEach(btn => {
                    btn.addEventListener('click', (e) => { e.stopPropagation(); post('copyText', { text: btn.dataset.code, id: btn.dataset.id }); });
                  });
                  document.querySelectorAll('.create-test-btn').forEach(btn => {
                    btn.addEventListener('click', (e) => {
                      e.stopPropagation();
                      post('createTestFile', { affectedFile: btn.dataset.affected, filename: btn.dataset.filename, code: btn.dataset.code });
                    });
                  });
                  document.querySelectorAll('.suppress-btn').forEach(btn => {
                    btn.addEventListener('click', (e) => {
                      e.stopPropagation();
                      post('suppressFinding', { fingerprint: btn.dataset.fingerprint, file: btn.dataset.file, line: Number(btn.dataset.line), title: btn.dataset.title });
                    });
                  });
                  document.querySelectorAll('.unsuppress-btn').forEach(btn => {
                    btn.addEventListener('click', () => post('unsuppressFinding', { fingerprint: btn.dataset.fingerprint }));
                  });
                  document.querySelectorAll('.fp-btn').forEach(btn => {
                    btn.addEventListener('click', (e) => {
                      e.stopPropagation(); btn.disabled = true; btn.textContent = 'Recording…';
                      post('markFalsePositive', { fingerprint: btn.dataset.fingerprint, agent: btn.dataset.agent, category: btn.dataset.category, file: btn.dataset.file });
                    });
                  });
                  document.querySelectorAll('.explain-btn').forEach(btn => {
                    btn.addEventListener('click', (e) => {
                      e.stopPropagation(); btn.disabled = true; btn.textContent = 'Asking…';
                      post('explainIssue', { fingerprint: btn.dataset.fingerprint, agent: btn.dataset.agent, category: btn.dataset.category, file: btn.dataset.file, title: btn.dataset.title });
                    });
                  });
                }
                render();
                """.trimIndent()
            )
            append("</script></body></html>")
        }
        browser.loadHTML(html)
        loadSimilarPrs(report.requestId)
    }

    /** Fire-and-forget — a nice-to-have context panel, never worth blocking
     * or erroring the main report render over. Silently does nothing on
     * failure or an empty result (ApiClient.fetchSimilarPRs already
     * swallows non-OK responses) — mirrors resultsPanel.ts's own
     * loadSimilarPrs. */
    private fun loadSimilarPrs(requestId: String) {
        val url = backendUrl
        val key = apiKey
        if (url.isBlank() || key.isBlank()) return
        Thread {
            val items = try {
                ApiClient(url, key).fetchSimilarPRs(requestId)
            } catch (e: Exception) {
                emptyList()
            }
            if (items.isEmpty()) return@Thread
            val json = kotlinx.serialization.json.buildJsonObject {
                put("command", "similarPrsResult")
                put("items", kotlinx.serialization.json.buildJsonArray {
                    items.forEach { it_ ->
                        add(buildJsonObject {
                            put("pr_title", it_.prTitle); put("source_ref", it_.sourceRef); put("repo", it_.repo)
                            put("similarity", it_.similarity); put("gate", it_.gate); put("elapsed", it_.elapsed)
                            put("shared_files", kotlinx.serialization.json.buildJsonArray { it_.sharedFiles.forEach { f -> add(kotlinx.serialization.json.JsonPrimitive(f)) } })
                        })
                    }
                })
            }
            respond(json)
        }.start()
    }

    private fun respond(json: JsonObject) {
        ApplicationManager.getApplication().invokeLater {
            val escaped = json.toString().replace("\\", "\\\\").replace("'", "\\'")
            browser.cefBrowser.executeJavaScript("window.__gtoRespond && window.__gtoRespond('$escaped');", browser.cefBrowser.url, 0)
        }
    }

    private fun dispatch(payloadJson: String) {
        val msg = try {
            Json.parseToJsonElement(payloadJson).jsonObject
        } catch (e: Exception) {
            return
        }
        when (msg["command"]?.jsonPrimitive?.content) {
            "openFinding" -> handleOpenFinding(msg)
            "copyMarkdown" -> handleCopyMarkdown()
            "applyFix" -> handleApplyFix(msg)
            "applyAllFixes" -> handleApplyAllFixes()
            "suppressFinding" -> handleSuppress(msg)
            "unsuppressFinding" -> handleUnsuppress(msg)
            "markFalsePositive" -> handleMarkFalsePositive(msg)
            "explainIssue" -> handleExplainIssue(msg)
            "postToPr" -> handlePostToPr()
            "approvePr" -> handleApprovePr()
            "createTestFile" -> handleCreateTestFile(msg)
            "copyText" -> handleCopyText(msg)
        }
    }

    private fun handleOpenFinding(msg: JsonObject) {
        val file = msg["file"]?.jsonPrimitive?.content ?: return
        val line = msg["line"]?.jsonPrimitive?.content?.toIntOrNull() ?: 1
        val root = repoRoot ?: return
        ApplicationManager.getApplication().invokeLater {
            val vFile = LocalFileSystem.getInstance().refreshAndFindFileByPath(File(root, file).path) ?: return@invokeLater
            val descriptor = OpenFileDescriptor(project, vFile, (line - 1).coerceAtLeast(0), 0)
            val editor = FileEditorManager.getInstance(project).openTextEditor(descriptor, true)
            editor?.scrollingModel?.scrollToCaret(ScrollType.CENTER)
        }
    }

    private fun handleCopyMarkdown() {
        browser.cefBrowser.executeJavaScript(
            "navigator.clipboard.writeText(GtoReportView.reportToMarkdown(__report));",
            browser.cefBrowser.url, 0
        )
    }

    private fun handleCopyText(msg: JsonObject) {
        val text = msg["text"]?.jsonPrimitive?.content ?: return
        val id = msg["id"]?.jsonPrimitive?.content ?: ""
        com.intellij.openapi.ide.CopyPasteManager.getInstance().setContents(java.awt.datatransfer.StringSelection(text))
        respond(buildJsonObject { put("command", "copyTextDone"); put("id", id) })
    }

    private fun handleApplyFix(msg: JsonObject) {
        val file = msg["file"]?.jsonPrimitive?.content ?: return
        val line0 = msg["line0"]?.jsonPrimitive?.content?.toIntOrNull() ?: return
        val root = repoRoot ?: return
        val fixes = report?.raw?.get("remediation")?.jsonObject?.get("code_fixes")?.jsonArray ?: kotlinx.serialization.json.JsonArray(emptyList())
        val fix = fixes.map { it.jsonObject }.find {
            it["file_path"]?.jsonPrimitive?.content == file && GtoCodeFixApplier.parseFixLine(it) == line0
        }
        val status = if (fix != null) GtoCodeFixApplier.apply(project, root, fix) else GtoCodeFixApplier.Status.ERROR
        respond(buildJsonObject { put("command", "fixResult"); put("file", file); put("line0", line0); put("status", status.name.lowercase()) })
        when (status) {
            GtoCodeFixApplier.Status.STALE -> notify("File changed since analysis — fix not applied to avoid editing the wrong line.", true)
            GtoCodeFixApplier.Status.ERROR -> notify("Couldn't apply that fix.", true)
            else -> {}
        }
    }

    private fun handleApplyAllFixes() {
        val root = repoRoot ?: return
        val fixes = report?.raw?.get("remediation")?.jsonObject?.get("code_fixes")?.jsonArray
            ?.map { it.jsonObject }?.filter { it["confidence"]?.jsonPrimitive?.content == "high" } ?: emptyList()
        var applied = 0
        var skipped = 0
        for (fix in fixes) {
            val line0 = GtoCodeFixApplier.parseFixLine(fix) ?: continue
            val filePath = fix["file_path"]?.jsonPrimitive?.content ?: continue
            val status = GtoCodeFixApplier.apply(project, root, fix)
            respond(buildJsonObject { put("command", "fixResult"); put("file", filePath); put("line0", line0); put("status", status.name.lowercase()) })
            if (status == GtoCodeFixApplier.Status.APPLIED) applied++ else skipped++
        }
        notify(if (skipped > 0) "Applied $applied/${fixes.size} — $skipped stale, skipped." else "Applied $applied fix(es).", false)
        respond(buildJsonObject { put("command", "applyAllDone") })
    }

    private fun handleSuppress(msg: JsonObject) {
        val fp = msg["fingerprint"]?.jsonPrimitive?.content ?: return
        val file = msg["file"]?.jsonPrimitive?.content ?: ""
        val line = msg["line"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
        val title = msg["title"]?.jsonPrimitive?.content ?: ""
        val root = repoRoot ?: return
        ApplicationManager.getApplication().invokeLater {
            val reason = Messages.showInputDialog(
                project,
                "Optional reason — saved to .gto-ignore.json so the team can see why this was suppressed.",
                "Suppress This Finding",
                null,
            ) ?: return@invokeLater // Escape/Cancel = cancelled, not "suppress with no reason"
            GtoReportState.addSuppression(root, GtoReportState.SuppressedEntry(fp, file, line, title, reason.trim(), Instant.now().toString()))
            respond(buildJsonObject { put("command", "suppressDone"); put("fingerprint", fp) })
        }
    }

    private fun handleUnsuppress(msg: JsonObject) {
        val fp = msg["fingerprint"]?.jsonPrimitive?.content ?: return
        val root = repoRoot ?: return
        GtoReportState.removeSuppression(root, fp)
        respond(buildJsonObject { put("command", "unsuppressDone"); put("fingerprint", fp) })
    }

    private fun handleMarkFalsePositive(msg: JsonObject) {
        val fp = msg["fingerprint"]?.jsonPrimitive?.content ?: return
        val agent = msg["agent"]?.jsonPrimitive?.content ?: ""
        val category = msg["category"]?.jsonPrimitive?.content
        val file = msg["file"]?.jsonPrimitive?.content
        val requestId = report?.requestId ?: return
        Thread {
            try {
                ApiClient(backendUrl, apiKey).submitFindingFeedback(requestId, agent, category, file, "false_positive")
                respond(buildJsonObject { put("command", "fpDone"); put("fingerprint", fp) })
            } catch (e: Exception) {
                notify(describeError(e), true)
            }
        }.start()
    }

    private fun handleExplainIssue(msg: JsonObject) {
        val fp = msg["fingerprint"]?.jsonPrimitive?.content ?: return
        val agent = msg["agent"]?.jsonPrimitive?.content ?: ""
        val category = msg["category"]?.jsonPrimitive?.content
        val file = msg["file"]?.jsonPrimitive?.content
        val title = msg["title"]?.jsonPrimitive?.content
        val requestId = report?.requestId ?: return
        Thread {
            try {
                val text = ApiClient(backendUrl, apiKey).explainFinding(requestId, agent, category, file, title)
                respond(buildJsonObject { put("command", "explainDone"); put("fingerprint", fp); put("text", text) })
            } catch (e: Exception) {
                respond(buildJsonObject { put("command", "explainDone"); put("fingerprint", fp); put("text", ""); put("error", describeError(e)) })
            }
        }.start()
    }

    /** Shared by "Post to PR" and "Approve PR" — neither GitDiffProvider's
     * RepoRoot nor the report carries a PR number, so both ask once per click. */
    private fun promptForPrId(): String? {
        var result: String? = null
        ApplicationManager.getApplication().invokeAndWait {
            result = Messages.showInputDialog(
                project, "Which PR is this for?", "Pull Request Number", null, "",
                object : com.intellij.openapi.ui.InputValidator {
                    override fun checkInput(inputString: String?): Boolean = inputString?.trim()?.matches(Regex("""\d+""")) == true
                    override fun canClose(inputString: String?): Boolean = checkInput(inputString)
                }
            )
        }
        return result?.trim()?.takeIf { it.isNotEmpty() }
    }

    private fun handlePostToPr() {
        val rep = report ?: return
        val prId = promptForPrId()
        if (prId == null) {
            respond(buildJsonObject { put("command", "postToPrDone") })
            return
        }
        val repoSlug = ownerRepoFromUrl(rep.raw["repo_url"]?.jsonPrimitive?.content ?: "")
        val provider = GtoSettingsState.getInstance().state.gitProvider
        Thread {
            try {
                val result = ApiClient(backendUrl, apiKey).postFindingsToPr(rep.requestId, provider, repoSlug, prId)
                notify(
                    if (result.ok) "Posted ${result.filesCommented} file comment(s) + summary to PR #$prId."
                    else "Couldn't post any comments — check the backend's shared bot credential and PR number.",
                    !result.ok,
                )
            } catch (e: Exception) {
                notify(describeError(e), true)
            }
            respond(buildJsonObject { put("command", "postToPrDone") })
        }.start()
    }

    private fun handleApprovePr() {
        val rep = report ?: return
        val token = GtoCredentials.gitProviderToken
        if (token.isNullOrBlank()) {
            notify("No personal token set — run \"GTO: Set Personal Git Provider Token\" first (the approval must show as you, not the shared bot).", true)
            respond(buildJsonObject { put("command", "approvePrDone") })
            return
        }
        val prId = promptForPrId()
        if (prId == null) {
            respond(buildJsonObject { put("command", "approvePrDone") })
            return
        }
        val repoSlug = ownerRepoFromUrl(rep.raw["repo_url"]?.jsonPrimitive?.content ?: "")
        val provider = GtoSettingsState.getInstance().state.gitProvider
        Thread {
            try {
                val result = ApiClient(backendUrl, apiKey).approvePr(rep.requestId, provider, token, repoSlug, prId)
                notify(
                    if (result.status == "approved") "PR #$prId approved."
                    else "Could not approve PR #$prId — ${result.errors.firstOrNull() ?: "unknown error"}",
                    result.status != "approved",
                )
            } catch (e: Exception) {
                notify(describeError(e), true)
            }
            respond(buildJsonObject { put("command", "approvePrDone") })
        }.start()
    }

    private fun handleCreateTestFile(msg: JsonObject) {
        val affectedFile = msg["affectedFile"]?.jsonPrimitive?.content ?: ""
        val filename = msg["filename"]?.jsonPrimitive?.content ?: return
        val code = msg["code"]?.jsonPrimitive?.content ?: ""
        val root = repoRoot ?: return
        ApplicationManager.getApplication().invokeLater {
            val suggestedDir = File(root, affectedFile).parentFile?.let {
                if (it.path.contains("${File.separator}src${File.separator}main${File.separator}")) {
                    File(it.path.replace("${File.separator}src${File.separator}main${File.separator}", "${File.separator}src${File.separator}test${File.separator}"))
                } else it
            } ?: File(root)
            val descriptor = com.intellij.openapi.fileChooser.FileChooserDescriptorFactory.createSingleFolderDescriptor()
            val chosenDir = com.intellij.openapi.fileChooser.FileChooser.chooseFile(descriptor, project, LocalFileSystem.getInstance().refreshAndFindFileByPath(suggestedDir.also { it.mkdirs() }.path))
                ?: return@invokeLater
            WriteCommandActionSave(project, File(chosenDir.path, filename), code)
        }
    }

    private fun WriteCommandActionSave(project: Project, target: File, content: String) {
        com.intellij.openapi.command.WriteCommandAction.runWriteCommandAction(project) {
            target.writeText(content)
            val vFile = LocalFileSystem.getInstance().refreshAndFindFileByIoFile(target) ?: return@runWriteCommandAction
            FileEditorManager.getInstance(project).openFile(vFile, true)
        }
    }

    private fun notify(message: String, isError: Boolean) {
        ApplicationManager.getApplication().invokeLater {
            val group = com.intellij.notification.NotificationGroupManager.getInstance().getNotificationGroup("GTO Review")
            group.createNotification(message, if (isError) com.intellij.notification.NotificationType.WARNING else com.intellij.notification.NotificationType.INFORMATION)
                .notify(project)
        }
    }

    private fun describeError(e: Exception): String = if (e is ApiException) e.message ?: "Unknown error" else (e.message ?: e.toString())

    private fun showMessage(html: String) {
        browser.loadHTML(
            "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"><style>${VsCodeThemeTokens.cssBlock()}" +
                "body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);padding:12px 16px;}" +
                "</style></head><body><p>$html</p></body></html>"
        )
    }

    companion object {
        /** report-view.iife.js ships as a plugin resource — JCEF needs a
         * real file:// URL, not a classpath resource stream, so it's
         * extracted once per IDE session to a temp file. */
        private val extractedBundleFile: File by lazy {
            val tmp = Files.createTempFile("gto-report-view-", ".js").toFile()
            tmp.deleteOnExit()
            GtoResultsPanel::class.java.getResourceAsStream("/webview/report-view.iife.js")?.use { input ->
                tmp.outputStream().use { output -> input.copyTo(output) }
            }
            tmp
        }
    }
}
