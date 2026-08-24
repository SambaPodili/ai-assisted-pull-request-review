// vscode-extension/src/promptGuard.ts
// -----------------------------------------------------------------------------
// Third hand-maintained copy of the prompt-injection/gate-manipulation rule
// set — mirrors governance/prompt_guard.py (authoritative, enforced) and
// frontend/src/promptGuard.js (the web app's real-time copy). Same
// duplication tradeoff as that file's header explains: ~15 rules, changes
// rarely, a shared-codegen pipeline would be overkill for three small copies.
// If you edit the rules, edit all three.
//
// Used for the priorities-prompt input box's live validateInput feedback —
// advisory only. The backend's governance/prompt_guard.py is still the
// authoritative enforcement point (see api/routes/analysis.py's
// _check_user_instructions validator) regardless of what this catches.

export interface GuardMatch {
  category: 'override' | 'gate_manipulation' | 'exfiltration';
  phrase: string;
}

const RULES: [GuardMatch['category'], string, RegExp][] = [
  // ── override: system/developer impersonation, instruction override ────────
  ['override', 'ignore previous instructions',
    /\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions?|rules?|prompt|guidance|directions?)\b/i],
  ['override', 'disregard the rules above',
    /\bdisregard\s+(the\s+)?(rules?|instructions?|policy|guidelines?)\s+(above|before)\b/i],
  ['override', 'you are now',
    /\byou\s+are\s+now\b/i],
  ['override', 'system/developer role header',
    /^\s*(system|developer)\s*:/im],
  ['override', 'new system prompt',
    /\bnew\s+system\s+prompt\b/i],
  ['override', 'override your instructions',
    /\boverride\s+(your|the)\s+(instructions?|rules?|guidelines?)\b/i],
  ['override', 'act as a different/unrestricted assistant',
    /\bact\s+as\s+(if\s+you\s+are\s+)?(a\s+)?(different|new|unrestricted)\b/i],

  // ── gate_manipulation: forcing outcomes / suppressing finding categories ──
  ['gate_manipulation', 'always approve',
    /\balways\s+(approve|pass)\b/i],
  ['gate_manipulation', 'never block/hold',
    /\bnever\s+(block|hold|fail)\b/i],
  ['gate_manipulation', 'mark everything as passing',
    /\bmark\s+(everything|all\s+findings?|this)\s+as\s+(low|passing|approved)\b/i],
  ['gate_manipulation', 'suppress security/secrets findings',
    /\b(ignore|skip|suppress|hide|don'?t\s+report|do\s+not\s+report)\s+(all\s+|any\s+)?(security|secrets?|vulnerabilit\w*|findings?)\b/i],
  ['gate_manipulation', 'skip the security agent',
    /\bskip\s+the\s+(security|secrets?)\s+agent\b/i],
  ['gate_manipulation', 'force approve/gate',
    /\bforce\s+(approve|gate)\b/i],
  ['gate_manipulation', 'set gate decision directly',
    /\bset\s+gate\s*(decision)?\s*=?\s*(approve|hold|block)\b/i],
  ['gate_manipulation', 'downgrade severity',
    /\bdowngrade\s+(all\s+)?(severity|findings?)\b/i],

  // ── exfiltration: system prompt extraction ─────────────────────────────────
  ['exfiltration', 'reveal your system prompt',
    /\b(repeat|print|reveal|show|output|dump)\s+(your\s+)?(system\s+prompt|instructions)\b/i],
  ['exfiltration', 'what is your system prompt',
    /\bwhat\s+(is|are)\s+your\s+(system\s+prompt|instructions)\b/i],
];

export function scanUserInstructions(text: string): { blocked: boolean; matches: GuardMatch[] } {
  if (!text) return { blocked: false, matches: [] };
  const matches = RULES.filter(([, , re]) => re.test(text)).map(([category, phrase]) => ({ category, phrase }));
  return { blocked: matches.length > 0, matches };
}
