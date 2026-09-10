package com.uob.gto

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File

/** Kotlin port of vscode-extension/src/codeActions.ts's applyCodeFix — same
 * staleness-checked logic (refuses rather than editing the wrong line if the
 * file changed since analysis), shared between the results panel's "Apply"
 * button and the Quick Fix lightbulb (see inspection/GtoFindingInspection.kt). */
object GtoCodeFixApplier {

    private val LINE_RE = Regex("""@@ line (\d+) @@""")

    /** 0-based line number a CodeFix targets, or null if its `diff` doesn't
     * carry the `@@ line N @@` marker fix_generator.py always emits. */
    fun parseFixLine(fix: JsonObject): Int? {
        val diff = fix["diff"]?.jsonPrimitive?.content ?: return null
        val m = LINE_RE.find(diff) ?: return null
        return m.groupValues[1].toIntOrNull()?.minus(1)
    }

    enum class Status { APPLIED, STALE, ERROR }

    fun apply(project: Project, repoRoot: String, fix: JsonObject): Status {
        val filePath = fix["file_path"]?.jsonPrimitive?.content
        val before = fix["before"]?.jsonPrimitive?.content
        val after = fix["after"]?.jsonPrimitive?.content
        val line = parseFixLine(fix)
        if (line == null || filePath.isNullOrEmpty() || before == null || after == null) return Status.ERROR

        return try {
            val vFile = LocalFileSystem.getInstance().refreshAndFindFileByPath(File(repoRoot, filePath).path) ?: return Status.ERROR
            val document = FileDocumentManager.getInstance().getDocument(vFile) ?: return Status.ERROR
            if (line >= document.lineCount) return Status.STALE
            val lineStart = document.getLineStartOffset(line)
            val lineEnd = document.getLineEndOffset(line)
            if (document.getText(com.intellij.openapi.util.TextRange(lineStart, lineEnd)) != before) return Status.STALE

            var result = Status.APPLIED
            ApplicationManager.getApplication().invokeAndWait {
                WriteCommandAction.runWriteCommandAction(project, "Apply GTO Fix", null, {
                    try {
                        document.replaceString(lineStart, lineEnd, after)
                    } catch (e: Exception) {
                        result = Status.ERROR
                    }
                })
            }
            result
        } catch (e: Exception) {
            Status.ERROR
        }
    }
}
