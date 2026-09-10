package com.uob.gto.inspection

import com.intellij.codeInspection.InspectionManager
import com.intellij.codeInspection.LocalInspectionTool
import com.intellij.codeInspection.LocalQuickFix
import com.intellij.codeInspection.ProblemDescriptor
import com.intellij.codeInspection.ProblemHighlightType
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiFile
import com.uob.gto.GtoCodeFixApplier
import com.uob.gto.LastReportHolder
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File

/**
 * Surfaces the last GTO report's top_issues as native Problems-panel
 * warnings/squiggles on the affected line — the IntelliJ equivalent of
 * vscode-extension's diagnostics.ts — and, where a matching deterministic
 * CodeFix exists in remediation.code_fixes, a Quick Fix (💡) lightbulb —
 * the IntelliJ equivalent of codeActions.ts's GtoCodeActionProvider. Same
 * staleness check as that file and the results panel's "Apply" button
 * (GtoCodeFixApplier, one shared implementation for all three entry points).
 *
 * Language-agnostic (overrides [checkFile] directly rather than
 * [buildVisitor]) since a finding can land in any language the backend
 * reviews. Deliberately reads from [LastReportHolder] (already-fetched,
 * in-memory) rather than calling the backend — this inspection re-runs on
 * every document change IntelliJ's highlighting pass triggers, so it must
 * be free; it only ever reflects the most recent review, same as the VS
 * Code side's diagnostics (cleared implicitly once a new report replaces
 * the old one).
 *
 * The CodeLens-style "⚠ N GTO issue(s)" inline hint (codeLenses.ts) is
 * ported separately via IntelliJ's Code Vision — see
 * inspection/GtoCodeVisionProvider.kt — not duplicated here.
 */
class GtoFindingInspection : LocalInspectionTool() {

    override fun checkFile(file: PsiFile, manager: InspectionManager, isOnTheFly: Boolean): Array<ProblemDescriptor>? {
        val report = LastReportHolder.getInstance().report ?: return null
        val repoRoot = LastReportHolder.getInstance().repoRoot ?: return null
        val vFile = file.virtualFile ?: return null
        val relPath = try {
            File(repoRoot).toPath().relativize(File(vFile.path).toPath()).toString().replace('\\', '/')
        } catch (e: Exception) {
            return null
        }

        val issues = report.raw["top_issues"]?.jsonArray ?: return null
        val codeFixes = report.raw["remediation"]?.jsonObject?.get("code_fixes")?.jsonArray
            ?.map { it.jsonObject }.orEmpty()
        val document = com.intellij.openapi.fileEditor.FileDocumentManager.getInstance().getDocument(vFile) ?: return null
        val descriptors = mutableListOf<ProblemDescriptor>()

        for (issue in issues) {
            val obj = issue.jsonObject
            val issueFile = obj["file_path"]?.jsonPrimitive?.content ?: continue
            if (issueFile.replace('\\', '/') != relPath) continue
            val line = (obj["line"]?.jsonPrimitive?.content?.toIntOrNull() ?: continue) - 1
            if (line < 0 || line >= document.lineCount) continue
            val title = obj["title"]?.jsonPrimitive?.content ?: "GTO finding"
            val severity = obj["severity"]?.jsonPrimitive?.content ?: "medium"
            val agents = obj["agents"]?.jsonArray?.joinToString(", ") { it.jsonPrimitive.content } ?: ""

            val startOffset = document.getLineStartOffset(line)
            val endOffset = document.getLineEndOffset(line)
            if (startOffset >= endOffset) continue

            // A CodeFix targeting this exact (file, 0-based line) — matches
            // codeActions.ts's own line-based matching (not by issue title).
            val fix = codeFixes.find {
                it["file_path"]?.jsonPrimitive?.content == issueFile && GtoCodeFixApplier.parseFixLine(it) == line
            }
            val quickFixes = if (fix != null) arrayOf<LocalQuickFix>(GtoApplyFixQuickFix(fix, relPath)) else LocalQuickFix.EMPTY_ARRAY

            descriptors += manager.createProblemDescriptor(
                file,
                com.intellij.openapi.util.TextRange(startOffset, minOf(endOffset, startOffset + (endOffset - startOffset))),
                "GTO [$severity]: $title" + if (agents.isNotEmpty()) " ($agents)" else "",
                highlightTypeFor(severity),
                isOnTheFly,
                *quickFixes,
            )
        }
        return if (descriptors.isEmpty()) null else descriptors.toTypedArray()
    }

    private fun highlightTypeFor(severity: String): ProblemHighlightType = when (severity.lowercase()) {
        "critical", "high" -> ProblemHighlightType.GENERIC_ERROR
        "medium" -> ProblemHighlightType.WARNING
        else -> ProblemHighlightType.WEAK_WARNING
    }
}

/** Applies one deterministic CodeFix (agents/fix_generator.py — never an
 * LLM-proposed one; those are surfaced in the results panel only, same
 * restriction codeActions.ts's GtoCodeActionProvider applies via
 * `cached.fix.confidence === 'high'` for isPreferred). Refuses silently
 * (no-op) rather than editing the wrong line if the file changed since
 * analysis — see GtoCodeFixApplier.apply's staleness check. */
private class GtoApplyFixQuickFix(private val fix: JsonObject, private val relPath: String) : LocalQuickFix {
    override fun getFamilyName(): String = "GTO fix: ${fix["title"]?.jsonPrimitive?.content ?: "apply suggested fix"}"

    override fun applyFix(project: Project, descriptor: ProblemDescriptor) {
        val repoRoot = LastReportHolder.getInstance().repoRoot ?: return
        GtoCodeFixApplier.apply(project, repoRoot, fix)
    }
}
