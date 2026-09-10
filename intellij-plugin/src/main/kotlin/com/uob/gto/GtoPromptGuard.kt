package com.uob.gto

/**
 * Fourth hand-maintained copy of the prompt-injection/gate-manipulation rule
 * set — mirrors governance/prompt_guard.py (authoritative, enforced),
 * frontend/src/promptGuard.js, and vscode-extension/src/promptGuard.ts (this
 * is now a straight Kotlin port of that file — same ~15 rules). Same
 * duplication tradeoff those files' headers explain: changes rarely, a
 * shared-codegen pipeline would be overkill for four small copies. If you
 * edit the rules, edit all four.
 *
 * Used for the priorities-prompt input's live validation feedback —
 * advisory only. The backend's governance/prompt_guard.py is still the
 * authoritative enforcement point regardless of what this catches.
 */
object GtoPromptGuard {

    enum class Category { OVERRIDE, GATE_MANIPULATION, EXFILTRATION }
    data class GuardMatch(val category: Category, val phrase: String)

    private val RULES: List<Triple<Category, String, Regex>> = listOf(
        // ── override: system/developer impersonation, instruction override ──
        Triple(Category.OVERRIDE, "ignore previous instructions",
            Regex("""\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions?|rules?|prompt|guidance|directions?)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.OVERRIDE, "disregard the rules above",
            Regex("""\bdisregard\s+(the\s+)?(rules?|instructions?|policy|guidelines?)\s+(above|before)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.OVERRIDE, "you are now",
            Regex("""\byou\s+are\s+now\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.OVERRIDE, "system/developer role header",
            Regex("""^\s*(system|developer)\s*:""", setOf(RegexOption.IGNORE_CASE, RegexOption.MULTILINE))),
        Triple(Category.OVERRIDE, "new system prompt",
            Regex("""\bnew\s+system\s+prompt\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.OVERRIDE, "override your instructions",
            Regex("""\boverride\s+(your|the)\s+(instructions?|rules?|guidelines?)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.OVERRIDE, "act as a different/unrestricted assistant",
            Regex("""\bact\s+as\s+(if\s+you\s+are\s+)?(a\s+)?(different|new|unrestricted)\b""", RegexOption.IGNORE_CASE)),

        // ── gate_manipulation: forcing outcomes / suppressing categories ─────
        Triple(Category.GATE_MANIPULATION, "always approve",
            Regex("""\balways\s+(approve|pass)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.GATE_MANIPULATION, "never block/hold",
            Regex("""\bnever\s+(block|hold|fail)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.GATE_MANIPULATION, "mark everything as passing",
            Regex("""\bmark\s+(everything|all\s+findings?|this)\s+as\s+(low|passing|approved)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.GATE_MANIPULATION, "suppress security/secrets findings",
            Regex("""\b(ignore|skip|suppress|hide|don'?t\s+report|do\s+not\s+report)\s+(all\s+|any\s+)?(security|secrets?|vulnerabilit\w*|findings?)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.GATE_MANIPULATION, "skip the security agent",
            Regex("""\bskip\s+the\s+(security|secrets?)\s+agent\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.GATE_MANIPULATION, "force approve/gate",
            Regex("""\bforce\s+(approve|gate)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.GATE_MANIPULATION, "set gate decision directly",
            Regex("""\bset\s+gate\s*(decision)?\s*=?\s*(approve|hold|block)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.GATE_MANIPULATION, "downgrade severity",
            Regex("""\bdowngrade\s+(all\s+)?(severity|findings?)\b""", RegexOption.IGNORE_CASE)),

        // ── exfiltration: system prompt extraction ───────────────────────────
        Triple(Category.EXFILTRATION, "reveal your system prompt",
            Regex("""\b(repeat|print|reveal|show|output|dump)\s+(your\s+)?(system\s+prompt|instructions)\b""", RegexOption.IGNORE_CASE)),
        Triple(Category.EXFILTRATION, "what is your system prompt",
            Regex("""\bwhat\s+(is|are)\s+your\s+(system\s+prompt|instructions)\b""", RegexOption.IGNORE_CASE)),
    )

    fun scan(text: String): List<GuardMatch> {
        if (text.isBlank()) return emptyList()
        return RULES.filter { (_, _, re) -> re.containsMatchIn(text) }.map { (cat, phrase, _) -> GuardMatch(cat, phrase) }
    }
}
