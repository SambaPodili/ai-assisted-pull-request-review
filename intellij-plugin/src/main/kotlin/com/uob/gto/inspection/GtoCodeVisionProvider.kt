package com.uob.gto.inspection

import com.intellij.codeInsight.codeVision.CodeVisionAnchorKind
import com.intellij.codeInsight.codeVision.CodeVisionEntry
import com.intellij.codeInsight.codeVision.CodeVisionProvider
import com.intellij.codeInsight.codeVision.CodeVisionRelativeOrdering
import com.intellij.codeInsight.codeVision.CodeVisionState
import com.intellij.codeInsight.codeVision.ui.model.ClickableTextCodeVisionEntry
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.ui.popup.JBPopupFactory
import com.intellij.openapi.util.TextRange
import com.uob.gto.LastReportHolder
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.awt.event.MouseEvent
import java.io.File

/**
 * IntelliJ's Code Vision is the platform's own equivalent of VS Code's
 * CodeLens — an always-visible inline "N problem(s)" annotation above a
 * line, distinct from (and more prominent than) an inspection's hover-only
 * gutter icon. This is a straight port of codeLenses.ts: same grouping
 * (multiple issues on one line become ONE entry), same label format
 * ("⚠ N GTO issue(s)"), same click behavior (single issue shows its detail
 * directly; multiple issues show a chooser first).
 */
class GtoCodeVisionProvider : CodeVisionProvider<Unit> {

    override val id: String = "GtoCodeVision"
    override val name: String = "GTO Review findings"
    override val relativeOrderings: List<CodeVisionRelativeOrdering> = emptyList()
    override val defaultAnchor: CodeVisionAnchorKind = CodeVisionAnchorKind.Top

    override fun precomputeOnUiThread(editor: Editor) {}

    override fun computeCodeVision(editor: Editor, uiData: Unit): CodeVisionState {
        editor.project ?: return CodeVisionState.Ready(emptyList())
        val report = LastReportHolder.getInstance().report ?: return CodeVisionState.Ready(emptyList())
        val repoRoot = LastReportHolder.getInstance().repoRoot ?: return CodeVisionState.Ready(emptyList())
        val vFile = com.intellij.openapi.fileEditor.FileDocumentManager.getInstance().getFile(editor.document)
            ?: return CodeVisionState.Ready(emptyList())
        val relPath = try {
            File(repoRoot).toPath().relativize(File(vFile.path).toPath()).toString().replace('\\', '/')
        } catch (e: Exception) {
            return CodeVisionState.Ready(emptyList())
        }

        val issues = report.raw["top_issues"]?.jsonArray ?: return CodeVisionState.Ready(emptyList())
        val byLine = LinkedHashMap<Int, MutableList<kotlinx.serialization.json.JsonObject>>()
        for (issue in issues) {
            val obj = issue.jsonObject
            val issueFile = obj["file_path"]?.jsonPrimitive?.content ?: continue
            if (issueFile.replace('\\', '/') != relPath) continue
            val line = (obj["line"]?.jsonPrimitive?.content?.toIntOrNull() ?: continue) - 1
            if (line < 0 || line >= editor.document.lineCount) continue
            byLine.getOrPut(line) { mutableListOf() }.add(obj)
        }

        val entries = byLine.map { (line, groupIssues) ->
            val n = groupIssues.size
            val range = TextRange(editor.document.getLineStartOffset(line), editor.document.getLineEndOffset(line))
            val entry: CodeVisionEntry = ClickableTextCodeVisionEntry(
                "⚠ $n GTO issue${if (n > 1) "s" else ""}",
                id,
                { _: MouseEvent?, _: Editor ->
                    showIssuePicker(groupIssues)
                },
            )
            range to entry
        }
        return CodeVisionState.Ready(entries)
    }

    private fun showIssuePicker(issues: List<kotlinx.serialization.json.JsonObject>) {
        if (issues.size == 1) {
            showDetail(issues[0])
            return
        }
        val step = object : com.intellij.openapi.ui.popup.util.BaseListPopupStep<kotlinx.serialization.json.JsonObject>(
            "GTO Issues on This Line", issues
        ) {
            override fun getTextFor(value: kotlinx.serialization.json.JsonObject): String {
                val title = value["title"]?.jsonPrimitive?.content ?: ""
                val severity = value["severity"]?.jsonPrimitive?.content ?: ""
                val confidence = value["confidence"]?.jsonPrimitive?.content ?: ""
                return "$title  ($severity · $confidence)"
            }
            override fun onChosen(selectedValue: kotlinx.serialization.json.JsonObject, finalChoice: Boolean): com.intellij.openapi.ui.popup.PopupStep<*>? {
                showDetail(selectedValue)
                return com.intellij.openapi.ui.popup.PopupStep.FINAL_CHOICE
            }
        }
        JBPopupFactory.getInstance().createListPopup(step).showInFocusCenter()
    }

    private fun showDetail(issue: kotlinx.serialization.json.JsonObject) {
        val title = issue["title"]?.jsonPrimitive?.content ?: ""
        val descriptions = issue["descriptions"]?.jsonArray?.joinToString(" ") { it.jsonPrimitive.content } ?: ""
        val detail = if (descriptions.isNotEmpty()) "$title — $descriptions" else title
        com.intellij.notification.NotificationGroupManager.getInstance().getNotificationGroup("GTO Review")
            .createNotification(detail, com.intellij.notification.NotificationType.INFORMATION)
            .notify(null)
    }
}
