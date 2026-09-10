package com.uob.gto.git

import com.uob.gto.settings.GtoSettingsState
import java.io.File
import java.nio.file.Files
import java.util.concurrent.TimeUnit

class GitException(message: String) : Exception(message)

data class RepoRoot(val cwd: String, val branch: String, val repoUrl: String)

/** Git access via the `git` CLI directly — same choice as
 * vscode-extension/src/gitDiff.ts (see that file's header comment): fewer
 * moving parts, no dependency on a bundled VCS plugin's internal API. */
object GitDiffProvider {

    private fun run(cwd: String, vararg args: String): String {
        val process = ProcessBuilder(listOf("git") + args)
            .directory(File(cwd))
            .redirectErrorStream(false)
            .start()
        val out = process.inputStream.bufferedReader().readText()
        val err = process.errorStream.bufferedReader().readText()
        if (!process.waitFor(30, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            throw GitException("git ${args.joinToString(" ")} timed out")
        }
        if (process.exitValue() != 0) {
            throw GitException(err.ifBlank { "git ${args.joinToString(" ")} failed" })
        }
        return out
    }

    fun resolveRepoRoot(startDir: String): RepoRoot {
        val cwd = run(startDir, "rev-parse", "--show-toplevel").trim()
        val branch = try {
            run(cwd, "rev-parse", "--abbrev-ref", "HEAD").trim()
        } catch (e: GitException) {
            "HEAD"
        }
        val remoteUrl = try {
            run(cwd, "config", "--get", "remote.origin.url").trim()
        } catch (e: GitException) {
            ""
        }
        val folderName = File(cwd).name
        return RepoRoot(cwd, branch, normalizeRepoUrl(remoteUrl, folderName))
    }

    /** Derives a `namespace/repo`-shaped identifier from a git remote URL, or
     * a sensible local fallback when there's no remote — the backend only
     * stores this for display/logging. Kotlin port of gitDiff.ts's
     * normalizeRepoUrl — same SSH/scp-like and ssh:// URL conversions, same
     * local/<folder> fallback. This is the value AnalyzeOptions.repoUrl
     * actually sends; [ownerRepoFromUrl] below is a DIFFERENT, later step
     * (extracting just "org/repo" from an already-normalized URL, for a git
     * provider API call) — don't compose the two. */
    private fun normalizeRepoUrl(remoteUrl: String, folderName: String): String {
        val trimmed = remoteUrl.trim()
        if (trimmed.isEmpty()) return "local/$folderName"
        Regex("""^git@([^:]+):(.+?)(\.git)?$""").find(trimmed)?.let {
            return "https://${it.groupValues[1]}/${it.groupValues[2]}"
        }
        Regex("""^ssh://git@([^:/]+)(?::\d+)?/(.+?)(\.git)?$""").find(trimmed)?.let {
            return "https://${it.groupValues[1]}/${it.groupValues[2]}"
        }
        return trimmed.removeSuffix(".git")
    }

    /** Uncommitted changes: staged + unstaged + untracked, via a temp index
     * trick so untracked files show up too (`git diff --no-index` per file),
     * mirroring gitDiff.ts::getUncommittedDiff. Never touches the real index. */
    fun getUncommittedDiff(repoRoot: String, excludePatterns: List<String>): String {
        val staged = try { run(repoRoot, "diff", "--cached") } catch (e: GitException) { "" }
        val unstaged = try { run(repoRoot, "diff") } catch (e: GitException) { "" }
        val untrackedFiles = try {
            run(repoRoot, "ls-files", "--others", "--exclude-standard")
                .lines().map { it.trim() }.filter { it.isNotEmpty() }
        } catch (e: GitException) {
            emptyList()
        }
        val filtered = untrackedFiles.filterNot { isExcluded(it, excludePatterns) }
        val devNull = if (System.getProperty("os.name").lowercase().contains("win")) "NUL" else "/dev/null"
        val untrackedDiff = filtered.joinToString("\n") { rel ->
            try {
                run(repoRoot, "diff", "--no-index", "--", devNull, rel)
            } catch (e: GitException) {
                // `git diff --no-index` exits 1 (not 0) when there IS a
                // difference — that's the expected/only case here, so a
                // thrown GitException actually carries the real diff text
                // via stderr/stdout depending on git version; re-run capturing
                // both streams together to recover it reliably.
                runAllowNonZero(repoRoot, "diff", "--no-index", "--", devNull, rel)
            }
        }
        return listOf(staged, unstaged, untrackedDiff).filter { it.isNotBlank() }.joinToString("\n")
    }

    fun getBranchDiff(repoRoot: String, source: String, target: String): String =
        run(repoRoot, "diff", "$target...$source")

    fun listOtherBranches(repoRoot: String): List<String> =
        run(repoRoot, "branch", "--format=%(refname:short)")
            .lines().map { it.trim() }.filter { it.isNotEmpty() }

    /** `git diff --no-index` exits 1 on a real diff (not an error) — capture
     * output regardless of exit code, matching how gitDiff.ts treats this
     * specific command's exit code as meaningless. */
    private fun runAllowNonZero(cwd: String, vararg args: String): String {
        val process = ProcessBuilder(listOf("git") + args)
            .directory(File(cwd))
            .redirectErrorStream(true)
            .start()
        val out = process.inputStream.bufferedReader().readText()
        process.waitFor(30, TimeUnit.SECONDS)
        return out
    }

    /** .gitignore-flavored: a pattern with no '/' matches at any path depth —
     * same semantics as gtoExcludePatterns / vscode-extension's excludePatterns. */
    private fun isExcluded(relPath: String, patterns: List<String>): Boolean {
        val norm = relPath.replace('\\', '/')
        return patterns.any { pattern ->
            val glob = if (pattern.contains('/')) pattern else "**/$pattern"
            globMatch(glob, norm) || globMatch(glob, "/$norm")
        }
    }

    private fun globMatch(glob: String, path: String): Boolean {
        val regex = Regex(
            "^" + glob
                .replace(".", "\\.")
                .replace("**/", "(.*/)?")
                .replace("**", ".*")
                .replace("*", "[^/]*")
                .replace("?", "[^/]") + "$"
        )
        return regex.matches(path.removePrefix("/"))
    }
}

fun defaultExcludePatterns(): List<String> = GtoSettingsState.getInstance().state.excludePatterns

/** Best-effort owner/repo slug from a git remote URL — used for the
 * repo_url field sent to the backend (mirrors gitDiff.ts::ownerRepoFromUrl). */
fun ownerRepoFromUrl(url: String): String {
    val cleaned = url.trim().removeSuffix(".git")
    val sshMatch = Regex("git@[^:]+:(.+)$").find(cleaned)
    if (sshMatch != null) return sshMatch.groupValues[1]
    return try {
        val path = java.net.URI(cleaned).path.trim('/')
        if (path.isNotBlank()) path else cleaned
    } catch (e: Exception) {
        cleaned
    }
}

fun tempFileFor(content: String): File {
    val f = Files.createTempFile("gto-", ".diff").toFile()
    f.writeText(content)
    f.deleteOnExit()
    return f
}
