"""
agents/base_agent.py
--------------------
Abstract base class for all specialist agents.
Uses UnifiedLLMClient so agents work with Anthropic, OpenAI, Ollama, or Azure.

Model config resolution order (highest priority first):
  1. context["model_config"] dict — per-request override from the UI
  2. Environment variables / settings.py defaults
"""
from __future__ import annotations
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, TypeVar, Generic, Type

from pydantic import ValidationError

from core.token_manager import TokenBudgetManager, estimate_tokens
from core.models import AgentName, AnalysisRequest, AgentResultBase, DiffHunk
from agents.llm_client import UnifiedLLMClient, ModelConfig, make_llm_client
from governance.observability import agent_span

log = logging.getLogger(__name__)
T   = TypeVar("T", bound=AgentResultBase)


def format_hunks_for_prompt(
    hunks: list[DiffHunk],
    max_chars_per_hunk: int = 3000,
    max_total_chars:    int = 40_000,   # ~10K tokens — keeps prompts focused
    focus: str = "general",             # "security" | "interface" | "general"
) -> str:
    """
    Format diff hunks into a prompt block with per-file language headers.

    For large PRs (>15 files) ranks hunks by relevance before including them:
      - Security focus: auth/crypto/config paths ranked highest
      - Interface focus: openapi/proto/schema paths ranked highest
      - General: ranked by raw change volume (additions + deletions)
    Includes a summary line when files are omitted so the LLM knows the scope.
    """
    from ingestion.language_registry import lang_meta
    from ingestion.diff_parser import is_low_signal_path

    if not hunks:
        return "(no diff hunks)"

    # ── Input guardrails: drop binary/lockfile/generated/vendored noise ───────
    signal = [h for h in hunks if not is_low_signal_path(h.file_path)]
    skipped_noise = len(hunks) - len(signal)
    if signal:                       # never end up empty (a binary-only PR keeps its hunks)
        hunks = signal

    # ── Rank hunks for large PRs ──────────────────────────────────────────────
    def _hunk_score(h: DiffHunk) -> int:
        score = h.additions + h.deletions
        fp = h.file_path.lower()
        if focus == "security":
            if any(k in fp for k in ("auth", "crypt", "secret", "password", "token",
                                     "jwt", "oauth", "key", "cert", "ssl", "tls")):
                score += 500
            if any(k in fp for k in ("config", "setting", "env", "permission", "rbac")):
                score += 200
            # Deprioritise tests — they contain keywords but aren't the vuln
            if any(k in fp for k in ("test", "spec", "mock", "fixture")):
                score -= 300
        elif focus == "interface":
            if any(k in fp for k in ("openapi", "swagger", "proto", "schema",
                                     "api-spec", "asyncapi", "contract")):
                score += 500
        return score

    ranked = sorted(hunks, key=_hunk_score, reverse=True)
    total_files = len(ranked)

    # ── Select hunks that fit within max_total_chars ──────────────────────────
    selected:    list[DiffHunk] = []
    running_chars = 0
    for h in ranked:
        budget = min(max_chars_per_hunk, max_total_chars - running_chars)
        if budget <= 0:
            break
        selected.append(h)
        running_chars += min(len(h.content), budget)

    omitted = total_files - len(selected)

    # ── Format selected hunks ─────────────────────────────────────────────────
    parts: list[str] = []
    if omitted > 0:
        parts.append(
            f"[Large PR: showing {len(selected)} of {total_files} reviewable files, "
            f"ranked by {'security sensitivity' if focus=='security' else 'change volume'}. "
            f"{omitted} lower-priority files omitted.]"
        )
    if skipped_noise > 0:
        parts.append(
            f"[{skipped_noise} binary/lockfile/generated file(s) excluded as low-signal.]"
        )

    for h in selected:
        meta    = lang_meta(h.language)
        budget  = min(max_chars_per_hunk, max_total_chars - sum(
            min(len(x.content), max_chars_per_hunk) for x in selected[:selected.index(h)]
        ))
        content = h.content[:max(budget, 200)]
        if len(h.content) > len(content):
            content += f"\n... [truncated — {h.additions + h.deletions} lines total]"
        parts.append(
            f"### File: {h.file_path}  "
            f"[language: {meta.display}]  "
            f"(+{h.additions}/-{h.deletions})\n"
            + content
        )
    return "\n\n".join(parts)


def count_files_in_llm_budget(hunks, max_chars_per_hunk: int = 3000,
                              max_total_chars: int = 40_000, focus: str = "general") -> dict:
    """How many changed files the default LLM prompt budget can include — mirrors
    format_hunks_for_prompt's ranking + packing so the coverage figure is exact.
    Returns {total, signal, reviewed, truncated, skipped_noise}. `truncated` =
    reviewed files whose diff is longer than the per-file char cap, so the model
    only sees a slice — these are full-coverage candidates for deep-scan."""
    from ingestion.diff_parser import is_low_signal_path
    if not hunks:
        return {"total": 0, "signal": 0, "reviewed": 0, "truncated": 0, "skipped_noise": 0}
    total = len(hunks)
    signal = [h for h in hunks if not is_low_signal_path(h.file_path)]
    skipped_noise = total - len(signal)
    pool = signal or hunks

    def _score(h):
        s = (h.additions or 0) + (h.deletions or 0)
        fp = (h.file_path or "").lower()
        if focus == "security" and any(k in fp for k in (
                "auth", "crypt", "secret", "password", "token", "jwt", "oauth", "key", "cert", "ssl", "tls")):
            s += 500
        return s

    used, reviewed, truncated = 0, 0, 0
    for h in sorted(pool, key=_score, reverse=True):
        budget = min(max_chars_per_hunk, max_total_chars - used)
        if budget <= 0:
            break
        reviewed += 1
        clen = len(h.content or "")
        if clen > budget:
            truncated += 1
        used += min(clen, budget)
    return {"total": total, "signal": len(pool), "reviewed": reviewed,
            "truncated": truncated, "skipped_noise": skipped_noise}


# Shared quality rubric appended to every agent's system prompt (see _call_llm).
_QUALITY_DIRECTIVE = (
    "\n\n"
    "━━ GROUNDING & QUALITY RULES (apply to every finding) ━━\n"
    "1. EVIDENCE IS MANDATORY. Every finding must tie to a SPECIFIC added/changed "
    "line in the provided diff. Put the real file path in `file_path`, the changed "
    "line number in `line`, and BEGIN the `description` by quoting the exact code "
    "token(s) from that line (verbatim, in backticks) that the finding is about. "
    "If you cannot quote the offending code from the diff, DO NOT report it.\n"
    "2. Do NOT invent files, symbols, endpoints, columns or issues that are not "
    "visible in the diff. The line you cite must actually contain what you describe "
    "(e.g. if you say 'inside a loop', a for/while/forEach must be in the quoted "
    "context; if you say a column is X, that column name must appear in the diff).\n"
    "3. NO SPECULATION. Do not report conditional or hypothetical issues. Banned "
    "unless you quote concrete proof from the diff: 'Potential…', 'Possible…', "
    "'may/might/could…', 'if X is not…', 'ensure/consider/verify that…'. If a risk "
    "depends on code you cannot see, OMIT it — another agent or the reviewer owns it. "
    "State confirmed facts about the diff, not advice.\n"
    "4. Precision over recall: a few well-grounded findings beat many speculative "
    "ones. Three real issues with quotes are worth more than ten guesses.\n"
    "5. Domain-specific anti-false-positive rules:\n"
    "   • N+1 / loop claims: only if a loop construct (for/while/forEach/stream) is "
    "visible in the diff around the DB call. A flat sequence of calls is NOT a loop.\n"
    "   • PII/privacy: flag only ACTUAL personal data (name, email, phone, SSN/NRIC, "
    "address, DOB, card/account number, etc.). Operational columns like "
    "error_message, error_code, *_status, *_id, timestamps are NOT PII.\n"
    "   • SQL injection: only when user-controlled input is concatenated into a query "
    "in the diff. A static/parameterised statement or a changed TABLE NAME is NOT "
    "injection.\n"
    "   • Schema: an index/primary key/foreign key defined inside a CREATE TABLE for a "
    "NEW table has no locking/online-DDL concern (the table is empty). Do not warn "
    "about CREATE INDEX locks for a table created in the same diff.\n"
    "6. Severity calibration — use consistently:\n"
    "   • CRITICAL = exploitable now / data loss / guaranteed production break\n"
    "   • HIGH     = likely incident, breaking change, or sensitive-data exposure\n"
    "   • MEDIUM   = a real issue that needs follow-up but isn't urgent\n"
    "   • LOW      = minor, stylistic, or defensive-improvement\n"
    "7. If nothing in YOUR domain applies to this diff, return an empty findings "
    "list — do not pad the response with generic best-practice advice. An empty list "
    "is a CORRECT, high-quality answer.\n"
)


class BaseAgent(ABC, Generic[T]):

    agent_name:    AgentName
    output_model:  Type[T]
    system_prompt: str

    # Subclasses may override to allow more output tokens for verbose responses.
    # The effective cap is min(output_token_cap, remaining_budget).
    output_token_cap: int = 4000

    def __init__(self, api_key: str | None = None) -> None:
        self._default_api_key = api_key

    # ── Shared logging ──────────────────────────────────────────────────────────
    # One consistent line per agent run so you can see, in the logs, whether each
    # agent actually called the LLM or fell back to static rules, and how many
    # findings it produced. Used by base run() AND by every agent that overrides
    # run() with a static-first path that would otherwise bypass this logging.

    @property
    def _key(self) -> str:
        # AgentName subclasses str, so isinstance(an, str) is True — use .value
        # (the lowercase agent key) when present, else the raw string.
        an = getattr(self, "agent_name", "agent")
        return getattr(an, "value", an)

    @staticmethod
    def _primary_count(result: Any) -> int:
        """Best-effort size of the result's main finding/list field, for logs."""
        for attr in ("findings", "pii_findings", "issues", "taint_paths",
                     "breaking_changes", "changes", "scenarios", "impacts",
                     "references", "requirements", "secrets", "uncovered_paths",
                     "hot_files", "gaps", "violations"):
            v = getattr(result, attr, None)
            if isinstance(v, list):
                return len(v)
        return 0

    def log_start(self, request: AnalysisRequest) -> None:
        log.info("[%s] %-22s start", request.request_id, self._key)

    def log_done(self, request: AnalysisRequest, result: Any,
                 mode: str | None = None, note: str = "") -> None:
        """Emit the canonical per-agent completion line.

        mode: 'llm' | 'static' | 'fallback' | 'no-budget' | 'skip'. When omitted it
        is inferred from result.fallback_used.
        """
        if mode is None:
            mode = "static" if getattr(result, "fallback_used", False) else "llm"
        log.info(
            "[%s] %-22s done   mode=%-9s findings=%-3d tokens=%-5d time=%6.2fs model=%s%s",
            request.request_id, self._key, mode,
            self._primary_count(result),
            getattr(result, "token_usage", 0) or 0,
            getattr(result, "duration_s", 0.0) or 0.0,
            getattr(result, "model_used", "") or "-",
            f"  {note}" if note else "",
        )

    def run(self, request: AnalysisRequest, budget: TokenBudgetManager, context: dict[str, Any] | None = None) -> T:
        ctx       = context or {}
        agent_key = self.agent_name.value
        model_cfg = self._resolve_model_config(ctx, agent_key)
        client    = make_llm_client(model_cfg)

        from core.progress import get_progress_store
        progress = get_progress_store().get_or_create(request.request_id)
        progress.agent_started(agent_key)

        self.log_start(request)
        user_prompt = self.build_user_prompt(request, ctx)
        needed      = estimate_tokens(user_prompt) + 800

        if not budget.check_and_reserve(agent_key, needed):
            result = self.fallback_result(request)
            result.fallback_used = True
            result.duration_s    = 0.0
            progress.agent_done(agent_key, 0, 0.0, "", True)
            log.warning("[%s] %-22s budget exhausted -> static fallback", request.request_id, agent_key)
            self.log_done(request, result, mode="no-budget")
            return result

        t0 = time.monotonic()
        with agent_span(agent_key, request.request_id):
            try:
                result, tokens = self._call_llm(client, user_prompt, budget.get_remaining(agent_key))
                duration = round(time.monotonic() - t0, 2)
                budget.record_usage(agent_key, tokens, client.model_name)
                result.token_usage   = tokens
                result.model_used    = client.model_name
                result.fallback_used = False
                result.duration_s    = duration
                progress.agent_done(agent_key, tokens, duration, client.model_name, False)
                self.log_done(request, result, mode="llm")
                return result
            except Exception as exc:
                duration = round(time.monotonic() - t0, 2)
                log.error("[%s] %-22s LLM error -> static fallback: %s",
                          request.request_id, agent_key, exc, exc_info=True)
                result = self.fallback_result(request)
                result.fallback_used = True
                result.duration_s    = duration
                progress.agent_done(agent_key, 0, duration, "", True)
                self.log_done(request, result, mode="fallback",
                              note="LLM call failed — see error above")
                return result

    @abstractmethod
    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str: ...

    @abstractmethod
    def fallback_result(self, request: AnalysisRequest) -> T: ...

    def report_static_progress(self, request: AnalysisRequest, duration_s: float = 0.0,
                               tokens: int = 0) -> None:
        """
        Emit start+done progress for a run that did NOT go through the LLM path
        (e.g. budget too low, or a static-only branch was taken).

        Custom run() overrides that conditionally skip super().run() must call
        this in the skip branch so the agent still appears in the live progress
        panel and the Timings tab. Safe to call exactly once per run.
        """
        from core.progress import get_progress_store
        key = self.agent_name.value if not isinstance(self.agent_name, str) else self.agent_name
        progress = get_progress_store().get_or_create(request.request_id)
        progress.agent_started(key)
        progress.agent_done(key, tokens, round(duration_s, 2), "static", False)

    def _call_llm(self, client: UnifiedLLMClient, user_prompt: str, remaining: int) -> tuple[T, int]:
        # Leave at least 200 tokens for the input; cap output at output_token_cap.
        # Reasoning models (Qwen/QwQ/R1) spend output tokens "thinking" before the
        # JSON, so a low cap yields an EMPTY answer. LLM_MAX_OUTPUT_TOKENS (>0)
        # raises the cap globally for such endpoints.
        from config.settings import get_settings as _gs
        _cap = getattr(_gs(), "llm_max_output_tokens", 0) or self.output_token_cap
        prompt_tokens = estimate_tokens(user_prompt)
        max_output    = min(_cap, max(200, remaining - prompt_tokens))

        schema_hint = json.dumps(self.output_model.model_json_schema(), indent=2)
        full_user   = (
            f"{user_prompt}\n\n"
            f"Respond ONLY with a valid JSON object matching this schema "
            f"(no markdown fences, no preamble):\n{schema_hint}"
        )
        # Append a shared grounding + severity rubric to EVERY agent's system
        # prompt. This lifts output quality uniformly: findings must tie to a real
        # changed line (no hallucinated files), severity is calibrated the same way
        # across agents, and agents stop padding with generic advice when nothing
        # in their domain applies.
        system = self.system_prompt + _QUALITY_DIRECTIVE
        response = client.create(system=system, user=full_user, max_tokens=max_output)
        raw      = _strip_fences(response.text.strip())
        tokens   = response.total_tokens

        parsed = self._parse_json(raw)
        return parsed, tokens

    def _parse_json(self, raw: str) -> T:
        """
        Parse the LLM JSON response, trying a chain of recovery candidates so that
        self-hosted models (Llama, Mistral, vLLM, …) that emit *almost*-valid JSON
        still parse instead of falling back to static rules:
          1. Direct parse — the happy path
          2. Balanced-brace extraction — strip preamble/suffix/fences
          3. Lenient repair (no deps) — Python literals (True/False/None), //+/* */
             comments, trailing commas
          4. Truncation repair — close strings/arrays/objects cut by max_tokens
          5. json-repair library (if installed) — handles unescaped inner quotes,
             single quotes, missing brackets, etc.
        """
        raw = _strip_reasoning(raw or "")   # drop <think>…</think> from reasoning models
        for candidate in _json_candidates(raw):
            try:
                return self.output_model.model_validate_json(candidate)
            except (ValidationError, ValueError):
                continue

        # All recovery failed — log WHAT came back so the cause is diagnosable
        # (truncated JSON vs. prose vs. an HTML proxy/error page vs. wrong model).
        diag = _diagnose_raw(raw)
        log.error(
            "[%s] Unparseable LLM response (%d chars) — %s\n  head: %s\n  tail: %s",
            self.agent_name, len(raw or ""), diag,
            (raw or "")[:300].replace("\n", "\\n"),
            (raw or "")[-200:].replace("\n", "\\n"),
        )
        raise ValueError(
            f"Could not parse {self.agent_name} response after all recovery attempts ({diag})"
        )

    def _resolve_model_config(self, context: dict[str, Any], agent_key: str) -> ModelConfig:
        """Resolve which provider/model/key this agent should use.

        Precedence, highest first:
          1. UI-supplied per-request override (context["model_config"])
          2. Backend settings / .env
        A blank api_key in the UI override means "use the configured env key"
        (env is only a *fallback*, never an override of a UI-supplied key).
        """
        override = context.get("model_config")
        if override:
            cfg = ModelConfig.from_dict(override)
            ui_named_model = bool((override.get("model") or "").strip())

            if cfg.provider == "anthropic":
                # The UI key is authoritative; fall back to the env key when blank.
                if not cfg.api_key and self._default_api_key:
                    cfg.api_key = self._default_api_key
                # Honour per-agent fast/strong selection ONLY when the UI did not
                # explicitly pick a model (otherwise the user's choice wins).
                if not ui_named_model:
                    from core.token_manager import MODEL_FAST, MODEL_STRONG, _STRONG_AGENTS
                    cfg.model = MODEL_STRONG if agent_key in _STRONG_AGENTS else MODEL_FAST
            else:
                # Self-hosted / custom / OpenAI / Azure share ONE endpoint+key. When
                # the UI omits the URL or key (e.g. several models — Llama, Qwen —
                # behind the same gateway, only the model name differs), fall back to
                # the backend env (LLM_BASE_URL / LLM_API_KEY) so the key stays
                # server-side and is sent as a Bearer header, never in the URL.
                from config.settings import get_settings as _gs
                _s = _gs()
                if not cfg.base_url:
                    cfg.base_url = (getattr(_s, "llm_base_url", "") or "").strip()
                if not cfg.api_key:
                    cfg.api_key = (getattr(_s, "llm_api_key", "") or getattr(_s, "openai_api_key", "") or "").strip()
            return cfg

        cfg = ModelConfig.from_settings()
        if self._default_api_key and cfg.provider == "anthropic":
            cfg.api_key = self._default_api_key
        if cfg.provider == "anthropic":
            from core.token_manager import MODEL_FAST, MODEL_STRONG, _STRONG_AGENTS
            cfg.model = MODEL_STRONG if agent_key in _STRONG_AGENTS else MODEL_FAST
        return cfg


# ── JSON extraction / diagnostics ─────────────────────────────────────────────

def _greedy_brace(raw: str) -> str:
    """First '{' to last '}' (fast path for prose-wrapped JSON)."""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    return m.group() if m else ""


def _extract_balanced_json(raw: str) -> str:
    """Return the first COMPLETE top-level {...} object, respecting strings and
    escapes. More reliable than a greedy regex when the model appends trailing
    prose after valid JSON."""
    if not raw:
        return ""
    start = raw.find("{")
    if start == -1:
        return ""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:            esc = False
            elif c == "\\":    esc = True
            elif c == '"':     in_str = False
        else:
            if c == '"':       in_str = True
            elif c == "{":     depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
    return ""   # never closed → truncated; let the repair step handle it


def _diagnose_raw(raw: str) -> str:
    """Heuristic label for an unparseable response — points at the real cause."""
    s = (raw or "").lstrip().lower()
    if not s:
        return "empty response (model returned nothing — check the API key/endpoint)"
    if s.startswith("<!doctype") or s.startswith("<html") or "<body" in s[:200]:
        return "looks like an HTML page (corporate proxy / captive portal / blocked endpoint)"
    if s.startswith(("i ", "sure", "here", "the ", "as ", "based ")):
        return "model returned prose, not JSON (model not following the JSON instruction)"
    if "{" in s and s.count("{") > s.count("}"):
        return "JSON truncated mid-response (raise the agent's output budget/cap)"
    return "malformed JSON"


# ── JSON repair ───────────────────────────────────────────────────────────────

def _repair_truncated_json(raw: str) -> str:
    """
    Close any open strings, arrays, and objects left by a mid-stream truncation.

    Walk the JSON character by character tracking:
      - Whether we are inside a string literal
      - The nesting stack of '{' and '['
    Then append the necessary closing characters.

    A trailing comma before the repair suffix is also stripped so that
    ``{"a": [1, 2,`` becomes ``{"a": [1, 2]}`` rather than ``{"a": [1, 2,]}``.
    """
    in_string   = False
    escape_next = False
    stack: list[str] = []   # '{' or '['

    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()

    suffix = ""
    if in_string:
        suffix += '"'          # close the dangling string

    # Drop a trailing comma that would produce invalid JSON after closing
    stripped = (raw + suffix).rstrip()
    if stripped.endswith(","):
        stripped = stripped[:-1]

    # Close containers in reverse order
    for opener in reversed(stack):
        stripped += "}" if opener == "{" else "]"

    return stripped


def _lenient_json_repair(raw: str) -> str:
    """No-dependency repair of the common self-hosted-LLM JSON quirks, preserving
    string contents (a state machine, NOT naive regex, so `https://` and code in
    string values are untouched):
      • strip // line comments and /* */ block comments
      • normalise Python literals True/False/None → true/false/null
      • drop trailing commas before } or ]
    """
    if not raw:
        return raw
    out: list[str] = []
    i, n = 0, len(raw)
    in_str = False
    esc = False
    _LITS = (("True", "true"), ("False", "false"), ("None", "null"))

    def _wordish(c: str) -> bool:
        return c.isalnum() or c == "_"

    while i < n:
        ch = raw[i]
        if in_str:
            if esc:
                out.append(ch); esc = False
            elif ch == "\\":
                out.append(ch); esc = True
            elif ch == '"':
                out.append(ch); in_str = False
            # Escape raw control characters the model left unescaped inside a
            # string value (a multi-line description = the #1 self-hosted-LLM bug).
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20:
                out.append("\\u%04x" % ord(ch))
            else:
                out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True; out.append(ch); i += 1; continue
        if ch == "/" and i + 1 < n and raw[i + 1] == "/":          # line comment
            nl = raw.find("\n", i); i = n if nl < 0 else nl; continue
        if ch == "/" and i + 1 < n and raw[i + 1] == "*":          # block comment
            end = raw.find("*/", i + 2); i = n if end < 0 else end + 2; continue
        matched = False
        for lit, repl in _LITS:
            if raw.startswith(lit, i) \
               and (i == 0 or not _wordish(raw[i - 1])) \
               and (i + len(lit) >= n or not _wordish(raw[i + len(lit)])):
                out.append(repl); i += len(lit); matched = True; break
        if matched:
            continue
        out.append(ch); i += 1

    s = "".join(out)
    s = re.sub(r",(\s*[}\]])", r"\1", s)   # trailing commas
    return s


def _json_repair_lib(raw: str) -> str | None:
    """Repair via the optional `json-repair` package (handles unescaped inner
    quotes, single quotes, missing brackets). Returns None when not installed."""
    if not raw:
        return None
    try:
        from json_repair import repair_json
    except ImportError:
        return None
    try:
        fixed = repair_json(raw)
        return fixed if isinstance(fixed, str) and fixed.strip() not in ("", "{}", "[]") else None
    except Exception:
        return None


def _json_candidates(raw: str):
    """Yield progressively-repaired parse candidates (deduped, cheapest first)."""
    seen: set[str] = set()

    def _emit(c):
        if c and c not in seen:
            seen.add(c)
            return c
        return None

    yield raw
    seen.add(raw)
    bal = _extract_balanced_json(raw) or _greedy_brace(raw)
    base = bal or raw
    for c in (_emit(bal),
              _emit(_lenient_json_repair(base)),
              _emit(_repair_truncated_json(base)),
              _emit(_repair_truncated_json(_lenient_json_repair(base))),
              _emit(_json_repair_lib(base)),
              _emit(_json_repair_lib(raw))):
        if c:
            yield c


def _strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:] if len(lines) > 1 else lines
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Remove <think>…</think> chain-of-thought that reasoning models (Qwen / QwQ /
    DeepSeek-R1 style) emit BEFORE the JSON answer. Handles closed blocks and a
    truncated/unclosed <think> (keep only what follows the last </think>, else the
    first '{'). Leaves normal responses untouched."""
    if not text or "<think>" not in text.lower():
        return text
    out = _THINK_RE.sub("", text)
    if "<think>" in out.lower():                 # unclosed (truncated mid-think)
        end = out.lower().rfind("</think>")
        if end != -1:
            out = out[end + len("</think>"):]
        else:
            brace = out.find("{")
            out = out[brace:] if brace != -1 else ""
    return out.strip()
