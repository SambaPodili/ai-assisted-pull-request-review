"""
evaluation/judge.py
--------------------
Single LLM judge that evaluates one agent's output against a fixed rubric.

Uses UnifiedLLMClient so any configured provider (Anthropic, OpenAI, Azure,
Ollama) can act as a judge — the same abstraction the agents themselves use.
"""
from __future__ import annotations
import json
import logging
import re
import time
from typing import Any

from agents.llm_client import make_llm_client, ModelConfig
from agents.base_agent import _json_candidates, _strip_reasoning
from evaluation.models import JudgeScore

log = logging.getLogger(__name__)

# ── Rubric system prompt ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert code security reviewer acting as an INDEPENDENT JUDGE.

You will be shown:
  1. A code diff submitted for automated analysis
  2. The output from an AI analysis agent that reviewed the diff

Your task is to evaluate the QUALITY of the agent's analysis using the rubric below.
Be rigorous and critical — this evaluation is used to improve the AI system.
Do NOT be lenient: a score of 5 means genuinely excellent, not "good enough".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCORING RUBRIC — rate each dimension 1–5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

COMPLETENESS (1-5): How many real issues in this diff did the agent detect?
  1 = Missed most issues (>80% missed — severe gap)
  2 = Caught only the most obvious issues
  3 = Found roughly half of the real issues
  4 = Found most issues with minor gaps
  5 = Found all or nearly all real issues

PRECISION (1-5): What fraction of findings are legitimate (low false-positive rate)?
  1 = Majority are false positives (>50% wrong)
  2 = High false-positive rate (~35% wrong)
  3 = Acceptable (~75% correct)
  4 = Good precision (~90% correct)
  5 = Excellent — all findings appear legitimate

SEVERITY_ACCURACY (1-5): Are severity ratings correctly calibrated?
  1 = Systematically miscalibrated (e.g. critical issues rated low, trivial rated critical)
  2 = Major calibration errors on important findings
  3 = Roughly correct with some over/under-rating
  4 = Well-calibrated with minor errors only
  5 = All severity levels are precisely appropriate

SPECIFICITY (1-5): Are findings actionable with concrete details?
  1 = Too vague to act on (no file/line/remediation)
  2 = Some context but missing key details
  3 = Generally actionable with gaps
  4 = Good detail level, mostly specific
  5 = Highly specific — file paths, line ranges, clear remediation steps

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VERDICT THRESHOLDS (based on avg of the 4 scores)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  PASS    = avg >= 4.0  (agent performing well)
  PARTIAL = 2.5 <= avg < 4.0  (has gaps worth fixing)
  FAIL    = avg < 2.5  (significant quality problems)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT — respond ONLY with this JSON object (no markdown fences):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "completeness":      <integer 1-5>,
  "precision":         <integer 1-5>,
  "severity_accuracy": <integer 1-5>,
  "specificity":       <integer 1-5>,
  "missed_issues":     ["<specific issue the agent should have found>", ...],
  "false_positives":   ["<finding that appears to be incorrect>", ...],
  "verdict":           "PASS" | "PARTIAL" | "FAIL",
  "reasoning":         "<2-3 sentence explanation of your overall verdict>",
  "key_insight":       "<single most important observation about this agent's output>"
}

Rules:
  - missed_issues must describe REAL issues visible in the diff that the agent MISSED
  - false_positives must describe specific agent findings that appear wrong
  - Both lists may be empty if not applicable
  - Do not invent issues that are not visible in the diff

GROUNDING (avoid generic feedback):
  - Every missed_issue MUST cite concrete evidence from THIS diff — the exact
    file name, symbol/field/endpoint, and why it matters. Reject vague phrasing
    like "may introduce technical debt" or "could have security risks" unless
    tied to a specific changed line.
  - key_insight and reasoning must reference what actually changed in this diff,
    not boilerplate that could apply to any change.

SCOPE AWARENESS (do not punish correct silence):
  - Each agent has a narrow remit (e.g. the dependency agent analyses dependency
    MANIFESTS; the interface agent analyses API/data CONTRACTS; temporal_risk
    needs change HISTORY). If the diff contains nothing in an agent's remit, an
    empty or "no impact" output is CORRECT — score it PASS/PARTIAL, not FAIL, and
    say so in reasoning. Only fail an agent for missing issues that genuinely fall
    INSIDE its remit and are present in this diff.
"""


# ── LLMJudge ─────────────────────────────────────────────────────────────────

class LLMJudge:
    """
    A single LLM judge.

    Parameters
    ----------
    config : dict with keys: provider, model, api_key (optional), base_url (optional)
             Example: {"provider": "anthropic", "model": "claude-sonnet-4-6", "api_key": "..."}
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg    = ModelConfig.from_dict(config)
        self._client = make_llm_client(self._cfg)

    @property
    def judge_id(self) -> str:
        return f"{self._cfg.provider}/{self._cfg.model}"

    def evaluate(
        self,
        agent_name:    str,
        diff_text:     str,
        findings_json: dict[str, Any],
        max_tokens:    int = 1500,
    ) -> JudgeScore:
        """
        Evaluate one agent's output against the rubric.

        Parameters
        ----------
        agent_name    : The agent being evaluated, e.g. "security"
        diff_text     : The original code diff the agent analyzed
        findings_json : agent_result.model_dump() — the agent's full structured output
        max_tokens    : upper bound on judge response length
        """
        user_prompt = _build_user_prompt(agent_name, diff_text, findings_json)
        t0 = time.monotonic()

        # A reasoning model (Qwen/QwQ/R1) spends most of its output budget on
        # chain-of-thought, so 1500 tokens truncates the JSON (or leaves content
        # empty). Give the judge real headroom and honour the same env knobs the
        # agents use (LLM_MAX_OUTPUT_TOKENS / LLM_BUDGET_MULTIPLIER).
        eff_tokens = _judge_budget(max_tokens)

        try:
            response = self._client.create(
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=eff_tokens,
            )
            raw      = _strip_fences(response.text.strip())
            duration = round(time.monotonic() - t0, 2)
            data     = _parse_response(raw, self.judge_id, agent_name)
            return JudgeScore(
                judge_id=self.judge_id,
                agent_name=agent_name,
                duration_s=duration,
                fallback_used=False,
                **data,
            )

        except Exception as exc:
            duration = round(time.monotonic() - t0, 2)
            log.warning("[Judge %s] Failed for agent %s: %s", self.judge_id, agent_name, exc)
            return JudgeScore(
                judge_id=self.judge_id,
                agent_name=agent_name,
                completeness=1, precision=1, severity_accuracy=1, specificity=1,
                missed_issues=[f"[Judge error — could not evaluate: {exc}]"],
                false_positives=[],
                verdict="FAIL",
                reasoning=f"Judge call failed: {exc}",
                key_insight="",
                duration_s=duration,
                fallback_used=True,
            )


# ── Budget ────────────────────────────────────────────────────────────────────

_JUDGE_TOKEN_FLOOR = 4000   # reasoning models need room for CoT + the JSON answer


def _judge_budget(requested: int) -> int:
    """Effective judge max_tokens: an explicit LLM_MAX_OUTPUT_TOKENS wins, else a
    reasoning-safe floor; then scaled by LLM_BUDGET_MULTIPLIER."""
    try:
        from config.settings import get_settings
        cfg = get_settings()
        override = int(getattr(cfg, "llm_max_output_tokens", 0) or 0)
        mult     = float(getattr(cfg, "llm_budget_multiplier", 1.0) or 1.0)
    except Exception:
        override, mult = 0, 1.0
    base = override if override > 0 else max(requested, _JUDGE_TOKEN_FLOOR)
    return max(requested, int(base * mult))


# ── Prompt builder ────────────────────────────────────────────────────────────

_MAX_DIFF_CHARS     = 6_000
_MAX_FINDINGS_CHARS = 4_000


def _build_user_prompt(
    agent_name: str,
    diff_text:  str,
    findings:   dict[str, Any],
) -> str:
    # Trim to keep within token limits
    if len(diff_text) > _MAX_DIFF_CHARS:
        diff_text = diff_text[:_MAX_DIFF_CHARS] + "\n... [diff truncated]"

    findings_str = json.dumps(findings, indent=2, default=str)
    if len(findings_str) > _MAX_FINDINGS_CHARS:
        findings_str = findings_str[:_MAX_FINDINGS_CHARS] + "\n... [findings truncated]"

    return (
        f"AGENT UNDER EVALUATION: {agent_name}\n\n"
        f"=== CODE DIFF ===\n{diff_text}\n\n"
        f"=== AGENT OUTPUT ===\n{findings_str}\n\n"
        "Evaluate the agent output using the rubric. Respond with the JSON object only."
    )


# ── Response parsing ──────────────────────────────────────────────────────────

_REQUIRED_KEYS = {
    "completeness", "precision", "severity_accuracy", "specificity",
    "missed_issues", "false_positives", "verdict", "reasoning",
}


def _parse_response(raw: str, judge_id: str, agent_name: str) -> dict:
    """Robust JSON parsing — the SAME recovery the agents use, so reasoning models
    (Qwen / QwQ / DeepSeek-R1) parse too: strip <think>…</think> chain-of-thought,
    then try progressively-repaired candidates (handles trailing prose, comments,
    Python literals and TRUNCATED JSON when the model ran out of output budget).
    Validation errors on an otherwise-complete object propagate immediately."""
    cleaned = _strip_reasoning(raw or "")
    first_dict = None
    for candidate in _json_candidates(cleaned):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if _REQUIRED_KEYS.issubset(data.keys()):
            _validate_judge_data(data)   # raises with a specific message on bad values
            return data
        if first_dict is None:
            first_dict = data            # parseable but incomplete — keep for a precise error

    # A judge-shaped object that's merely missing fields gets the specific error;
    # genuinely-unparseable output gets the generic one.
    if first_dict is not None:
        _validate_judge_data(first_dict)

    raise ValueError(
        f"Judge {judge_id} returned unparseable output for agent {agent_name}: {(raw or '')[:200]!r}"
    )


def _validate_judge_data(data: dict) -> None:
    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        raise ValueError(f"Judge response missing fields: {missing}")
    for dim in ("completeness", "precision", "severity_accuracy", "specificity"):
        v = data.get(dim)
        if not isinstance(v, int) or not (1 <= v <= 5):
            raise ValueError(f"Field {dim!r} must be an integer 1–5, got {v!r}")
    if data.get("verdict") not in ("PASS", "PARTIAL", "FAIL"):
        raise ValueError(f"verdict must be PASS/PARTIAL/FAIL, got {data.get('verdict')!r}")


# ── Utility ───────────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:] if len(lines) > 1 else lines
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()
